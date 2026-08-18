#!/usr/bin/env python3
"""Claude Code hook: archive the session transcript on every agent turn.

Wired to Stop, SessionEnd and PreCompact by `install_hooks.py`. Reads the hook
event from stdin and writes two files per session into the archive:

    <root>/<project>/<date>_<time>-<sid8>.md      readable Markdown
    <root>/<project>/<date>_<time>-<sid8>.jsonl   faithful copy of the source

The default root is `~/.claude/session-archive`, deliberately *not*
`~/.claude/transcripts`: that name is already occupied by Claude Code's own
storage, and sharing a directory with a tool that prunes it is how an archive
quietly disappears.

The Markdown is rewritten in place on each turn, so the archive always holds the
current state of the session rather than one file per turn.

Two contracts this script must never break:

  1. It always exits 0 and never writes to stdout. A hook that fails loudly, or
     that prints stray text where Claude Code expects JSON, turns a background
     convenience into something that interferes with every turn. Failures go to
     `<root>/_autosave.log` where they can be read after the fact.

  2. PreCompact snapshots are immutable (`.precompact-1.md`). Compaction is the
     one moment the full pre-summary conversation still exists; overwriting the
     canonical file would let the next turn replace it with the already-compacted
     version, destroying the very thing the snapshot was taken for.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import transcript_lib as lib  # noqa: E402

DEFAULT_ROOT = "~/.claude/session-archive"
LOG_NAME = "_autosave.log"
LOG_MAX_BYTES = 512 * 1024
LOG_KEEP_LINES = 200
FALSEY = {"0", "false", "off", "no"}


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def env_flag(env, name, default=True):
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in FALSEY


def env_int(env, name, default):
    try:
        return int(str(env.get(name, "")).strip())
    except (TypeError, ValueError):
        return default


def resolve_root(env):
    return os.path.expanduser(env.get("CLAUDE_TRANSCRIPT_DIR") or DEFAULT_ROOT)


def render_options(env):
    return lib.RenderOptions(
        include_thinking=env_flag(env, "CLAUDE_TRANSCRIPT_THINKING", True),
        include_meta=env_flag(env, "CLAUDE_TRANSCRIPT_META", False),
        max_result_chars=env_int(env, "CLAUDE_TRANSCRIPT_MAX_RESULT", 2000),
    )


# --------------------------------------------------------------------------
# io helpers
# --------------------------------------------------------------------------

def write_private(path, data):
    """Write atomically, then tighten permissions.

    Atomic because a hook can be killed by its timeout mid-write and a truncated
    archive is worse than a stale one. 0600 because transcripts routinely contain
    secrets, tokens and private source pasted into the conversation, and the
    source `.jsonl` files Claude Code writes are 0600 too — the archive should
    not silently widen that.
    """
    tmp = f"{path}.tmp{os.getpid()}"
    mode = "wb" if isinstance(data, bytes) else "w"
    kwargs = {} if isinstance(data, bytes) else {"encoding": "utf-8"}
    with open(tmp, mode, **kwargs) as fh:
        fh.write(data)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def private_dir(path):
    """Create a directory only the owner can enter.

    The files inside are 0600, but a world-readable directory still leaks the
    project names and session times to anyone on the box — on a shared dev
    machine that is a list of what the user is working on.
    """
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def log(root, message):
    path = os.path.join(root, LOG_NAME)
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        private_dir(root)
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            with open(path, encoding="utf-8", errors="replace") as fh:
                tail = fh.readlines()[-LOG_KEEP_LINES:]
            with open(path, "w", encoding="utf-8") as fh:
                fh.writelines(tail)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp}  {message}\n")
        os.chmod(path, 0o600)
    except OSError:
        pass


def next_snapshot_path(directory, base, suffix):
    """Find a free `<base>.<suffix>-<n>` slot so snapshots never overwrite."""
    for index in range(1, 1000):
        candidate = os.path.join(directory, f"{base}.{suffix}-{index}")
        if not os.path.exists(candidate + ".md") and not os.path.exists(candidate + ".jsonl"):
            return candidate
    return os.path.join(directory, f"{base}.{suffix}-overflow")


# --------------------------------------------------------------------------
# core
# --------------------------------------------------------------------------

def archive(transcript_path, event, env=None, home=None):
    """Archive one transcript. Returns a dict describing what was written."""
    env = env if env is not None else os.environ
    home = home or os.path.expanduser("~")
    root = resolve_root(env)
    started = time.time()

    transcript_path = os.path.expanduser(transcript_path or "")
    if not transcript_path or not os.path.isfile(transcript_path):
        return {"skipped": "transcript non trovato", "path": transcript_path}

    raw_bytes = os.path.getsize(transcript_path)
    max_bytes = env_int(env, "CLAUDE_TRANSCRIPT_MAX_MB", 25) * 1024 * 1024

    with open(transcript_path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    records = lib.load_records(text)
    if not records:
        return {"skipped": "transcript vuoto", "path": transcript_path}

    session_id = event.get("session_id") or ""
    hook_event = event.get("hook_event_name") or "manual"
    cwd = event.get("cwd") or ""
    if not cwd:
        for rec in reversed(records):
            if rec.get("cwd"):
                cwd = rec["cwd"]
                break

    slug = lib.project_slug(cwd, home)
    directory = os.path.join(root, slug)
    private_dir(root)
    private_dir(directory)

    stem = os.path.splitext(os.path.basename(transcript_path))[0]
    base = lib.output_basename(records, session_id, fallback_stem=stem)

    if hook_event == "PreCompact":
        target = next_snapshot_path(directory, base, "precompact")
    else:
        target = os.path.join(directory, base)

    md_path = target + ".md"
    jsonl_path = target + ".jsonl"

    write_private(jsonl_path, text)

    if raw_bytes > max_bytes:
        # The raw copy is the part that must not be lost; rendering a very large
        # transcript could exceed the hook timeout, so it is skipped rather than
        # risking a killed process that leaves nothing behind.
        return {
            "written": [jsonl_path],
            "skipped_render": f"{lib.human_size(raw_bytes)} > limite",
            "records": len(records),
            "ms": int((time.time() - started) * 1000),
        }

    meta = lib.session_meta(records, session_id=session_id, trigger=hook_event,
                            project=slug, home=home)
    markdown = lib.render_markdown(records, meta, render_options(env))
    write_private(md_path, markdown)

    return {
        "written": [md_path, jsonl_path],
        "records": len(records),
        "bytes": len(markdown),
        "title": meta.title,
        "ms": int((time.time() - started) * 1000),
    }


def read_event(argv, stdin):
    """Build the hook event, from stdin JSON or from --transcript for manual runs."""
    if "--transcript" in argv:
        path = argv[argv.index("--transcript") + 1]
        return {"hook_event_name": "manual", "transcript_path": path}
    raw = "" if stdin.isatty() else stdin.read()
    if not raw.strip():
        return None
    event = json.loads(raw)
    return event if isinstance(event, dict) else None


def backfill(env, home, limit=0, verbose=False):
    """Archive every transcript Claude Code has on disk.

    Useful once, right after installing: the hook only ever sees sessions that
    run from now on, and the existing history is usually the part worth keeping.
    """
    projects = os.path.join(home, ".claude", "projects")
    found = []
    for dirpath, _dirnames, filenames in os.walk(projects):
        for name in sorted(filenames):
            if name.endswith(".jsonl"):
                found.append(os.path.join(dirpath, name))
    found.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    if limit:
        found = found[:limit]
    done = 0
    for path in found:
        result = archive(path, {"hook_event_name": "backfill"}, env=env, home=home)
        if result.get("written"):
            done += 1
        if verbose:
            print(f"{'ok ' if result.get('written') else 'skip'} {path}", file=sys.stderr)
    return done, len(found)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    env = os.environ
    home = os.path.expanduser("~")
    root = resolve_root(env)
    verbose = "--verbose" in argv

    if not env_flag(env, "CLAUDE_TRANSCRIPT_AUTOSAVE", True):
        if verbose:
            print("autosave disabilitato (CLAUDE_TRANSCRIPT_AUTOSAVE)", file=sys.stderr)
        return 0

    if "--backfill" in argv:
        limit = 0
        if "--limit" in argv:
            try:
                limit = int(argv[argv.index("--limit") + 1])
            except (IndexError, ValueError):
                limit = 0
        done, total = backfill(env, home, limit=limit, verbose=True)
        print(f"archiviati {done}/{total} transcript in {root}")
        return 0

    try:
        event = read_event(argv, sys.stdin)
    except (ValueError, IndexError) as exc:
        log(root, f"ERRORE payload illeggibile: {exc}")
        return 0

    if not event:
        log(root, "SKIP nessun payload su stdin")
        return 0

    try:
        result = archive(event.get("transcript_path"), event, env=env, home=home)
    except Exception as exc:  # noqa: BLE001 - a hook must not surface a traceback
        import traceback
        log(root, f"ERRORE {type(exc).__name__}: {exc}")
        log(root, "  " + " | ".join(traceback.format_exc().splitlines()[-3:]))
        return 0

    sid = (event.get("session_id") or "?")[:8]
    hook_event = event.get("hook_event_name") or "manual"
    if result.get("written"):
        note = result.get("skipped_render")
        log(root, f"OK {hook_event} {sid} {result['records']} record "
                  f"{result.get('ms', 0)}ms -> {os.path.basename(result['written'][0])}"
                  + (f" (render saltato: {note})" if note else ""))
    else:
        log(root, f"SKIP {hook_event} {sid}: {result.get('skipped', 'motivo ignoto')}")

    if verbose:
        print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - last-resort guard, never break the turn
        sys.exit(0)
