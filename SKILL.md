---
name: transcript-autosave
description: Memoria automatica delle conversazioni con Claude Code. Aggancia gli hook Stop, SessionEnd e PreCompact per archiviare ogni sessione su disco (Markdown leggibile + copia JSONL), mantiene in ogni cartella di progetto un INDEX.md interrogabile con richiesta iniziale, richieste successive e file toccati di ogni conversazione, e a ogni SessionStart inietta nella nuova sessione le voci recenti dell'indice del progetto. Usa questa skill ogni volta che serve ricordare o ritrovare qualcosa detto in una sessione passata: "cosa avevamo deciso su X", "come avevamo risolto quel bug", "riprendiamo il lavoro di ieri", "di cosa abbiamo parlato la settimana scorsa", "quale sessione ha toccato questo file", "ho perso tutto dopo il /clear". Usala anche quando si parla di salvare, archiviare, esportare, conservare o versionare transcript, conversazioni o cronologia delle sessioni; di attivare, disattivare, verificare o diagnosticare il salvataggio automatico; di hook Stop / SessionEnd / PreCompact / SubagentStop; o di ricostruire l'indice dell'archivio. Prima di rispondere "non ho memoria delle sessioni precedenti", controlla qui: probabilmente ce l'hai.
---

# Transcript autosave

Dà a Claude una memoria delle conversazioni passate. Tre hook (`Stop`,
`SessionEnd`, `PreCompact`) archiviano ogni sessione mentre accade, ogni
cartella di progetto mantiene un indice interrogabile di cosa è stato detto, e
un quarto hook (`SessionStart`) porta le voci recenti di quell'indice dentro
ogni nuova sessione, così il passato si presenta da solo invece di aspettare un
`grep`:

```
~/.claude/session-archive/
├── INDEX.md                              # mappa dei progetti
└── <progetto>/
    ├── INDEX.md                          # una voce per conversazione
    ├── _index.json                        # gli stessi dati, machine-readable
    ├── <data>_<ora>-<sid8>.md            # la conversazione, leggibile
    └── <data>_<ora>-<sid8>.jsonl         # copia fedele del transcript nativo
```

Archiviare senza indicizzare non sarebbe memoria: sarebbe un mucchio di file in
cui, per ricordare qualcosa, bisogna prima indovinare quale dei duecento aprire.
L'indice esiste per rendere quella domanda rispondibile con un `grep`.

Il `.md` viene **riscritto** a ogni fine turno, quindi contiene sempre lo stato
corrente della sessione: un file vivo per sessione, non uno per turno. Il
`.jsonl` è la copia bit-per-bit del transcript nativo, la rete di sicurezza se
un giorno il rendering dovesse sbagliare qualcosa.

Due punti da tenere a mente quando lavori su questa skill:

- **L'hook funziona senza che la skill sia caricata.** Lo script è autonomo:
  questa skill è l'installer e il manuale operativo, non una dipendenza runtime.
  Se l'utente dice "non sta salvando", non cercare il problema nella skill —
  guarda la registrazione dell'hook e il log.
- **Lo snapshot `PreCompact` è immutabile** (`.precompact-1.md`, `-2`, …). La
  compattazione è l'unico momento in cui la conversazione integrale pre-riassunto
  esiste ancora; se lo scrivessimo sul file canonico, il turno successivo — che
  vede già il transcript compattato — lo sovrascriverebbe distruggendo proprio
  ciò che volevamo salvare.

## Installare

Dalla radice del repo:

```bash
./install.sh                      # registra la skill + gli hook (Stop, SessionEnd, PreCompact, SessionStart)
./install.sh --dry-run            # mostra cosa cambierebbe nei settings, senza scrivere
./install.sh --events Stop        # solo fine turno
python3 scripts/install_hooks.py --status   # cosa è registrato, dove, quante sessioni in archivio
```

`install_hooks.py` scrive in `~/.claude/settings.json` fondendosi con la
configurazione esistente: rimuove e riscrive **solo** le proprie voci, non tocca
gli altri hook, e fa un backup `settings.json.bak-<timestamp>` prima di
scrivere. Rieseguirlo è sicuro e converge — non accumula duplicati. Preferisci
sempre questo script alla modifica a mano dei settings, che è il modo abituale di
perdere un hook altrui.

Claude Code rilegge i settings a caldo, quindi anche le sessioni già aperte
iniziano a salvare senza riavvio (verificato sul campo: una sessione avviata
un'ora prima dell'installazione ha archiviato da sola al primo fine turno utile).
Se `--status` mostra la registrazione ma il log resta vuoto, apri `/hooks` per
forzare il rilettura.

Un'eccezione da conoscere, perché sembra un guasto e non lo è: **lo `Stop` non
scatta se interrompi il turno con ESC.** È il comportamento documentato di Claude
Code — l'hook non gira sulle interruzioni utente. È esattamente il buco che
`SessionEnd` chiude, ed è il motivo per cui conviene tenerlo registrato: alla
chiusura della sessione il transcript viene comunque archiviato per intero.

Per portarsi dietro anche le sessioni già esistenti (l'hook vede solo quelle
future, e lo storico è spesso la parte che vale):

```bash
python3 scripts/save_transcript.py --backfill              # tutto lo storico
python3 scripts/save_transcript.py --backfill --limit 50   # le 50 più recenti
```

## Verificare che stia salvando

In ordine di utilità, dal segnale più diretto:

```bash
tail -5 ~/.claude/session-archive/_autosave.log     # una riga per ogni esecuzione dell'hook
ls -lt ~/.claude/session-archive/*/ | head          # i file più recenti
python3 scripts/install_hooks.py --status       # registrazione + conteggio sessioni
```

Il log è la prova decisiva: ogni invocazione lascia una riga `OK Stop <sid> N
record Xms -> <file>` oppure `SKIP`/`ERRORE` con il motivo. Se il log non ha una
riga nuova dopo un turno, l'hook non è stato invocato — il problema è nei
settings, non nello script.

## Usare l'archivio come memoria

È l'uso principale della skill. Quando l'utente chiede qualcosa che riguarda il
passato — "cosa avevamo deciso su X", "come l'avevamo risolto", "riprendiamo da
dove eravamo" — non rispondere che non hai memoria delle sessioni precedenti:
cercala. Il percorso è sempre lo stesso, dal generale al particolare.

```bash
# 1. quali progetti esistono e quando sono stati toccati l'ultima volta
cat ~/.claude/session-archive/INDEX.md

# 2. in quali conversazioni si è parlato di un tema (cerca in tutti gli indici)
grep -i -B2 "openfga" ~/.claude/session-archive/*/INDEX.md

# 3. quale sessione ha modificato un certo file
grep -l "src/lib/import-platform/etl.ts" ~/.claude/session-archive/*/INDEX.md

# 4. leggi la conversazione trovata
cat ~/.claude/session-archive/addway-siarx/2026-07-28_1803-eed271b7.md
```

Cerca **prima negli `INDEX.md`**: sono piccoli e densi. Nell'archivio reale
l'indice del progetto più grande pesa 64 KB contro i 59 MB dei suoi transcript —
un `grep -r` su tutto riempirebbe il contesto di rumore per trovare la stessa
cosa.

Se l'indice non basta, scendi al secondo livello. L'indice contiene la richiesta
iniziale, il titolo e i file toccati: un termine detto a metà conversazione lì non
c'è. In quel caso restringi a un progetto e cerca nei transcript, che è costoso
ma mirato:

```bash
# nell'indice non c'è: cerca nel testo delle conversazioni di UN progetto
grep -rl "openfga" ~/.claude/session-archive/addway-siarx/*.md

# con contesto, per capire se è la sessione giusta prima di aprirla
grep -rn -C2 "openfga" ~/.claude/session-archive/addway-siarx/*.md | head -40
```

L'ordine conta: indice → transcript di un progetto → transcript completo. Saltare
al terzo livello significa caricare decine di MB per una domanda che l'indice
avrebbe risolto in un `grep`.

Ogni voce dell'indice contiene le chiavi con cui si recupera davvero una
conversazione, scelte perché rispondono alle domande che le persone fanno
davvero:

- **`chiesto:`** — la richiesta iniziale con le parole dell'utente, prima che la
  conversazione derivasse. È il campo più utile dell'indice.
- **`poi:`** — le ultime richieste sostanziali dopo la prima. Una sessione
  partita come "fixa il test" e finita a ridisegnare l'autenticazione sarebbe
  invisibile a `grep` senza questo campo.
- **`modificati:`** / **`consultati:`** — i file toccati. Risponde a "quale
  sessione ha lavorato su questo file", che titolo e data da soli non possono.
- titolo, data, durata, numero di turni, branch git, snapshot pre-compattazione.

Se un indice manca, è corrotto o precede una modifica al formato, si ricostruisce
dai transcript già archiviati — funziona anche se gli originali in
`~/.claude/projects` non ci sono più:

```bash
python3 scripts/save_transcript.py --rebuild-index
```

L'indice si aggiorna da solo a ogni fine turno: una voce per sessione, non per
turno. Le sessioni parallele sullo stesso progetto sono serializzate da un lock,
perché due scritture simultanee farebbero sparire una conversazione dalla memoria
senza alcun errore visibile.

## Configurare

Variabili d'ambiente, tutte opzionali. Impostale nel tuo profilo shell o in
`env` dentro i settings di Claude Code.

| Variabile | Default | Effetto |
|---|---|---|
| `CLAUDE_TRANSCRIPT_DIR` | `~/.claude/session-archive` | Radice dell'archivio |
| `CLAUDE_TRANSCRIPT_AUTOSAVE` | `1` | `0` sospende il salvataggio senza disinstallare |
| `CLAUDE_TRANSCRIPT_THINKING` | `1` | `0` esclude i blocchi di ragionamento dal `.md` |
| `CLAUDE_TRANSCRIPT_META` | `0` | `1` include i messaggi di sistema iniettati |
| `CLAUDE_TRANSCRIPT_MAX_RESULT` | `2000` | Caratteri di output tool tenuti per chiamata |
| `CLAUDE_TRANSCRIPT_MAX_MB` | `25` | Oltre questa soglia salva solo il `.jsonl` |
| `CLAUDE_TRANSCRIPT_INJECT` | `1` | `0` disattiva l'iniezione di memoria a `SessionStart` |
| `CLAUDE_TRANSCRIPT_INJECT_SESSIONS` | `5` | Quante sessioni recenti iniettare a inizio sessione |

Il `.jsonl` non è mai filtrato da queste opzioni: qualsiasi cosa escludi dal
Markdown resta nella copia fedele.

## Diagnosticare

| Sintomo | Causa probabile | Verifica |
|---|---|---|
| Nessuna riga nuova nel log | Hook non registrato in un file che Claude Code legge | `install_hooks.py --status` |
| Riga `SKIP ... transcript non trovato` | `transcript_path` del payload non esiste | leggi il percorso nel log |
| Riga `ERRORE` | Bug di rendering su una forma di record nuova | `python3 -m unittest discover -s tests` |
| Salva due volte per turno | Registrato sia in `settings.json` sia in `settings.local.json` | `--status` elenca entrambi |
| Solo `.jsonl`, niente `.md` | Transcript oltre `CLAUDE_TRANSCRIPT_MAX_MB` | il log dice `render saltato` |
| Un file per turno invece di uno per sessione | Il transcript non ha timestamp: nomi `nodate-*` | controlla i nomi in archivio |

Lo script esce **sempre** con codice 0 e su stdout scrive o niente o
esattamente il JSON che Claude Code si aspetta: gli hook di salvataggio non
stampano nulla, `SessionStart` stampa il payload di iniezione oppure nulla. Un
hook che fallisce rumorosamente, o che stampa testo spurio dove Claude Code si
aspetta JSON, trasformerebbe una comodità di sfondo in un fastidio a ogni turno.
Per questo gli errori finiscono nel log invece di interrompere il turno — e per
questo il log è il primo posto dove guardare.

L'iniezione a `SessionStart` lascia anch'essa una riga di log (`OK SessionStart
<sid> iniettati N caratteri` oppure `SKIP ... niente da iniettare`). Non scatta
alla ripresa post-compattazione (`source: compact`, il contesto c'è già), non
inietta la voce della sessione stessa quando si riprende con `--resume`, e il
payload è comunque limitato a pochi KB.

## Rimuovere

```bash
./install.sh --uninstall     # togli gli hook e la skill installata
```

I file già archiviati non vengono toccati: cancellali a mano se vuoi. Per fare
pulizia periodica:

```bash
find ~/.claude/session-archive -name '*.md' -mtime +180 -delete
```

## Privacy

I transcript contengono spesso segreti, token e codice privato: tutto ciò che è
passato nella conversazione. I file vengono scritti con permessi `0600`, come i
transcript nativi di Claude Code. Prima di committare o condividere un file
archiviato, leggilo. Se l'utente vuole l'archivio dentro un repo, imposta
`CLAUDE_TRANSCRIPT_DIR` su una cartella ignorata da git e diglielo esplicitamente.

## Estendere

`references/transcript-jsonl-format.md` documenta i tipi di record del formato
`.jsonl` così come sono stati osservati sui transcript reali — leggilo prima di
modificare il rendering, perché la maggior parte dei tipi è rumore di interfaccia
e va scartata, non resa.

Claude Code introduce tipi di record nuovi tra una release e l'altra: il
renderer li ignora invece di sollevare eccezioni, e i test coprono
esplicitamente questo caso. Se aggiungi un caso, aggiungi il test — la suite gira
in mezzo secondo:

```bash
python3 -m unittest discover -s tests
```
