"""Per-project conversation index — the memory layer over the archive.

The archive alone is a pile of Markdown: to *recall* something you first have to
know which of 200 files to open. This module maintains, in each project folder:

    _index.json   one entry per session, machine-readable, the source of truth
    INDEX.md      the same entries rendered for reading and grepping

and a root INDEX.md listing the projects. The split matters: rebuilding the
index by re-parsing every archived transcript on every turn would cost seconds,
so each save updates only its own entry in the JSON and re-renders from there.

What goes into an entry is chosen for *retrieval*, not for reporting. Titles and
dates alone answer almost nothing — the questions people actually ask are "what
did we decide about auth", "which session touched the migration", "how did we
fix that bug last week". So each entry carries the opening request verbatim and
the files the session modified, which is what those questions key on.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from collections import Counter

import transcript_lib as lib

INDEX_JSON = "_index.json"
INDEX_MD = "INDEX.md"

# Tools whose file_path means "this session changed that file" — the strongest
# retrieval key a coding conversation leaves behind.
WRITE_TOOLS = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit"})
READ_TOOLS = frozenset({"Read"})

# Plenty of real work happens through the shell — heredocs, sed, cat — and leaves
# no file_path anywhere. Without this, "which session touched that file" silently
# misses those sessions. Conservative on purpose: a token has to start with a word
# character and end in a known source extension, so `*.ts` in a grep pattern and
# bare flags never match, and `./install.sh` is not mistaken for `/install.sh`.
BASH_PATH = re.compile(
    r"(?<![\w.*?])/?\b[\w][\w./-]*\.(?:ts|tsx|js|jsx|mjs|cjs|py|rb|go|rs|java|kt|php|sql|prisma"
    r"|md|json|ya?ml|toml|sh|css|scss|html|svelte|vue)\b")
BASH_PATH_LIMIT = 6


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------

def _relativize(path, cwd):
    if cwd and path.startswith(cwd.rstrip("/") + "/"):
        return path[len(cwd.rstrip("/")) + 1:]
    return path


def files_touched(records, cwd=""):
    """Return (modified, read) file lists, most-referenced first.

    Read files are kept as a fallback because plenty of valuable sessions change
    nothing at all — an investigation that ended in a decision is exactly the
    kind of thing worth recalling, and its only fingerprint is what it looked at.
    """
    edited, read = Counter(), Counter()
    shell = Counter()
    for rec in records:
        if rec.get("type") != "assistant":
            continue
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            inp = block.get("input")
            if not isinstance(inp, dict):
                continue
            if block.get("name") == "Bash" and isinstance(inp.get("command"), str):
                for hit in BASH_PATH.findall(inp["command"]):
                    shell[_relativize(hit, cwd)] += 1
            path = inp.get("file_path")
            if not isinstance(path, str) or not path:
                continue
            name = block.get("name")
            if name in WRITE_TOOLS:
                edited[_relativize(path, cwd)] += 1
            elif name in READ_TOOLS:
                read[_relativize(path, cwd)] += 1

    def ordered(counter):
        return [name for name, _ in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))]

    edited_list, read_list = ordered(edited), ordered(read)
    # Shell-derived paths are the weakest signal, so they only fill the tail and
    # never displace a file a real tool call reported.
    known = set(edited_list) | set(read_list)
    for name in ordered(shell)[:BASH_PATH_LIMIT]:
        if name not in known:
            read_list.append(name)
    return edited_list, read_list


_COMMAND_TAG = re.compile(r"<command-(name|message|args)>(.*?)</command-\1>", re.S)
_BARE_COMMAND = re.compile(r"/[\w:.-]+")


def _unwrap_command(text):
    """Turn a slash-command invocation back into what the user typed.

    Claude Code expands `/foo bar` into `<command-name>/foo</command-name>
    <command-args>bar</command-args>` plus the whole skill body. Indexing that raw
    would fill the memory with boilerplate instead of the request.
    """
    tags = {m.group(1): m.group(2).strip() for m in _COMMAND_TAG.finditer(text)}
    rest = _COMMAND_TAG.sub(" ", text).strip() if tags else text
    if "ARGUMENTS:" in rest:
        rest = rest.split("ARGUMENTS:", 1)[-1].strip()
    if not tags:
        return rest
    name, args = tags.get("name", ""), tags.get("args", "")
    if args:
        return f"{name} {args}".strip()
    return rest or name


def first_prompt(records, limit=300):
    """The opening request, flattened to one line.

    This is the single most useful field in the index: it is how the user
    themselves described what they wanted, in their own words, before the
    conversation drifted.
    """
    bare = ""
    for rec in records:
        if rec.get("type") != "user" or rec.get("isMeta"):
            continue
        text = lib._visible_user_text(rec)
        if not text:
            continue
        flat = _unwrap_command(" ".join(text.split()))
        if not flat:
            continue
        if _BARE_COMMAND.fullmatch(flat):
            # `/clear` alone says nothing about the conversation; remember it only
            # in case the session contains nothing else.
            bare = bare or flat
            continue
        return flat[:limit] + ("…" if len(flat) > limit else "")
    return bare


def build_entry(records, meta, md_name, jsonl_name, snapshot=""):
    stamps = [lib.parse_ts(r.get("timestamp")) for r in records]
    stamps = [s for s in stamps if s]
    started, ended = (min(stamps), max(stamps)) if stamps else (None, None)
    duration = int((ended - started).total_seconds() // 60) if started and ended else 0
    edited, read = files_touched(records, meta.cwd)
    return {
        "session_id": meta.session_id,
        "title": meta.title,
        "started": started.astimezone().isoformat(timespec="seconds") if started else "",
        "ended": ended.astimezone().isoformat(timespec="seconds") if ended else "",
        "duration_min": duration,
        "user_turns": meta.user_turns,
        "assistant_turns": meta.assistant_turns,
        "tool_calls": meta.tool_calls,
        "input_tokens": meta.input_tokens,
        "output_tokens": meta.output_tokens,
        "branch": meta.git_branch,
        "models": meta.models,
        "cwd": meta.cwd,
        "first_prompt": first_prompt(records),
        "files_edited": edited,
        "files_read": read,
        "md": md_name,
        "jsonl": jsonl_name,
        "snapshots": [snapshot] if snapshot else [],
    }


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

def load_index(directory):
    path = os.path.join(directory, INDEX_JSON)
    if not os.path.isfile(path):
        return {"project": os.path.basename(directory), "sessions": []}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (ValueError, OSError):
        # A corrupted index is recoverable (`--rebuild-index`); refusing to save
        # the transcript because of it would not be.
        return {"project": os.path.basename(directory), "sessions": []}
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
        return {"project": os.path.basename(directory), "sessions": []}
    return data


def upsert(index, entry, canonical_exists=True):
    """Merge an entry, keyed by session id.

    A session is archived repeatedly — once per turn — so this replaces rather
    than appends. PreCompact snapshots are the exception: they accumulate under
    the session they belong to instead of becoming separate rows, because they
    are moments of one conversation, not conversations of their own.
    """
    sessions = index.setdefault("sessions", [])
    for i, existing in enumerate(sessions):
        if existing.get("session_id") == entry["session_id"]:
            merged = dict(existing)
            merged.update(entry)
            snaps = list(existing.get("snapshots") or [])
            for snap in entry.get("snapshots") or []:
                if snap not in snaps:
                    snaps.append(snap)
            merged["snapshots"] = snaps
            if not canonical_exists and existing.get("md"):
                merged["md"] = existing["md"]
            sessions[i] = merged
            return merged
    sessions.append(entry)
    return entry


def sorted_sessions(index):
    return sorted(index.get("sessions", []),
                  key=lambda s: s.get("started") or "", reverse=True)


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def _plural(n, singular, plural):
    return f"{n} {singular if n == 1 else plural}"


def _fmt_day(iso):
    dt = lib.parse_ts(iso)
    return f"{dt:%Y-%m-%d %H:%M}" if dt else "?"


def _fmt_files(paths, limit=5):
    if not paths:
        return ""
    shown = ", ".join(f"`{p}`" for p in paths[:limit])
    extra = len(paths) - limit
    return shown + (f" (+{extra})" if extra > 0 else "")


def render_index_md(index, updated=None):
    sessions = sorted_sessions(index)
    project = index.get("project", "?")
    updated = updated or datetime.datetime.now().astimezone()

    span = ""
    if sessions:
        first = _fmt_day(sessions[-1].get("started"))[:10]
        last = _fmt_day(sessions[0].get("started"))[:10]
        span = f" · dal {first} al {last}" if first != last else f" · {first}"

    out = [
        f"# Indice sessioni — {project}",
        f"{_plural(len(sessions), 'sessione', 'sessioni')}{span} · "
        f"aggiornato {updated:%Y-%m-%d %H:%M}",
        "Ogni voce è una conversazione archiviata in questa cartella. Cerca qui il "
        "tema (`grep -i auth INDEX.md`), poi apri il file `.md` indicato per "
        "rileggerla per intero. `_index.json` è la stessa informazione in forma "
        "machine-readable.",
    ]

    for s in sessions:
        head = f"## {_fmt_day(s.get('started'))} — {s.get('title') or 'senza titolo'}"
        facts = [f"`{s.get('md', '?')}`"]
        if s.get("user_turns"):
            facts.append(f"{s['user_turns']} turni")
        if s.get("tool_calls"):
            facts.append(f"{s['tool_calls']} tool")
        if s.get("duration_min"):
            facts.append(f"{s['duration_min']} min")
        if s.get("branch"):
            facts.append(f"branch `{s['branch']}`")
        lines = [head, "- " + " · ".join(facts)]
        if s.get("first_prompt"):
            lines.append(f"- chiesto: {s['first_prompt']}")
        if s.get("files_edited"):
            lines.append(f"- modificati: {_fmt_files(s['files_edited'])}")
        elif s.get("files_read"):
            lines.append(f"- consultati: {_fmt_files(s['files_read'], 4)}")
        if s.get("snapshots"):
            lines.append(f"- snapshot pre-compattazione: {_fmt_files(s['snapshots'])}")
        out.append("\n".join(lines))

    return "\n\n".join(out).rstrip() + "\n"


def render_root_index(projects, updated=None):
    """A map of the archive: which projects exist and when they were last active.

    Without it, recalling something means guessing the folder name first.
    """
    updated = updated or datetime.datetime.now().astimezone()
    total = sum(p["count"] for p in projects)
    out = [
        "# Archivio sessioni Claude Code",
        f"{_plural(total, 'conversazione', 'conversazioni')} in "
        f"{_plural(len(projects), 'progetto', 'progetti')} · aggiornato "
        f"{updated:%Y-%m-%d %H:%M}",
        "Ogni progetto ha il suo `INDEX.md` con una voce per conversazione. "
        "Per cercare in tutto l'archivio: `grep -ri \"<tema>\" */INDEX.md`.",
        "| Progetto | Sessioni | Ultima attività | Ultimo argomento |",
        "|---|---|---|---|",
    ]
    for p in sorted(projects, key=lambda x: x.get("last") or "", reverse=True):
        title = (p.get("last_title") or "").replace("|", "\\|")[:60]
        out.append(f"| [{p['project']}]({p['project']}/{INDEX_MD}) | {p['count']} | "
                   f"{_fmt_day(p.get('last'))} | {title} |")
    return "\n\n".join(out[:3]) + "\n\n" + "\n".join(out[3:]) + "\n"


def collect_projects(root):
    projects = []
    if not os.path.isdir(root):
        return projects
    for name in sorted(os.listdir(root)):
        directory = os.path.join(root, name)
        if not os.path.isdir(directory):
            continue
        index = load_index(directory)
        sessions = sorted_sessions(index)
        if not sessions:
            continue
        projects.append({
            "project": name,
            "count": len(sessions),
            "last": sessions[0].get("started", ""),
            "last_title": sessions[0].get("title", ""),
        })
    return projects
