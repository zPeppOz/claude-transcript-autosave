# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An automatic memory for Claude Code conversations. Three hooks (`Stop`, `SessionEnd`, `PreCompact`) archive every session to `~/.claude/session-archive/` as readable Markdown plus a faithful `.jsonl` copy, and maintain a per-project `INDEX.md` so past sessions can be found with `grep` instead of guesswork. A fourth hook (`SessionStart`) pushes the recent index entries of the project into each new session as `additionalContext`, so the archive surfaces without being asked. Pure Python 3.8+ standard library, no dependencies. Docs (README, SKILL.md) are in Italian.

## Commands

```bash
python3 -m unittest discover -s tests                    # full suite (~0.5s)
python3 -m unittest tests.test_transcript_lib            # one module
python3 -m unittest tests.test_session_index.TestClass.test_name   # one test

./install.sh                                 # install skill + hooks into ~/.claude
./install.sh --dry-run                       # show the settings diff without writing
./install.sh --uninstall                     # remove hooks and skill (archive untouched)
python3 scripts/install_hooks.py --status    # what is registered and where
python3 scripts/save_transcript.py --backfill        # archive pre-existing transcripts
python3 scripts/save_transcript.py --rebuild-index   # rebuild indexes from archived files
```

## Architecture

Four scripts in `scripts/`, wired together like this:

- **`save_transcript.py`** — the hook entry point. Reads the hook event JSON from stdin, copies the native transcript, renders Markdown via `transcript_lib`, and updates the index via `session_index`. On `SessionStart` it instead reads the project's `_index.json` and prints the recent entries as the hook's `additionalContext` JSON (skipping `source: compact` and the resumed session's own entry, capped at a few KB). Also hosts the CLI (`--backfill`, `--rebuild-index`).
- **`transcript_lib.py`** — parsing and Markdown rendering, deliberately side-effect free so tests can exercise it without spawning a hook process. Knows which `.jsonl` record types are conversation content and which are UI noise to skip.
- **`session_index.py`** — the memory layer. Maintains per-project `_index.json` (source of truth, one entry per session) and renders `INDEX.md` from it, plus the root project map. Each save updates only its own entry rather than re-parsing the whole archive. Entries are optimized for retrieval: the verbatim opening request and the files the session touched (including paths inferred from Bash commands).
- **`install_hooks.py`** — idempotent settings merge. Identifies its own entries by the `save_transcript.py` marker in the command, rewrites only those, backs up `settings.json` before writing. The registered command is guarded by a file-existence test so a moved/deleted repo becomes a no-op, not a per-turn error.

`references/transcript-jsonl-format.md` documents the `.jsonl` record types as observed on real transcripts — read it before changing the rendering. Most record types are interface noise and must be skipped, not rendered.

## Invariants — do not break these

1. **The hook always exits 0, and stdout carries either nothing or exactly the JSON Claude Code expects.** The save hooks print nothing; `SessionStart` prints the injection payload or nothing. Stray output or a nonzero exit turns a background convenience into per-turn interference. Errors go to `<archive>/_autosave.log`.
2. **Unknown record types are skipped, never fatal.** Claude Code adds new `.jsonl` record types between releases; a renderer that raises would cost the user their archive. Tests cover this case explicitly — if you add handling for a shape, add a test.
3. **One file per session, not per turn.** Filenames derive from the session's *first* timestamp, so each turn rewrites the same `.md` in place. Same for the index: one entry per conversation.
4. **`PreCompact` snapshots (`.precompact-N.md`) are immutable.** Compaction is the only moment the full pre-summary conversation still exists; writing it to the canonical file would let the next turn overwrite it with the compacted version.
5. **`_index.json` is a rebuildable cache** — `--rebuild-index` reconstructs it from archived transcripts alone. Concurrent sessions on the same project are serialized with an `fcntl` lock (degrades to unlocked on non-POSIX).
6. **Atomic writes, `0600` permissions.** A hook killed by timeout must not leave truncated files, and the archive must not widen the permissions of the native transcripts.
7. **The `.jsonl` copy is never filtered.** Rendering options (`CLAUDE_TRANSCRIPT_*` env vars, see SKILL.md) only affect the Markdown.
8. **The installer touches only its own hook entries** and leaves other hooks in the settings intact.

## Notes

- The hook script is standalone at runtime; `SKILL.md` is the installer front door and operating manual, not a dependency. If autosave "isn't working", debug hook registration (`install_hooks.py --status`) and `_autosave.log`, not the skill.
- `Stop` does not fire on ESC-interrupted turns — documented Claude Code behavior, not a bug; `SessionEnd` covers that gap.
- Behavior is configured through `CLAUDE_TRANSCRIPT_*` environment variables (archive dir, thinking blocks, size caps…); the full table lives in `SKILL.md`.
