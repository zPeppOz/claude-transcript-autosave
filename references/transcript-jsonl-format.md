# Il formato `.jsonl` dei transcript di Claude Code

Rilevato empiricamente il 2026-08-18 su 16 progetti / ~24.000 record
(`~/.claude/projects/*/*.jsonl`, Claude Code 2.1.234). Non è un formato
documentato: trattalo come osservazione, non come contratto. Serve a decidere
cosa rendere e cosa scartare in `scripts/transcript_lib.py`.

Un transcript è un file JSON Lines, una riga per record, in ordine cronologico di
scrittura. Il nome del file è `<session-id>.jsonl` sotto
`~/.claude/projects/<cwd-slugificato>/`.

## Campi comuni

Presenti su quasi tutti i record: `type`, `sessionId`, `timestamp` (ISO 8601 con
`Z`), `uuid`, `parentUuid`, `cwd`, `gitBranch`, `version`, `userType`,
`isSidechain`, `entrypoint`.

`parentUuid` forma un albero, non una lista: un ramo abbandonato (dopo una
modifica di prompt) resta nel file. Il renderer segue l'ordine del file e non
l'albero — è una semplificazione consapevole, non un bug.

## Tipi che contengono conversazione

| `type` | Occorrenze | Contenuto |
|---|---|---|
| `assistant` | 9.354 | `message.content[]` con blocchi `text`, `thinking`, `tool_use`; `message.model`, `message.usage` |
| `user` | 5.751 | `message.content` stringa oppure `[]` con blocchi `text`, `tool_result`, `image`, `document` |
| `system` | 721 | `subtype` + `level` + `content`; quasi sempre telemetria |

Dettagli che contano per il rendering:

- **I risultati dei tool non stanno nel record che li ha chiamati.** Un
  `tool_use` con `id: "t1"` nel record `assistant` trova il suo output in un
  blocco `tool_result` con `tool_use_id: "t1"` nel record `user` successivo. Vanno
  ricuciti, altrimenti si legge una lista di chiamate senza risposte.
- `tool_result.content` è una stringa nel 95% dei casi, una lista di blocchi
  (`text`, `image`) nel resto. `is_error: true` segnala il fallimento.
- Alcuni record `user` portano `toolUseResult` a livello top-level: la forma
  strutturata dello stesso risultato, utile come fallback.
- `isMeta: true` sui record `user` marca contesto iniettato dal sistema
  (system-reminder, output di hook): rumore per un lettore umano, escluso di
  default.
- `message.usage` contiene `input_tokens`, `output_tokens`,
  `cache_read_input_tokens`, `cache_creation_input_tokens`. I token di cache
  dominano il totale sulle sessioni lunghe.

### `system.subtype` osservati

`turn_duration` (309), `stop_hook_summary` (305), `away_summary` (59),
`local_command` (38), `bridge_status` (6), `informational` (4). Tutti tranne
`local_command` e `informational` hanno `content: null`: sono telemetria e
vengono scartati.

## Tipi di sola interfaccia (scartati)

`attachment` (2.499), `last-prompt` (1.442), `ai-title` (1.350), `mode` (1.100),
`queue-operation` (612), `permission-mode` (587), `file-history-delta` (308),
`file-history-snapshot` (232), `agent-name` (120), `bridge-session` (85),
`agent-setting` (8), `worktree-state` (3), `relocated` (3).

Uno merita un'eccezione: **`ai-title`** porta `aiTitle`, il titolo che Claude Code
genera per la sessione. È l'unica etichetta umana disponibile e viene usata come
titolo del documento archiviato.

## Subagent

`isSidechain` è risultato `false` su tutti i record osservati: i transcript dei
subagent stanno in file separati sotto
`~/.claude/projects/<progetto>/<session-id>/subagents/agent-<id>.jsonl`, e il
payload dell'hook `SubagentStop` li indica in `agent_transcript_path` (campo
distinto da `transcript_path`).

Per questo `SubagentStop` è supportato da `install_hooks.py` ma non attivo di
default: archivierebbe il transcript della sessione principale una volta per ogni
subagent, non quello del subagent. Prima di attivarlo, `save_transcript.py` va
esteso per leggere `agent_transcript_path` quando presente.

## Dopo la compattazione

Un record `summary` (con `leafUuid`) compare quando il contesto viene compattato.
Il renderer non lo tratta in modo speciale: il caso è coperto dallo snapshot
immutabile `PreCompact`, che cattura la conversazione integrale *prima* che il
riassunto la sostituisca.

## Attenzione: `~/.claude/transcripts` non è libera

Claude Code usa già `~/.claude/transcripts/` per proprio conto: al 2026-08-18
conteneva 1.258 file `ses_*.jsonl` (un record `user` ciascuno, da febbraio a
giugno 2026). Per questo l'archivio di questa skill vive in
`~/.claude/session-archive/`: condividere una cartella con uno strumento che la
gestisce — e potenzialmente la ripulisce — è il modo più rapido per perdere
l'archivio. `install_hooks.py` avvisa se la radice configurata contiene file che
non ha scritto lui.
