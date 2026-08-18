# claude-transcript-autosave

Una memoria automatica delle conversazioni con Claude Code. Si aggancia agli
hook, archivia ogni sessione mentre accade — Markdown leggibile più copia
`.jsonl` fedele — e mantiene in ogni progetto un indice interrogabile di cosa è
stato detto, così una sessione passata si ritrova con un `grep` invece che a
memoria.

```
~/.claude/session-archive/
├── INDEX.md                             ← mappa dei progetti, ultima attività
├── addway-siarx/
│   ├── INDEX.md                         ← una voce per conversazione: richiesta e file toccati
│   ├── _index.json                      ← gli stessi dati, machine-readable
│   ├── 2026-08-18_1724-5133ad3d.md      ← riscritto a ogni turno, sempre aggiornato
│   ├── 2026-08-18_1724-5133ad3d.jsonl   ← copia bit-per-bit del transcript nativo
│   └── 2026-08-17_0910-eed271b7.precompact-1.md   ← snapshot immutabile pre-compattazione
├── xr1-tesoro/
└── _autosave.log
```

Una voce di indice:

```markdown
## 2026-07-28 18:03 — Add DDP import and fascicolo tracking
- `2026-07-28_1803-eed271b7.md` · 2 turni · 15 tool · 3 min · branch `test`
- chiesto: Review this change for security vulnerabilities…
- consultati: `src/lib/import-platform/etl.ts`, `src/lib/import-platform/persist.ts` (+8)
```

## Installazione

```bash
git clone <questo-repo> ~/xr1/claude-transcript-autosave
cd ~/xr1/claude-transcript-autosave
./install.sh
```

Registra la skill in `~/.claude/skills/` e tre hook in `~/.claude/settings.json`
(`Stop`, `SessionEnd`, `PreCompact`), fondendosi con la configurazione esistente
e facendone un backup. Vale anche per le sessioni già aperte, senza riavvio.

Nota: lo `Stop` non scatta sui turni interrotti con ESC — è il comportamento di
Claude Code, non un difetto. `SessionEnd` copre quel caso alla chiusura.

Per archiviare anche lo storico già su disco:

```bash
python3 scripts/save_transcript.py --backfill
```

## Cosa produce

Il Markdown ha frontmatter YAML interrogabile (titolo generato da Claude Code,
progetto, branch, modelli, token, conteggio turni) e un corpo che ricuce ogni
tool call con il suo output — la cosa che manca al `.jsonl` grezzo. Il
ragionamento e gli output lunghi stanno in blocchi `<details>` richiudibili; i
messaggi molto lunghi vengono piegati, mai troncati.

## Ricordare

Si cerca negli indici, che sono piccoli e densi, non nei transcript, che sono
decine di MB:

```bash
cat ~/.claude/session-archive/INDEX.md                              # quali progetti, ultima attività
grep -i -B2 "openfga" ~/.claude/session-archive/*/INDEX.md          # dove se n'è parlato
grep -l "src/lib/etl.ts" ~/.claude/session-archive/*/INDEX.md       # chi ha toccato quel file
tail -5 ~/.claude/session-archive/_autosave.log                     # sta salvando?
```

I due campi che fanno il lavoro sono `chiesto:` — la richiesta iniziale con le
parole dell'utente — e `modificati:` / `consultati:`, i file toccati. Titolo e
data da soli non rispondono a "quale sessione ha lavorato su questo file".

Se l'indice non basta — un termine detto a metà conversazione lì non c'è — si
scende ai transcript di un solo progetto: `grep -rl "openfga"
~/.claude/session-archive/addway-siarx/*.md`. L'ordine importa: sull'archivio
reale l'indice del progetto più grande pesa 64 KB contro 59 MB di transcript.

## Comandi

```bash
./install.sh                                 # installa skill + hook
./install.sh --dry-run                       # mostra il diff dei settings senza scrivere
./install.sh --events Stop                   # solo fine turno
./install.sh --uninstall                     # rimuove hook e skill (l'archivio resta)
python3 scripts/install_hooks.py --status     # cosa è registrato e dove
python3 scripts/save_transcript.py --backfill      # archivia lo storico esistente
python3 scripts/save_transcript.py --rebuild-index # ricostruisce gli indici dall'archivio
python3 -m unittest discover -s tests              # 123 test, mezzo secondo
```

## Configurazione

`CLAUDE_TRANSCRIPT_DIR`, `CLAUDE_TRANSCRIPT_AUTOSAVE`,
`CLAUDE_TRANSCRIPT_THINKING`, `CLAUDE_TRANSCRIPT_META`,
`CLAUDE_TRANSCRIPT_MAX_RESULT`, `CLAUDE_TRANSCRIPT_MAX_MB` — tabella completa in
[`SKILL.md`](SKILL.md).

## Garanzie di progetto

- **Non interferisce mai con un turno.** Lo script esce sempre con codice 0 e non
  scrive nulla su stdout, dove Claude Code si aspetta JSON. Gli errori vanno in
  `_autosave.log`.
- **Un file per sessione, non per turno.** Il nome deriva dal *primo* timestamp
  della sessione, non dall'orologio, così il file viene aggiornato invece di
  moltiplicarsi. Vale anche per l'indice: una voce per conversazione.
- **Gli indici sopravvivono a se stessi.** `_index.json` è una cache: se sparisce
  o si corrompe, `--rebuild-index` lo ricostruisce dai transcript archiviati. Le
  sessioni parallele sullo stesso progetto sono serializzate da un lock.
- **Gli hook altrui restano al loro posto.** L'installer riscrive solo le proprie
  voci, con backup timestampato; rieseguirlo converge senza duplicati.
- **Scritture atomiche, permessi `0600`.** Un hook ucciso dal timeout non lascia
  file troncati, e l'archivio non allarga i permessi dei transcript nativi.
- **Se sposti o cancelli il repo, l'hook diventa un no-op**, non un errore a ogni
  turno: il comando registrato è protetto da un test di esistenza del file.

## Requisiti

Python 3.8+ (solo standard library), shell POSIX, Claude Code 2.x. Testato su
Linux/WSL2.

## Struttura

```
SKILL.md                              istruzioni per Claude: installare, verificare, cercare
install.sh                            front door: skill + hook
scripts/save_transcript.py            l'hook: stdin → archivio (esce sempre 0)
scripts/transcript_lib.py             parsing e rendering, senza side effect
scripts/session_index.py              l'indice per progetto: estrazione e rendering
scripts/install_hooks.py              merge idempotente dei settings, con backup
references/transcript-jsonl-format.md il formato .jsonl come osservato sul campo
tests/                                123 test, standard library
```
