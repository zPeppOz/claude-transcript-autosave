#!/usr/bin/env python3
"""Register (or remove) the transcript autosave hooks in a Claude Code settings file.

Editing `settings.json` by hand is the usual way to wire a hook and also the
usual way to lose an unrelated one. This script exists so the operation is
idempotent and reversible: it rewrites only the entries it owns (identified by
the script path), leaves every other hook untouched, and takes a timestamped
backup before writing.

    python3 install_hooks.py --status
    python3 install_hooks.py --dry-run
    python3 install_hooks.py
    python3 install_hooks.py --uninstall
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK_SCRIPT = os.path.join(HERE, "save_transcript.py")
MARKER = "save_transcript.py"
LOG_NAME = "_autosave.log"
INDEX_MD = "INDEX.md"
DEFAULT_EVENTS = ("Stop", "SessionEnd", "PreCompact")
ALL_EVENTS = ("Stop", "SessionEnd", "PreCompact", "SubagentStop")
TIMEOUT_SECONDS = 15
STATUS_MESSAGE = "Saving transcript"
DEFAULT_ROOT = "~/.claude/session-archive"


def hook_command(script):
    """Guard the call with a file test.

    If the repo is moved, renamed or deleted, an unguarded command would fail on
    every single turn for the rest of the user's life with Claude Code. With the
    guard it degrades to a silent no-op, which is the right failure mode for a
    background convenience.
    """
    return f'[ ! -f "{script}" ] || python3 "{script}"'


def hook_entry(script):
    return {
        "type": "command",
        "command": hook_command(script),
        "timeout": TIMEOUT_SECONDS,
        "statusMessage": STATUS_MESSAGE,
    }


def is_ours(entry):
    return isinstance(entry, dict) and MARKER in str(entry.get("command", ""))


# --------------------------------------------------------------------------
# settings io
# --------------------------------------------------------------------------

def load_settings(path):
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        text = fh.read().strip()
    if not text:
        return {}
    return json.loads(text)


def save_settings(path, settings):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    backup = ""
    if os.path.isfile(path):
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = f"{path}.bak-{stamp}"
        with open(path, encoding="utf-8") as src, open(backup, "w", encoding="utf-8") as dst:
            dst.write(src.read())
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    return backup


# --------------------------------------------------------------------------
# mutation
# --------------------------------------------------------------------------

def strip_ours(settings):
    """Remove every entry this script owns, pruning containers left empty."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    removed = 0
    for event in list(hooks.keys()):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                kept_groups.append(group)
                continue
            entries = group["hooks"]
            kept = [e for e in entries if not is_ours(e)]
            removed += len(entries) - len(kept)
            if kept or not entries:
                group["hooks"] = kept
                kept_groups.append(group)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    return removed


def install(settings, events, script):
    """Replace our entries with fresh ones. Called after strip_ours so a moved
    repo or a changed event list converges instead of accumulating duplicates."""
    strip_ours(settings)
    hooks = settings.setdefault("hooks", {})
    for event in events:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            groups = []
            hooks[event] = groups
        # No matcher: Stop/SessionEnd/PreCompact are not tool-scoped events.
        groups.append({"hooks": [hook_entry(script)]})
    return settings


def registered_events(settings):
    out = []
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return out
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                if any(is_ours(e) for e in group["hooks"]):
                    out.append(event)
                    break
    return sorted(out)


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

def foreign_files(root):
    """Count top-level regular files in the archive root that we did not write.

    The archive owns its root: one subdirectory per project plus `_autosave.log`.
    Loose files at the top level mean the root belongs to something else — which
    is exactly what `~/.claude/transcripts` turned out to be, a directory Claude
    Code already fills with its own `ses_*.jsonl`. Sharing a folder with a tool
    that prunes it is how an archive quietly disappears, so this is worth saying
    out loud before the first write rather than discovering it later.
    """
    if not os.path.isdir(root):
        return 0
    return sum(1 for name in os.listdir(root)
               if name != os.path.basename(LOG_NAME)
               and not name.endswith((".md", ".jsonl.tmp"))
               and os.path.isfile(os.path.join(root, name)))


def warn_if_root_is_foreign(root):
    count = foreign_files(root)
    if count:
        print(f"attenzione: {root} contiene già {count} file non prodotti da questa "
              f"skill. Non è una cartella nostra: imposta CLAUDE_TRANSCRIPT_DIR su un "
              f"percorso dedicato per non mescolare l'archivio con file altrui.",
              file=sys.stderr)


def candidate_paths(home, project_dir):
    return [
        ("globale", os.path.join(home, ".claude", "settings.json")),
        ("globale (local)", os.path.join(home, ".claude", "settings.local.json")),
        ("progetto", os.path.join(project_dir, ".claude", "settings.json")),
        ("progetto (local)", os.path.join(project_dir, ".claude", "settings.local.json")),
    ]


def cmd_status(home, project_dir, root):
    print(f"script hook : {HOOK_SCRIPT}")
    print(f"              {'presente' if os.path.isfile(HOOK_SCRIPT) else 'MANCANTE'}")
    print(f"archivio    : {root}")
    warn_if_root_is_foreign(root)
    if os.path.isdir(root):
        # INDEX.md files live alongside the transcripts; counting them would
        # inflate the total by one per project plus the root map.
        sessions = sum(1 for _d, _s, files in os.walk(root)
                       for f in files if f.endswith(".md") and f != INDEX_MD)
        print(f"              {sessions} conversazioni archiviate")
    else:
        print("              (ancora vuoto)")
    print()
    print("registrazione hook:")
    any_found = False
    for label, path in candidate_paths(home, project_dir):
        if not os.path.isfile(path):
            continue
        try:
            events = registered_events(load_settings(path))
        except ValueError as exc:
            print(f"  {label:16} {path}\n  {'':16} JSON non valido: {exc}")
            continue
        if events:
            any_found = True
            print(f"  {label:16} {', '.join(events)}  ({path})")
    if not any_found:
        print("  nessun hook registrato — esegui install_hooks.py per attivarlo")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scope", choices=("global", "project"), default="global",
                        help="settings da modificare (default: global = ~/.claude/settings.json)")
    parser.add_argument("--settings", help="percorso esplicito a un file settings.json")
    parser.add_argument("--events", default=",".join(DEFAULT_EVENTS),
                        help=f"eventi separati da virgola (disponibili: {', '.join(ALL_EVENTS)})")
    parser.add_argument("--uninstall", action="store_true", help="rimuove gli hook di questa skill")
    parser.add_argument("--status", action="store_true", help="mostra cosa è registrato e dove")
    parser.add_argument("--dry-run", action="store_true", help="mostra il risultato senza scrivere")
    args = parser.parse_args(argv)

    home = os.path.expanduser("~")
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    root = os.path.expanduser(os.environ.get("CLAUDE_TRANSCRIPT_DIR") or DEFAULT_ROOT)

    if args.status:
        return cmd_status(home, project_dir, root)

    if args.settings:
        path = os.path.abspath(os.path.expanduser(args.settings))
    elif args.scope == "global":
        path = os.path.join(home, ".claude", "settings.json")
    else:
        path = os.path.join(project_dir, ".claude", "settings.json")

    try:
        settings = load_settings(path)
    except ValueError as exc:
        print(f"errore: {path} non è JSON valido ({exc}).", file=sys.stderr)
        print("Correggilo a mano prima di procedere: non lo sovrascrivo per non "
              "perdere configurazione.", file=sys.stderr)
        return 1

    if args.uninstall:
        removed = strip_ours(settings)
        if not removed:
            print(f"nessun hook di questa skill in {path}")
            return 0
        if args.dry_run:
            print(f"[dry-run] rimuoverei {removed} hook da {path}")
            return 0
        backup = save_settings(path, settings)
        print(f"rimossi {removed} hook da {path}")
        if backup:
            print(f"backup: {backup}")
        return 0

    events = [e.strip() for e in args.events.split(",") if e.strip()]
    unknown = [e for e in events if e not in ALL_EVENTS]
    if unknown:
        print(f"errore: eventi non riconosciuti: {', '.join(unknown)}", file=sys.stderr)
        return 1

    # Registering the same hook in both settings.json and settings.local.json
    # makes it run twice per turn: harmless output, wasted work, confusing logs.
    for label, other in candidate_paths(home, project_dir):
        if os.path.abspath(other) == os.path.abspath(path) or not os.path.isfile(other):
            continue
        try:
            dupes = registered_events(load_settings(other))
        except ValueError:
            continue
        if dupes:
            print(f"attenzione: già registrato in {other} ({', '.join(dupes)}). "
                  f"Verrebbe eseguito due volte per turno — rimuovilo con "
                  f"--settings {other} --uninstall", file=sys.stderr)

    warn_if_root_is_foreign(root)
    install(settings, events, HOOK_SCRIPT)

    if args.dry_run:
        print(f"[dry-run] {path} diventerebbe:\n")
        print(json.dumps({"hooks": settings.get("hooks", {})}, indent=2, ensure_ascii=False))
        return 0

    backup = save_settings(path, settings)
    print(f"hook registrati in {path}: {', '.join(events)}")
    if backup:
        print(f"backup: {backup}")
    print(f"archivio: {root}")
    print("Le modifiche ai settings vengono rilette a sessione in corso: "
          "il prossimo fine turno salva già.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
