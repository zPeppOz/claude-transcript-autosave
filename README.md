# claude-transcript-autosave

Salva su disco il transcript di ogni sessione Claude Code, automaticamente, a
ogni fine turno dell'agente. Nessun comando da ricordare: si aggancia agli hook
di Claude Code e archivia in Markdown leggibile più copia `.jsonl` fedele.

```
~/.claude/session-archive/
├── addway-siarx/
│   ├── 2026-08-18_1724-5133ad3d.md      ← riscritto a ogni turno, sempre aggiornato
│   ├── 2026-08-18_1724-5133ad3d.jsonl   ← copia bit-per-bit del transcript nativo
│   └── 2026-08-17_0910-eed271b7.precompact-1.md   ← snapshot immutabile pre-compattazione
├── xr1-tesoro/
└── _autosave.log
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

```bash
grep -rl "openfga" ~/.claude/session-archive --include='*.md'   # in quali sessioni ne ho parlato
grep -h '^title:' $(find ~/.claude/session-archive -name '*.md' -mtime -7)
tail -5 ~/.claude/session-archive/_autosave.log                  # sta salvando?
```

## Comandi

```bash
./install.sh                                 # installa skill + hook
./install.sh --dry-run                       # mostra il diff dei settings senza scrivere
./install.sh --events Stop                   # solo fine turno
./install.sh --uninstall                     # rimuove hook e skill (l'archivio resta)
python3 scripts/install_hooks.py --status     # cosa è registrato e dove
python3 scripts/save_transcript.py --backfill # archivia lo storico esistente
python3 -m unittest discover -s tests         # 69 test, mezzo secondo
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
  moltiplicarsi.
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
scripts/install_hooks.py              merge idempotente dei settings, con backup
references/transcript-jsonl-format.md il formato .jsonl come osservato sul campo
tests/                                69 test, standard library
```
