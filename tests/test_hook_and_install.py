"""End-to-end tests for the hook adapter and the settings installer.

These run against temporary directories and temporary settings files only: the
installer edits real Claude Code configuration, so its merge and prune logic
must be proven somewhere that cannot damage the user's setup.
"""

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import install_hooks
import save_transcript
import session_index


def setUpModule():
    """Pin the timezone to UTC.

    Archive filenames deliberately use *local* time so they read naturally to
    whoever browses the folder. That makes the expected names below dependent on
    the machine's timezone unless it is pinned here.
    """
    if hasattr(time, "tzset"):
        os.environ["TZ"] = "UTC"
        time.tzset()


def read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()

def transcript_text(cwd):
    """A two-record session. `cwd` is injected so the fixture sits under the
    test's fake $HOME, which is what makes the archive slug come out as `proj`
    the same way a real project under the user's home does."""
    return "\n".join([
        json.dumps({"type": "user", "timestamp": "2026-08-18T09:00:00.000Z",
                    "cwd": cwd, "sessionId": "dead1234-beef",
                    "message": {"role": "user", "content": "prima domanda"}}),
        json.dumps({"type": "assistant", "timestamp": "2026-08-18T09:00:10.000Z",
                    "cwd": cwd, "sessionId": "dead1234-beef",
                    "message": {"role": "assistant", "model": "claude-opus-5",
                                "content": [{"type": "text", "text": "prima risposta"}]}}),
    ])


class HookTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        self.root = os.path.join(self.tmp.name, "archive")
        os.makedirs(self.home)
        self.cwd = os.path.join(self.home, "proj")
        os.makedirs(self.cwd)
        self.text = transcript_text(self.cwd)
        self.transcript = os.path.join(self.tmp.name, "dead1234-beef.jsonl")
        with open(self.transcript, "w", encoding="utf-8") as fh:
            fh.write(self.text + "\n")
        self.env = {"CLAUDE_TRANSCRIPT_DIR": self.root, "PATH": os.environ.get("PATH", "")}

    def tearDown(self):
        self.tmp.cleanup()

    def event(self, name="Stop"):
        return {"hook_event_name": name, "session_id": "dead1234-beef",
                "transcript_path": self.transcript, "cwd": self.cwd}

    def files(self, sub="proj"):
        path = os.path.join(self.root, sub)
        return sorted(os.listdir(path)) if os.path.isdir(path) else []

    def transcripts(self, sub="proj"):
        """Archived conversations only — the index machinery lives alongside them."""
        return [f for f in self.files(sub)
                if f.endswith((".md", ".jsonl")) and f != session_index.INDEX_MD]


class TestArchive(HookTestCase):
    def test_writes_markdown_and_raw_copy(self):
        result = save_transcript.archive(self.transcript, self.event(),
                                        env=self.env, home=self.home)
        self.assertEqual(len(result["written"]), 2)
        self.assertEqual(self.transcripts(), ["2026-08-18_0900-dead1234.jsonl",
                                              "2026-08-18_0900-dead1234.md"])
        md = read_text(result["written"][0])
        self.assertIn("prima domanda", md)
        self.assertIn("prima risposta", md)
        raw = read_text(result["written"][1])
        self.assertEqual(raw.strip(), self.text.strip())

    def test_files_are_owner_only(self):
        """Transcripts carry secrets; the source .jsonl is 0600 and the archive
        must not silently widen that."""
        result = save_transcript.archive(self.transcript, self.event(),
                                        env=self.env, home=self.home)
        for path in result["written"]:
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600, path)

    def test_repeated_turns_rewrite_one_file(self):
        for _ in range(3):
            save_transcript.archive(self.transcript, self.event(), env=self.env, home=self.home)
        self.assertEqual(len(self.transcripts()), 2)

    def test_growing_transcript_updates_the_same_file(self):
        save_transcript.archive(self.transcript, self.event(), env=self.env, home=self.home)
        with open(self.transcript, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "timestamp": "2026-08-18T09:05:00.000Z",
                                 "cwd": self.cwd, "sessionId": "dead1234-beef",
                                 "message": {"role": "user", "content": "seconda domanda"}}) + "\n")
        result = save_transcript.archive(self.transcript, self.event(), env=self.env, home=self.home)
        self.assertEqual(len(self.transcripts()), 2)
        self.assertIn("seconda domanda", read_text(result["written"][0]))

    def test_precompact_snapshots_are_immutable(self):
        """The whole point of the PreCompact snapshot is that the next turn — which
        sees an already-compacted transcript — cannot overwrite it."""
        save_transcript.archive(self.transcript, self.event("PreCompact"),
                                env=self.env, home=self.home)
        save_transcript.archive(self.transcript, self.event("PreCompact"),
                                env=self.env, home=self.home)
        names = self.transcripts()
        self.assertIn("2026-08-18_0900-dead1234.precompact-1.md", names)
        self.assertIn("2026-08-18_0900-dead1234.precompact-2.md", names)
        self.assertNotIn("2026-08-18_0900-dead1234.md", names)

    def test_session_end_updates_the_canonical_file(self):
        save_transcript.archive(self.transcript, self.event("Stop"), env=self.env, home=self.home)
        save_transcript.archive(self.transcript, self.event("SessionEnd"), env=self.env, home=self.home)
        self.assertEqual(len(self.transcripts()), 2)
        md = read_text(os.path.join(self.root, "proj", "2026-08-18_0900-dead1234.md"))
        self.assertIn('saved_on: "SessionEnd"', md)

    def test_missing_transcript_is_skipped_quietly(self):
        result = save_transcript.archive("/nope/missing.jsonl", self.event(),
                                        env=self.env, home=self.home)
        self.assertIn("skipped", result)
        self.assertNotIn("written", result)

    def test_empty_transcript_is_skipped(self):
        empty = os.path.join(self.tmp.name, "empty.jsonl")
        open(empty, "w").close()
        result = save_transcript.archive(empty, self.event(), env=self.env, home=self.home)
        self.assertIn("skipped", result)

    def test_oversized_transcript_still_keeps_the_raw_copy(self):
        env = dict(self.env, CLAUDE_TRANSCRIPT_MAX_MB="0")
        result = save_transcript.archive(self.transcript, self.event(), env=env, home=self.home)
        self.assertEqual(len(result["written"]), 1)
        self.assertTrue(result["written"][0].endswith(".jsonl"))
        self.assertIn("skipped_render", result)

    def test_cwd_falls_back_to_the_transcript_when_absent_from_the_payload(self):
        event = self.event()
        event.pop("cwd")
        save_transcript.archive(self.transcript, event, env=self.env, home=self.home)
        self.assertTrue(self.files())

    def test_thinking_can_be_disabled_by_env(self):
        with open(self.transcript, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "assistant", "timestamp": "2026-08-18T09:06:00.000Z",
                                 "cwd": self.cwd,
                                 "message": {"content": [{"type": "thinking",
                                                          "thinking": "segreto"}]}}) + "\n")
        env = dict(self.env, CLAUDE_TRANSCRIPT_THINKING="0")
        result = save_transcript.archive(self.transcript, self.event(), env=env, home=self.home)
        self.assertNotIn("segreto", read_text(result["written"][0]))


class TestHookProcess(HookTestCase):
    """The hook is run by Claude Code as a subprocess; these assert the process
    contract rather than the Python API."""

    def run_hook(self, payload, env_extra=None):
        env = dict(os.environ)
        env.update(self.env)
        env.update(env_extra or {})
        env["HOME"] = self.home
        return subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "save_transcript.py")],
            input=payload, capture_output=True, text=True, env=env, timeout=30)

    def test_exits_zero_and_prints_nothing_on_success(self):
        proc = self.run_hook(json.dumps(self.event()))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "", "stdout must stay empty: Claude Code parses it as JSON")
        self.assertTrue(self.files())

    def test_malformed_payload_never_breaks_the_turn(self):
        proc = self.run_hook("{ questo non e' json")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_empty_stdin_never_breaks_the_turn(self):
        self.assertEqual(self.run_hook("").returncode, 0)

    def test_missing_transcript_never_breaks_the_turn(self):
        payload = json.dumps({"hook_event_name": "Stop", "session_id": "x",
                              "transcript_path": "/nope.jsonl"})
        self.assertEqual(self.run_hook(payload).returncode, 0)

    def test_kill_switch_env_disables_saving(self):
        proc = self.run_hook(json.dumps(self.event()),
                             {"CLAUDE_TRANSCRIPT_AUTOSAVE": "0"})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.files(), [])

    def test_every_run_is_logged_for_debugging(self):
        self.run_hook(json.dumps(self.event()))
        self.run_hook("rotto")
        log = read_text(os.path.join(self.root, "_autosave.log"))
        self.assertIn("OK Stop", log)
        self.assertIn("ERRORE", log)


class TestSessionStartContext(HookTestCase):
    """SessionStart is the push half of the memory: the recent index entries are
    handed to the new session instead of waiting to be grepped."""

    def start_event(self, source="startup"):
        return {"hook_event_name": "SessionStart", "session_id": "new12345",
                "cwd": self.cwd, "source": source}

    def archive_current(self):
        save_transcript.archive(self.transcript, self.event(),
                                env=self.env, home=self.home)

    def test_recent_sessions_reach_the_new_session(self):
        self.archive_current()
        ctx = save_transcript.session_start_context(self.start_event(),
                                                    self.env, self.home)
        self.assertIn("prima domanda", ctx)

    def test_empty_archive_says_nothing(self):
        ctx = save_transcript.session_start_context(self.start_event(),
                                                    self.env, self.home)
        self.assertEqual(ctx, "")

    def test_compact_resume_is_not_reinjected(self):
        """After compaction the session already carries its own summary; adding
        the archive on top would spend context on what the user just kept."""
        self.archive_current()
        ctx = save_transcript.session_start_context(self.start_event(source="compact"),
                                                    self.env, self.home)
        self.assertEqual(ctx, "")

    def test_injection_can_be_disabled(self):
        self.archive_current()
        env = dict(self.env, CLAUDE_TRANSCRIPT_INJECT="0")
        ctx = save_transcript.session_start_context(self.start_event(), env, self.home)
        self.assertEqual(ctx, "")

    def test_session_count_is_configurable(self):
        self.archive_current()
        second = os.path.join(self.tmp.name, "beef5678-feed.jsonl")
        with open(second, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "type": "user", "timestamp": "2026-08-19T09:00:00.000Z",
                "cwd": self.cwd, "sessionId": "beef5678-feed",
                "message": {"role": "user",
                            "content": "seconda richiesta completamente diversa"}}) + "\n")
        save_transcript.archive(second, {"hook_event_name": "Stop",
                                         "session_id": "beef5678-feed",
                                         "transcript_path": second, "cwd": self.cwd},
                                env=self.env, home=self.home)
        env = dict(self.env, CLAUDE_TRANSCRIPT_INJECT_SESSIONS="1")
        ctx = save_transcript.session_start_context(self.start_event(), env, self.home)
        self.assertIn("seconda richiesta", ctx)
        self.assertNotIn("prima domanda", ctx)

    def test_a_resumed_session_does_not_inject_itself(self):
        """On resume the session's own history is already in the window; its
        index entry would be the one piece of the archive with zero news."""
        self.archive_current()
        event = self.start_event(source="resume")
        event["session_id"] = "dead1234-beef"
        ctx = save_transcript.session_start_context(event, self.env, self.home)
        self.assertEqual(ctx, "")

    def test_context_is_capped(self):
        directory = os.path.join(self.root, "proj")
        os.makedirs(directory)
        sessions = [{"session_id": f"s{i}", "title": "titolo",
                     "started": f"2026-07-{(i % 28) + 1:02d}T10:00:00+00:00",
                     "md": f"s{i}.md", "first_prompt": "parole a caso " * 40,
                     "files_edited": [f"file{j}.ts" for j in range(8)]}
                    for i in range(50)]
        with open(os.path.join(directory, "_index.json"), "w", encoding="utf-8") as fh:
            json.dump({"project": "proj", "sessions": sessions}, fh)
        env = dict(self.env, CLAUDE_TRANSCRIPT_INJECT_SESSIONS="50")
        ctx = save_transcript.session_start_context(self.start_event(), env, self.home)
        self.assertTrue(ctx)
        self.assertLessEqual(len(ctx), save_transcript.INJECT_MAX_CHARS)


class TestSessionStartProcess(HookTestCase):
    """Process contract for SessionStart: JSON on stdout is how the context gets
    injected — the one hook where stdout is supposed to speak."""

    def run_hook(self, payload, env_extra=None):
        env = dict(os.environ)
        env.update(self.env)
        env.update(env_extra or {})
        env["HOME"] = self.home
        return subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "save_transcript.py")],
            input=payload, capture_output=True, text=True, env=env, timeout=30)

    def payload(self, source="startup"):
        return json.dumps({"hook_event_name": "SessionStart",
                           "session_id": "new12345", "cwd": self.cwd,
                           "source": source})

    def test_prints_the_hook_json_contract(self):
        save_transcript.archive(self.transcript, self.event(),
                                env=self.env, home=self.home)
        proc = self.run_hook(self.payload())
        self.assertEqual(proc.returncode, 0)
        out = json.loads(proc.stdout)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("prima domanda", out["hookSpecificOutput"]["additionalContext"])

    def test_nothing_to_say_prints_nothing(self):
        proc = self.run_hook(self.payload())
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_a_broken_index_never_reaches_stdout(self):
        directory = os.path.join(self.root, "proj")
        os.makedirs(directory)
        with open(os.path.join(directory, "_index.json"), "w", encoding="utf-8") as fh:
            fh.write("{ rotto,,, }")
        proc = self.run_hook(self.payload())
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")


FOREIGN = {
    "hooks": {
        "PostToolUse": [{"matcher": "Edit|Write",
                         "hooks": [{"type": "command", "command": "node /altro/hook.mjs"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "node /altro/hook.mjs"}]}],
    },
    "model": "opus[1m]",
}


class TestInstaller(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "settings.json")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, data):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def read(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)

    def run_cli(self, *args):
        return install_hooks.main(["--settings", self.path, *args])

    def test_installs_into_a_missing_file(self):
        self.assertEqual(self.run_cli(), 0)
        self.assertEqual(install_hooks.registered_events(self.read()),
                         ["PreCompact", "SessionEnd", "SessionStart", "Stop"])

    def test_preserves_unrelated_settings_and_hooks(self):
        self.write(FOREIGN)
        self.run_cli()
        after = self.read()
        self.assertEqual(after["model"], "opus[1m]")
        self.assertEqual(len(after["hooks"]["PostToolUse"]), 1)
        stop_commands = [e["command"] for g in after["hooks"]["Stop"] for e in g["hooks"]]
        self.assertIn("node /altro/hook.mjs", stop_commands)
        self.assertEqual(sum("save_transcript.py" in c for c in stop_commands), 1)

    def test_is_idempotent(self):
        self.run_cli()
        self.run_cli()
        self.run_cli()
        stop = self.read()["hooks"]["Stop"]
        entries = [e for g in stop for e in g["hooks"] if install_hooks.is_ours(e)]
        self.assertEqual(len(entries), 1)

    def test_changing_the_event_list_converges(self):
        self.run_cli()
        self.run_cli("--events", "Stop")
        self.assertEqual(install_hooks.registered_events(self.read()), ["Stop"])

    def test_uninstall_removes_only_ours(self):
        self.write(FOREIGN)
        self.run_cli()
        self.run_cli("--uninstall")
        after = self.read()
        self.assertEqual(install_hooks.registered_events(after), [])
        self.assertEqual(len(after["hooks"]["Stop"]), 1)
        self.assertEqual(len(after["hooks"]["PostToolUse"]), 1)
        self.assertEqual(after["model"], "opus[1m]")

    def test_uninstall_leaves_no_empty_scaffolding(self):
        self.run_cli()
        self.run_cli("--uninstall")
        self.assertNotIn("hooks", self.read())

    def test_backup_is_taken_before_writing(self):
        self.write(FOREIGN)
        self.run_cli()
        backups = [f for f in os.listdir(self.tmp.name) if ".bak-" in f]
        self.assertEqual(len(backups), 1)
        with open(os.path.join(self.tmp.name, backups[0]), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), FOREIGN)

    def test_dry_run_writes_nothing(self):
        self.write(FOREIGN)
        self.run_cli("--dry-run")
        self.assertEqual(self.read(), FOREIGN)

    def test_refuses_to_touch_invalid_json(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ rotto,,, }")
        self.assertEqual(self.run_cli(), 1)
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "{ rotto,,, }")

    def test_rejects_unknown_events(self):
        self.assertEqual(self.run_cli("--events", "Stop,Inventato"), 1)
        self.assertFalse(os.path.exists(self.path))

    def test_command_is_guarded_against_a_moved_repo(self):
        self.run_cli()
        entry = self.read()["hooks"]["Stop"][0]["hooks"][0]
        self.assertTrue(entry["command"].startswith("[ ! -f "))
        self.assertIn("python3", entry["command"])
        self.assertEqual(entry["timeout"], install_hooks.TIMEOUT_SECONDS)


if __name__ == "__main__":
    unittest.main()


class TestArchivePrivacy(HookTestCase):
    def test_directories_are_not_world_readable(self):
        """Files are 0600, but a listable directory still exposes which projects
        the user works on and when."""
        save_transcript.archive(self.transcript, self.event(), env=self.env, home=self.home)
        for path in (self.root, os.path.join(self.root, "proj")):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700, path)

    def test_log_is_owner_only(self):
        save_transcript.log(self.root, "prova")
        self.assertEqual(
            stat.S_IMODE(os.stat(os.path.join(self.root, "_autosave.log")).st_mode), 0o600)


class TestForeignRootGuard(unittest.TestCase):
    """`~/.claude/transcripts` is already Claude Code's own directory. Writing an
    archive into a folder another tool manages is how an archive disappears."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_or_missing_root_is_fine(self):
        self.assertEqual(install_hooks.foreign_files(self.tmp.name), 0)
        self.assertEqual(install_hooks.foreign_files(os.path.join(self.tmp.name, "nope")), 0)

    def test_our_own_layout_is_not_foreign(self):
        os.makedirs(os.path.join(self.tmp.name, "progetto"))
        open(os.path.join(self.tmp.name, "_autosave.log"), "w").close()
        self.assertEqual(install_hooks.foreign_files(self.tmp.name), 0)

    def test_someone_elses_files_are_detected(self):
        for i in range(3):
            open(os.path.join(self.tmp.name, f"ses_{i}.jsonl"), "w").close()
        self.assertEqual(install_hooks.foreign_files(self.tmp.name), 3)


class TestIndexIntegration(HookTestCase):
    """The index is what makes the archive usable as memory: it has to stay
    correct across the way the hook actually runs — the same session archived
    once per turn, several sessions per project, snapshots in between."""

    def index_json(self, sub="proj"):
        return json.loads(read_text(os.path.join(self.root, sub, session_index.INDEX_JSON)))

    def index_md(self, sub="proj"):
        return read_text(os.path.join(self.root, sub, session_index.INDEX_MD))

    def second_session(self):
        path = os.path.join(self.tmp.name, "cafe5678-9999.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "type": "user", "timestamp": "2026-08-19T09:00:00.000Z", "cwd": self.cwd,
                "sessionId": "cafe5678-9999",
                "message": {"role": "user", "content": "altra conversazione"}}) + "\n")
        return path, {"hook_event_name": "Stop", "session_id": "cafe5678-9999",
                      "transcript_path": path, "cwd": self.cwd}

    def test_archiving_creates_both_index_files(self):
        save_transcript.archive(self.transcript, self.event(), env=self.env, home=self.home)
        self.assertIn(session_index.INDEX_MD, self.files())
        self.assertIn(session_index.INDEX_JSON, self.files())
        self.assertIn("proj", read_text(os.path.join(self.root, session_index.INDEX_MD)))

    def test_index_entry_carries_prompt_and_title(self):
        save_transcript.archive(self.transcript, self.event(), env=self.env, home=self.home)
        entry = self.index_json()["sessions"][0]
        self.assertEqual(entry["first_prompt"], "prima domanda")
        self.assertEqual(entry["md"], "2026-08-18_0900-dead1234.md")
        self.assertIn("prima domanda", self.index_md())

    def test_one_entry_per_session_not_per_turn(self):
        for _ in range(4):
            save_transcript.archive(self.transcript, self.event(), env=self.env, home=self.home)
        self.assertEqual(len(self.index_json()["sessions"]), 1)

    def test_multiple_sessions_accumulate_newest_first(self):
        save_transcript.archive(self.transcript, self.event(), env=self.env, home=self.home)
        path, event = self.second_session()
        save_transcript.archive(path, event, env=self.env, home=self.home)
        sessions = self.index_json()["sessions"]
        self.assertEqual(len(sessions), 2)
        md = self.index_md()
        self.assertLess(md.index("altra conversazione"), md.index("prima domanda"))

    def test_snapshot_does_not_create_a_second_entry(self):
        save_transcript.archive(self.transcript, self.event("Stop"), env=self.env, home=self.home)
        save_transcript.archive(self.transcript, self.event("PreCompact"),
                                env=self.env, home=self.home)
        sessions = self.index_json()["sessions"]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["snapshots"], ["2026-08-18_0900-dead1234.precompact-1.md"])
        self.assertEqual(sessions[0]["md"], "2026-08-18_0900-dead1234.md")

    def test_oversized_transcript_is_still_indexed(self):
        """A session missing from the index is invisible to recall, even though
        its raw copy sits right there."""
        env = dict(self.env, CLAUDE_TRANSCRIPT_MAX_MB="0")
        save_transcript.archive(self.transcript, self.event(), env=env, home=self.home)
        self.assertEqual(len(self.index_json()["sessions"]), 1)

    def test_corrupted_index_does_not_block_the_save(self):
        save_transcript.archive(self.transcript, self.event(), env=self.env, home=self.home)
        with open(os.path.join(self.root, "proj", session_index.INDEX_JSON), "w") as fh:
            fh.write("{ non piu' json")
        result = save_transcript.archive(self.transcript, self.event(),
                                        env=self.env, home=self.home)
        self.assertTrue(result.get("written"))
        self.assertEqual(len(self.index_json()["sessions"]), 1)

    def test_rebuild_recovers_the_index_from_the_archive_alone(self):
        save_transcript.archive(self.transcript, self.event(), env=self.env, home=self.home)
        path, event = self.second_session()
        save_transcript.archive(path, event, env=self.env, home=self.home)
        os.remove(os.path.join(self.root, "proj", session_index.INDEX_JSON))
        os.remove(os.path.join(self.root, "proj", session_index.INDEX_MD))

        count = save_transcript.rebuild_index(self.env, self.home)
        self.assertEqual(count, 2)
        self.assertEqual(len(self.index_json()["sessions"]), 2)
        self.assertIn("prima domanda", self.index_md())

    def test_rebuild_reattaches_snapshots_to_their_session(self):
        save_transcript.archive(self.transcript, self.event("Stop"), env=self.env, home=self.home)
        save_transcript.archive(self.transcript, self.event("PreCompact"),
                                env=self.env, home=self.home)
        os.remove(os.path.join(self.root, "proj", session_index.INDEX_JSON))
        save_transcript.rebuild_index(self.env, self.home)
        sessions = self.index_json()["sessions"]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["snapshots"], ["2026-08-18_0900-dead1234.precompact-1.md"])

    def test_index_files_are_owner_only(self):
        save_transcript.archive(self.transcript, self.event(), env=self.env, home=self.home)
        for name in (session_index.INDEX_MD, session_index.INDEX_JSON):
            path = os.path.join(self.root, "proj", name)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600, name)

    def test_concurrent_saves_keep_both_sessions(self):
        """Two sessions in the same project can finish a turn at the same instant;
        a lost read-modify-write would silently drop one from the memory."""
        import threading
        path, event = self.second_session()
        errors = []

        def run(transcript, ev):
            try:
                for _ in range(5):
                    save_transcript.archive(transcript, ev, env=self.env, home=self.home)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(self.transcript, self.event())),
                   threading.Thread(target=run, args=(path, event))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        ids = {s["session_id"] for s in self.index_json()["sessions"]}
        self.assertEqual(ids, {"dead1234-beef", "cafe5678-9999"})


class TestProjectAttribution(HookTestCase):
    def test_a_session_that_changed_directory_stays_in_one_folder(self):
        """Regression: running `cd` inside a session made the hook file later
        turns under a second project, duplicating the conversation."""
        event = self.event()
        event["cwd"] = os.path.join(self.home, "altrove")
        save_transcript.archive(self.transcript, self.event(), env=self.env, home=self.home)
        save_transcript.archive(self.transcript, event, env=self.env, home=self.home)
        folders = [d for d in os.listdir(self.root)
                   if os.path.isdir(os.path.join(self.root, d))]
        self.assertEqual(folders, ["proj"])


class TestBackfill(HookTestCase):
    def setUp(self):
        super().setUp()
        self.projects = os.path.join(self.home, ".claude", "projects", "-proj")
        os.makedirs(os.path.join(self.projects, "sess", "subagents"))
        for path in (os.path.join(self.projects, "sess.jsonl"),
                     os.path.join(self.projects, "sess", "subagents", "agent-1.jsonl")):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.text + "\n")

    def test_archives_history_but_skips_subagent_fragments(self):
        """A subagent transcript is part of a session, not a session: indexing it
        separately would bury the real conversations."""
        done, total = save_transcript.backfill(self.env, self.home)
        self.assertEqual((done, total), (1, 1))
        self.assertEqual(len(self.index_json()["sessions"]), 1)

    def index_json(self, sub="proj"):
        return json.loads(read_text(os.path.join(self.root, sub, session_index.INDEX_JSON)))


class TestEmptyShells(HookTestCase):
    def test_metadata_only_transcript_is_not_archived(self):
        path = os.path.join(self.tmp.name, "shell.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "ai-title", "aiTitle": "Titolo",
                                 "sessionId": "aa12d12e"}) + "\n")
        result = save_transcript.archive(path, self.event(), env=self.env, home=self.home)
        self.assertEqual(result.get("skipped"), "nessuna conversazione")
        self.assertFalse(os.path.isdir(os.path.join(self.root, "proj")))


class TestStatusCount(HookTestCase):
    def test_index_files_are_not_counted_as_conversations(self):
        save_transcript.archive(self.transcript, self.event(), env=self.env, home=self.home)
        import io
        import contextlib
        buffer = io.StringIO()
        env_backup = dict(os.environ)
        os.environ["CLAUDE_TRANSCRIPT_DIR"] = self.root
        try:
            with contextlib.redirect_stdout(buffer):
                install_hooks.main(["--status"])
        finally:
            os.environ.clear()
            os.environ.update(env_backup)
        self.assertIn("1 conversazioni archiviate", buffer.getvalue())
