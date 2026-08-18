"""Parsing and Markdown rendering for Claude Code session transcripts.

This module is deliberately side-effect free so the test suite can exercise it
without spawning a hook process. The record-type map it relies on lives in
`references/transcript-jsonl-format.md` and was derived from real transcripts.

The guiding constraint: Claude Code adds new record types and new content-block
kinds between releases. A renderer that raises on something unfamiliar would
cost the user their archive, so every unknown shape is skipped or degraded
rather than treated as fatal.
"""

from __future__ import annotations

import datetime
import html
import json
import re
from dataclasses import dataclass, field

# Records that carry no conversation content — UI state, titles, file-history
# deltas, queue bookkeeping. Skipping them is what keeps the Markdown readable.
NOISE_TYPES = frozenset({
    "attachment", "last-prompt", "ai-title", "mode", "queue-operation",
    "permission-mode", "file-history-delta", "file-history-snapshot",
    "agent-name", "agent-setting", "bridge-session", "worktree-state",
    "relocated",
})

# `system` records are mostly telemetry. These subtypes never say anything a
# reader of the archive would want; anything else with real text gets rendered.
NOISE_SYSTEM_SUBTYPES = frozenset({
    "turn_duration", "stop_hook_summary", "away_summary", "bridge_status",
})


@dataclass
class RenderOptions:
    include_thinking: bool = True
    include_meta: bool = False
    max_result_chars: int = 2000
    max_input_chars: int = 1200
    max_thinking_chars: int = 6000
    # Very long messages get folded rather than cut: a 100 KB generated prompt
    # (a /security-review payload, a pasted spec) would otherwise bury the rest
    # of the conversation, but truncating the user's own words loses the part of
    # the archive that matters most.
    collapse_text_chars: int = 3000


@dataclass
class SessionMeta:
    session_id: str = ""
    title: str = ""
    project: str = ""
    cwd: str = ""
    git_branch: str = ""
    claude_version: str = ""
    models: list = field(default_factory=list)
    started: str = ""
    trigger: str = ""
    user_turns: int = 0
    assistant_turns: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

def load_records(text):
    """Parse JSONL leniently. A half-written final line is normal: the hook can
    fire while Claude Code is still flushing, so bad lines are dropped, not
    raised on."""
    records = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def parse_ts(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def first_timestamp(records):
    stamps = [parse_ts(r.get("timestamp")) for r in records]
    stamps = [s for s in stamps if s is not None]
    return min(stamps) if stamps else None


# --------------------------------------------------------------------------
# naming
# --------------------------------------------------------------------------

_SLUG_STRIP = re.compile(r"[^A-Za-z0-9._-]+")


def project_slug(cwd, home):
    """Turn a working directory into a stable archive folder name.

    Paths under $HOME become their home-relative path with separators folded to
    dashes (`~/addway/siarx` -> `addway-siarx`), which keeps two projects that
    merely share a basename in separate folders. Leading dots are stripped so
    worktree directories don't produce hidden folders.
    """
    if not cwd:
        return "unknown"
    cwd = cwd.rstrip("/")
    home = (home or "").rstrip("/")
    if home and cwd == home:
        return "home"
    if home and cwd.startswith(home + "/"):
        rel = cwd[len(home) + 1:]
    else:
        rel = cwd.lstrip("/")
    parts = [_SLUG_STRIP.sub("-", p).strip("-.") for p in rel.split("/")]
    parts = [p for p in parts if p]
    return "-".join(parts) or "root"


def session_cwd(records, fallback=""):
    """The directory the session was launched from.

    Deliberately the *first* cwd in the transcript, not the current one: a
    session that cd's into a subproject halfway through would otherwise be filed
    under two different projects, and the same conversation would appear twice in
    the memory under different names. The launch directory is also how Claude
    Code itself attributes a session to a project folder, so the archive stays
    aligned with `~/.claude/projects`.
    """
    for rec in records:
        if rec.get("cwd"):
            return rec["cwd"]
    return fallback


def output_basename(records, session_id, fallback_stem=""):
    """Build the archive filename stem.

    Derived from the session's *first* timestamp rather than the current clock,
    because the hook rewrites this file on every turn: a wall-clock name would
    litter the archive with one file per turn instead of keeping one file per
    session that stays current.
    """
    sid = (session_id or fallback_stem or "unknown").strip()
    short = sid.split("-")[0][:8] or "unknown"
    started = first_timestamp(records)
    if started is None:
        return f"nodate-{short}"
    local = started.astimezone()
    return f"{local:%Y-%m-%d_%H%M}-{short}"


# --------------------------------------------------------------------------
# metadata
# --------------------------------------------------------------------------

def _visible_user_text(rec):
    msg = rec.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p).strip()
    return ""


def session_meta(records, session_id="", trigger="", project="", home=""):
    meta = SessionMeta(session_id=session_id, trigger=trigger, project=project)
    models = []
    title = ""
    first_prompt = ""

    for rec in records:
        rtype = rec.get("type")
        if rec.get("cwd"):
            meta.cwd = rec["cwd"]
        if rec.get("gitBranch"):
            meta.git_branch = rec["gitBranch"]
        if rec.get("version"):
            meta.claude_version = rec["version"]
        if not meta.session_id and rec.get("sessionId"):
            meta.session_id = rec["sessionId"]

        if rtype == "ai-title" and rec.get("aiTitle"):
            title = rec["aiTitle"]
        elif rtype == "user":
            if rec.get("isMeta"):
                continue
            text = _visible_user_text(rec)
            if text:
                meta.user_turns += 1
                if not first_prompt:
                    first_prompt = text
        elif rtype == "assistant":
            msg = rec.get("message") or {}
            if msg.get("model") and msg["model"] not in models:
                models.append(msg["model"])
            usage = msg.get("usage") or {}
            for key in ("input_tokens", "cache_creation_input_tokens",
                        "cache_read_input_tokens"):
                if isinstance(usage.get(key), int):
                    meta.input_tokens += usage[key]
            if isinstance(usage.get("output_tokens"), int):
                meta.output_tokens += usage["output_tokens"]
            blocks = msg.get("content")
            if isinstance(blocks, list):
                # Counts any record where Claude acted — a turn spent entirely on
                # tool calls is still a turn, and reporting 0 for a session with
                # 15 tool calls reads like a bug in the archiver.
                if any(isinstance(b, dict) and b.get("type") in
                       ("text", "tool_use", "thinking") for b in blocks):
                    meta.assistant_turns += 1
                meta.tool_calls += sum(
                    1 for b in blocks
                    if isinstance(b, dict) and b.get("type") == "tool_use")

    meta.models = models
    if not meta.project:
        meta.project = project_slug(meta.cwd, home)
    started = first_timestamp(records)
    meta.started = started.astimezone().isoformat(timespec="seconds") if started else ""
    meta.title = title or _shorten_title(first_prompt) or "Sessione Claude Code"
    return meta


def _shorten_title(text, limit=70):
    if not text:
        return ""
    flat = " ".join(text.split())
    flat = re.sub(r"<[^>]+>", "", flat).strip()
    if len(flat) <= limit:
        return flat
    return flat[:limit].rsplit(" ", 1)[0] + "…"


# --------------------------------------------------------------------------
# tool results
# --------------------------------------------------------------------------

def build_tool_results(records):
    """Map tool_use_id -> {'text', 'is_error'}.

    Results arrive as the *next* user record rather than inside the assistant
    record that made the call, so they are collected up front and stitched back
    into the call site during rendering. Reading a call without its output is
    the single most common frustration with raw transcripts.
    """
    results = {}
    for rec in records:
        if rec.get("type") != "user":
            continue
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tid = block.get("tool_use_id")
            if not tid:
                continue
            results[tid] = {
                "text": _result_text(block.get("content"), rec.get("toolUseResult")),
                "is_error": bool(block.get("is_error")),
            }
    return results


def _result_text(content, fallback=None):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif block.get("type") == "image":
                parts.append("[immagine]")
            else:
                parts.append(f"[{block.get('type', 'blocco')}]")
        return "\n".join(p for p in parts if p)
    if isinstance(fallback, str):
        return fallback
    if fallback is not None:
        try:
            return json.dumps(fallback, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            return str(fallback)
    return ""


# --------------------------------------------------------------------------
# rendering helpers
# --------------------------------------------------------------------------

def fence_for(text):
    """Pick a fence longer than any backtick run inside `text`.

    Tool output routinely contains triple backticks (this project's own README
    does). A fixed ``` fence would let that output escape its block and corrupt
    every heading below it.
    """
    runs = re.findall(r"`+", text or "")
    longest = max((len(r) for r in runs), default=0)
    return "`" * max(3, longest + 1)


def code_block(text, lang=""):
    text = (text or "").rstrip()
    fence = fence_for(text)
    return f"{fence}{lang}\n{text}\n{fence}"


def clip(text, limit):
    text = text or ""
    if limit <= 0 or len(text) <= limit:
        return text
    kept = text[:limit].rstrip()
    return f"{kept}\n… [tagliato, {len(text)} caratteri in totale]"


def human_size(n):
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def details(summary, body):
    return f"<details>\n<summary>{html.escape(str(summary))}</summary>\n\n{body}\n\n</details>"


def text_chunk(text, opts):
    """Render a message body, folding it if it is very long.

    Folding preserves every character while keeping the document skimmable, which
    is the opposite trade-off from clipping tool output: tool output is
    reproducible, the words in the conversation are not.
    """
    limit = getattr(opts, "collapse_text_chars", 0)
    if not limit or len(text) <= limit:
        return text
    preview = " ".join(text.split())[:110]
    lines = text.count("\n") + 1
    return details(f"{preview}… [{lines} righe, {human_size(len(text))}]", text)


def _yaml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_yaml_value(v) for v in value) + "]"
    if value is None:
        return '""'
    # JSON strings are valid YAML scalars, which sidesteps hand-rolled quoting.
    return json.dumps(str(value), ensure_ascii=False)


def _summarize_input(name, inp, opts):
    """Return (label, lang, body) for a tool call.

    Each tool has one field that answers "what did it actually do"; surfacing
    that as the label keeps the transcript skimmable, while the bulky field goes
    in the code block underneath.
    """
    if not isinstance(inp, dict):
        return "", "", clip(str(inp), opts.max_input_chars)

    def s(key):
        val = inp.get(key)
        return val if isinstance(val, str) else ""

    if name == "Bash":
        return s("description"), "bash", clip(s("command"), opts.max_input_chars)
    if name in ("Read", "NotebookEdit"):
        label = s("file_path")
        if inp.get("offset"):
            label += f" (da riga {inp['offset']})"
        if inp.get("pages"):
            label += f" (pagine {inp['pages']})"
        return label, "", ""
    if name == "Write":
        return s("file_path"), "", clip(s("content"), opts.max_input_chars)
    if name == "Edit":
        body = f"- {clip(s('old_string'), opts.max_input_chars // 2)}\n" \
               f"+ {clip(s('new_string'), opts.max_input_chars // 2)}"
        return s("file_path"), "diff", body
    if name in ("Grep", "Glob"):
        label = s("pattern")
        if s("path"):
            label += f"  in {s('path')}"
        return label, "", ""
    if name == "Skill":
        label = s("skill")
        if s("args"):
            label += f" {s('args')}"
        return label, "", ""
    if name in ("Agent", "Task"):
        label = s("description")
        if s("subagent_type"):
            label += f" [{s('subagent_type')}]"
        return label, "", clip(s("prompt"), opts.max_input_chars)
    if name == "WebFetch":
        return s("url"), "", clip(s("prompt"), opts.max_input_chars)
    if name == "WebSearch":
        return s("query"), "", ""
    if name == "TodoWrite":
        todos = inp.get("todos")
        count = len(todos) if isinstance(todos, list) else 0
        lines = []
        if isinstance(todos, list):
            for t in todos:
                if isinstance(t, dict):
                    mark = {"completed": "x", "in_progress": "~"}.get(t.get("status"), " ")
                    lines.append(f"- [{mark}] {t.get('content', '')}")
        return f"{count} voci", "", clip("\n".join(lines), opts.max_input_chars)

    try:
        dumped = json.dumps(inp, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        dumped = str(inp)
    return "", "json", clip(dumped, opts.max_input_chars)


def render_tool_use(block, results, opts):
    name = block.get("name") or "tool"
    label, lang, body = _summarize_input(name, block.get("input"), opts)
    head = f"**→ {name}**"
    if label:
        head += f" · {label}"
    chunks = [head]
    if body.strip():
        chunks.append(code_block(body, lang))
    res = results.get(block.get("id"))
    if res and res["text"].strip():
        size = human_size(len(res["text"]))
        flag = " ⚠️ errore" if res["is_error"] else ""
        summary = f"↳ output ({size}){flag}"
        chunks.append(details(summary, code_block(clip(res["text"], opts.max_result_chars))))
    return "\n\n".join(chunks)


# --------------------------------------------------------------------------
# main renderer
# --------------------------------------------------------------------------

def render_frontmatter(meta, saved_at=None):
    saved = saved_at or datetime.datetime.now().astimezone()
    rows = [
        ("title", meta.title),
        ("session_id", meta.session_id),
        ("project", meta.project),
        ("cwd", meta.cwd),
        ("git_branch", meta.git_branch),
        ("models", meta.models),
        ("claude_version", meta.claude_version),
        ("started", meta.started),
        ("saved", saved.isoformat(timespec="seconds")),
        ("saved_on", meta.trigger),
        ("user_turns", meta.user_turns),
        ("assistant_turns", meta.assistant_turns),
        ("tool_calls", meta.tool_calls),
        ("input_tokens", meta.input_tokens),
        ("output_tokens", meta.output_tokens),
    ]
    lines = ["---"]
    lines += [f"{k}: {_yaml_value(v)}" for k, v in rows]
    lines.append("---")
    return "\n".join(lines)


def render_markdown(records, meta, opts=None, saved_at=None):
    opts = opts or RenderOptions()
    results = build_tool_results(records)
    out = [render_frontmatter(meta, saved_at), f"# {meta.title}"]

    subtitle = [f"`{meta.session_id[:8]}`" if meta.session_id else ""]
    if meta.cwd:
        subtitle.append(meta.cwd)
    subtitle.append(f"{meta.user_turns} turni utente")
    subtitle.append(f"{meta.tool_calls} tool call")
    out.append(" · ".join(p for p in subtitle if p))

    speaker = None
    for rec in records:
        rtype = rec.get("type")
        if rtype in NOISE_TYPES:
            continue

        if rtype == "user":
            if rec.get("isMeta") and not opts.include_meta:
                continue
            chunks = _render_user(rec, opts)
            if not chunks:
                continue
            if speaker != "user":
                out.append(_heading("👤 Utente", rec))
                speaker = "user"
            out.extend(chunks)

        elif rtype == "assistant":
            chunks = _render_assistant(rec, results, opts)
            if not chunks:
                continue
            if speaker != "assistant":
                out.append(_heading("🤖 Claude", rec))
                speaker = "assistant"
            out.extend(chunks)

        elif rtype == "system":
            text = rec.get("content")
            subtype = rec.get("subtype") or ""
            if subtype in NOISE_SYSTEM_SUBTYPES or not isinstance(text, str) or not text.strip():
                continue
            out.append(f"> ⚙️ **{subtype or 'system'}** — {' '.join(text.split())[:500]}")
            speaker = None

    return "\n\n".join(out).rstrip() + "\n"


def _heading(who, rec):
    dt = parse_ts(rec.get("timestamp"))
    when = f" — {dt.astimezone():%H:%M}" if dt else ""
    return f"## {who}{when}"


def _render_user(rec, opts):
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return [text_chunk(content.strip(), opts)] if content.strip() else []
    if not isinstance(content, list):
        return []
    chunks = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = (block.get("text") or "").strip()
            if text:
                chunks.append(text_chunk(text, opts))
        elif btype == "image":
            chunks.append("_[immagine allegata]_")
        elif btype == "document":
            chunks.append("_[documento allegato]_")
        # tool_result blocks are rendered at their call site instead.
    return chunks


def _render_assistant(rec, results, opts):
    content = (rec.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    chunks = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = (block.get("text") or "").strip()
            if text:
                chunks.append(text_chunk(text, opts))
        elif btype == "thinking":
            if not opts.include_thinking:
                continue
            text = (block.get("thinking") or "").strip()
            if not text:
                continue
            words = len(text.split())
            chunks.append(details(
                f"💭 Ragionamento ({words} parole)",
                clip(text, opts.max_thinking_chars)))
        elif btype == "tool_use":
            chunks.append(render_tool_use(block, results, opts))
    return chunks
