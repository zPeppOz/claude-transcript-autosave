"""Tests for the conversation index — the layer that makes the archive recallable."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import session_index as idx
import transcript_lib as lib


def assistant(*tool_calls, ts="2026-08-18T10:00:00.000Z"):
    return {"type": "assistant", "timestamp": ts, "cwd": "/home/u/proj",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}
                for i, (name, inp) in enumerate(tool_calls)]}}


def user(text, ts="2026-08-18T10:00:00.000Z", meta=False):
    rec = {"type": "user", "timestamp": ts, "cwd": "/home/u/proj",
           "message": {"role": "user", "content": text}}
    if meta:
        rec["isMeta"] = True
    return rec


class TestFilesTouched(unittest.TestCase):
    def test_edits_and_writes_are_the_modified_set(self):
        records = [assistant(("Edit", {"file_path": "/home/u/proj/a.ts"}),
                             ("Write", {"file_path": "/home/u/proj/b.ts"}),
                             ("Read", {"file_path": "/home/u/proj/c.ts"}))]
        edited, read = idx.files_touched(records, "/home/u/proj")
        self.assertEqual(edited, ["a.ts", "b.ts"])
        self.assertEqual(read, ["c.ts"])

    def test_paths_are_relative_to_the_project(self):
        records = [assistant(("Edit", {"file_path": "/home/u/proj/src/deep/x.ts"}))]
        self.assertEqual(idx.files_touched(records, "/home/u/proj")[0], ["src/deep/x.ts"])

    def test_paths_outside_the_project_stay_absolute(self):
        records = [assistant(("Edit", {"file_path": "/etc/hosts"}))]
        self.assertEqual(idx.files_touched(records, "/home/u/proj")[0], ["/etc/hosts"])

    def test_most_edited_file_comes_first(self):
        records = [assistant(("Edit", {"file_path": "/home/u/proj/rare.ts"}),
                             ("Edit", {"file_path": "/home/u/proj/hot.ts"}),
                             ("Edit", {"file_path": "/home/u/proj/hot.ts"}))]
        self.assertEqual(idx.files_touched(records, "/home/u/proj")[0][0], "hot.ts")

    def test_malformed_tool_inputs_are_ignored(self):
        records = [assistant(("Edit", None), ("Edit", {"file_path": 42}), ("Edit", {}))]
        self.assertEqual(idx.files_touched(records, "/home/u/proj"), ([], []))


class TestFirstPrompt(unittest.TestCase):
    def test_returns_the_opening_request_flattened(self):
        records = [user("sistema\n  il   build\nrotto"), user("poi altro")]
        self.assertEqual(idx.first_prompt(records), "sistema il build rotto")

    def test_skips_injected_context(self):
        records = [user("<system-reminder>rumore</system-reminder>", meta=True),
                   user("la vera domanda")]
        self.assertEqual(idx.first_prompt(records), "la vera domanda")

    def test_slash_command_keeps_the_arguments(self):
        """A slash command's expansion is boilerplate; the arguments are the ask."""
        records = [user("<command-message>skill-creator</command-message>"
                        "<command-name>/skill-creator</command-name> lunga premessa "
                        "ARGUMENTS: crea una skill per i transcript")]
        self.assertEqual(idx.first_prompt(records), "crea una skill per i transcript")

    def test_is_truncated(self):
        self.assertEqual(len(idx.first_prompt([user("x" * 900)], limit=100)), 101)

    def test_no_prompt_is_not_an_error(self):
        self.assertEqual(idx.first_prompt([assistant(("Read", {"file_path": "a"}))]), "")


class TestEntryAndUpsert(unittest.TestCase):
    def entry(self, session_id="s1", title="Titolo", started="2026-08-18T10:00:00.000Z",
              snapshot=""):
        records = [user("la richiesta", ts=started),
                   assistant(("Edit", {"file_path": "/home/u/proj/a.ts"}),
                             ts="2026-08-18T10:30:00.000Z")]
        meta = lib.session_meta(records, session_id=session_id, home="/home/u")
        meta.title = title
        return idx.build_entry(records, meta, "file.md", "file.jsonl", snapshot)

    def test_entry_carries_the_retrieval_keys(self):
        e = self.entry()
        self.assertEqual(e["first_prompt"], "la richiesta")
        self.assertEqual(e["files_edited"], ["a.ts"])
        self.assertEqual(e["duration_min"], 30)
        self.assertEqual(e["session_id"], "s1")

    def test_same_session_replaces_instead_of_appending(self):
        """The hook archives the same session on every turn; appending would grow
        the index by one row per turn."""
        index = {"project": "proj", "sessions": []}
        idx.upsert(index, self.entry(title="primo"))
        idx.upsert(index, self.entry(title="aggiornato"))
        self.assertEqual(len(index["sessions"]), 1)
        self.assertEqual(index["sessions"][0]["title"], "aggiornato")

    def test_different_sessions_accumulate(self):
        index = {"project": "proj", "sessions": []}
        idx.upsert(index, self.entry(session_id="s1"))
        idx.upsert(index, self.entry(session_id="s2"))
        self.assertEqual(len(index["sessions"]), 2)

    def test_snapshots_attach_to_their_session_without_duplicating(self):
        index = {"project": "proj", "sessions": []}
        idx.upsert(index, self.entry())
        idx.upsert(index, self.entry(snapshot="file.precompact-1.md"))
        idx.upsert(index, self.entry(snapshot="file.precompact-1.md"))
        idx.upsert(index, self.entry(snapshot="file.precompact-2.md"))
        self.assertEqual(len(index["sessions"]), 1)
        self.assertEqual(index["sessions"][0]["snapshots"],
                         ["file.precompact-1.md", "file.precompact-2.md"])

    def test_newest_session_sorts_first(self):
        index = {"project": "proj", "sessions": []}
        idx.upsert(index, self.entry(session_id="vecchia", started="2026-01-01T08:00:00.000Z"))
        idx.upsert(index, self.entry(session_id="nuova", started="2026-08-18T08:00:00.000Z"))
        self.assertEqual(idx.sorted_sessions(index)[0]["session_id"], "nuova")


class TestRendering(unittest.TestCase):
    def index(self, **over):
        entry = {"session_id": "abc", "title": "Import DDP", "started": "2026-08-18T10:00:00+02:00",
                 "user_turns": 4, "tool_calls": 12, "duration_min": 45, "branch": "main",
                 "first_prompt": "importa il tracciato DDP", "files_edited": ["src/etl.ts"],
                 "files_read": [], "md": "2026-08-18_1000-abc.md", "jsonl": "x.jsonl",
                 "snapshots": []}
        entry.update(over)
        return {"project": "siarx", "sessions": [entry]}

    def test_index_contains_the_retrieval_keys(self):
        md = idx.render_index_md(self.index())
        for expected in ("# Indice sessioni — siarx", "Import DDP", "2026-08-18_1000-abc.md",
                         "importa il tracciato DDP", "src/etl.ts", "45 min", "branch `main`"):
            self.assertIn(expected, md)

    def test_read_files_shown_when_nothing_was_modified(self):
        """An investigation that changed no files is still worth recalling."""
        md = idx.render_index_md(self.index(files_edited=[], files_read=["src/auth.ts"]))
        self.assertIn("consultati: `src/auth.ts`", md)

    def test_long_file_lists_are_summarised(self):
        md = idx.render_index_md(self.index(files_edited=[f"f{i}.ts" for i in range(9)]))
        self.assertIn("(+4)", md)

    def test_snapshots_are_listed(self):
        md = idx.render_index_md(self.index(snapshots=["x.precompact-1.md"]))
        self.assertIn("x.precompact-1.md", md)

    def test_empty_index_still_renders(self):
        md = idx.render_index_md({"project": "vuoto", "sessions": []})
        self.assertIn("0 sessioni", md)

    def test_root_index_maps_the_projects(self):
        md = idx.render_root_index([
            {"project": "siarx", "count": 12, "last": "2026-08-18T10:00:00+02:00",
             "last_title": "Import DDP"},
            {"project": "tesoro", "count": 3, "last": "2026-08-01T10:00:00+02:00",
             "last_title": "Landing"}])
        self.assertIn("15 conversazioni in 2 progetti", md)
        self.assertIn("[siarx](siarx/INDEX.md)", md)
        self.assertLess(md.index("siarx"), md.index("tesoro"), "più recente per primo")

    def test_pipe_in_a_title_cannot_break_the_table(self):
        md = idx.render_root_index([{"project": "p", "count": 1, "last": "2026-08-18T10:00:00+02:00",
                                     "last_title": "a | b"}])
        self.assertIn("a \\| b", md)


if __name__ == "__main__":
    unittest.main()


class TestSlashCommandPrompts(unittest.TestCase):
    """Slash commands expand into their whole skill body; indexing that raw fills
    the memory with boilerplate instead of the request."""

    def test_command_args_become_the_prompt(self):
        text = ("<command-message>skill-creator</command-message>"
                "<command-name>/skill-creator</command-name>"
                "<command-args>crea una skill per i transcript</command-args>"
                " Base directory for this skill: ... testo lunghissimo della skill ...")
        self.assertEqual(idx.first_prompt([user(text)]),
                         "/skill-creator crea una skill per i transcript")

    def test_bare_command_defers_to_the_next_real_message(self):
        records = [user("<command-name>/clear</command-name>"
                        "<command-message>clear</command-message><command-args></command-args>"),
                   user("ora pianifichiamo la fase 1")]
        self.assertEqual(idx.first_prompt(records), "ora pianifichiamo la fase 1")

    def test_bare_command_is_kept_if_there_is_nothing_else(self):
        records = [user("<command-name>/clear</command-name><command-args></command-args>")]
        self.assertEqual(idx.first_prompt(records), "/clear")

    def test_plain_prompt_is_untouched(self):
        self.assertEqual(idx.first_prompt([user("sistema il build")]), "sistema il build")


class TestBashDerivedFiles(unittest.TestCase):
    """Work done through heredocs and sed leaves no file_path; without this the
    index would report nothing for entire sessions."""

    def bash(self, command):
        return [assistant(("Bash", {"command": command, "description": "x"}))]

    def test_paths_in_commands_are_captured(self):
        _, read = idx.files_touched(self.bash("cat > /home/u/proj/scripts/save.py <<'EOF'"),
                                    "/home/u/proj")
        self.assertEqual(read, ["scripts/save.py"])

    def test_glob_patterns_and_flags_are_not_files(self):
        _, read = idx.files_touched(
            self.bash("grep -rn 'foo' --include='*.ts' -A2 . | head -5"), "/home/u/proj")
        self.assertEqual(read, [])

    def test_shell_paths_never_displace_real_tool_paths(self):
        records = [assistant(("Edit", {"file_path": "/home/u/proj/a.ts"}),
                             ("Bash", {"command": "cat b.ts"}))]
        edited, read = idx.files_touched(records, "/home/u/proj")
        self.assertEqual(edited, ["a.ts"])
        self.assertEqual(read, ["b.ts"])

    def test_a_file_already_known_is_not_repeated(self):
        records = [assistant(("Read", {"file_path": "/home/u/proj/a.ts"}),
                             ("Bash", {"command": "wc -l a.ts"}))]
        self.assertEqual(idx.files_touched(records, "/home/u/proj")[1], ["a.ts"])

    def test_relative_command_is_not_read_as_absolute(self):
        """`./install.sh` must not be indexed as `/install.sh`."""
        _, read = idx.files_touched(self.bash("./install.sh --dry-run"), "/home/u/proj")
        self.assertEqual(read, ["install.sh"])

    def test_capped_so_one_noisy_command_cannot_flood_the_entry(self):
        cmd = " ".join(f"f{i}.ts" for i in range(40))
        self.assertLessEqual(len(idx.files_touched(self.bash(cmd), "/home/u/proj")[1]),
                             idx.BASH_PATH_LIMIT)


class TestPlurals(unittest.TestCase):
    def test_project_index(self):
        one = {"project": "p", "sessions": [{"session_id": "a", "started": "2026-08-18T10:00:00+02:00",
                                             "title": "t", "md": "a.md"}]}
        self.assertIn("1 sessione ·", idx.render_index_md(one))
        one["sessions"].append({"session_id": "b", "started": "2026-08-17T10:00:00+02:00",
                                "title": "t2", "md": "b.md"})
        self.assertIn("2 sessioni ·", idx.render_index_md(one))

    def test_root_index(self):
        md = idx.render_root_index([{"project": "p", "count": 1,
                                     "last": "2026-08-18T10:00:00+02:00", "last_title": "t"}])
        self.assertIn("1 conversazione in 1 progetto", md)
