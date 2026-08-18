"""Rendering and parsing tests. Stdlib only: `python3 -m unittest discover tests`."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import transcript_lib as lib


def rec(**kw):
    base = {"sessionId": "abcd1234-ef56", "cwd": "/home/u/proj", "version": "2.1.234",
            "gitBranch": "main"}
    base.update(kw)
    return base


def sample_records():
    return [
        rec(type="ai-title", aiTitle="Titolo generato"),
        rec(type="user", timestamp="2026-08-18T10:00:00.000Z",
            message={"role": "user", "content": "ciao, sistemami il build"}),
        rec(type="attachment", attachment={"kind": "noise"}),
        rec(type="user", timestamp="2026-08-18T10:00:01.000Z", isMeta=True,
            message={"role": "user", "content": "<system-reminder>rumore</system-reminder>"}),
        rec(type="assistant", timestamp="2026-08-18T10:00:30.000Z",
            message={"role": "assistant", "model": "claude-opus-5",
                     "usage": {"input_tokens": 10, "output_tokens": 5,
                               "cache_read_input_tokens": 100},
                     "content": [
                         {"type": "thinking", "thinking": "penso quindi rendo"},
                         {"type": "text", "text": "Guardo il progetto."},
                         {"type": "tool_use", "id": "t1", "name": "Bash",
                          "input": {"command": "ls -la", "description": "Elenca i file"}},
                     ]}),
        rec(type="user", timestamp="2026-08-18T10:00:31.000Z",
            message={"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "totale 0\ndrwx"},
            ]}),
        rec(type="system", subtype="turn_duration", content=None),
        rec(type="assistant", timestamp="2026-08-18T10:00:40.000Z",
            message={"role": "assistant", "model": "claude-opus-5",
                     "content": [{"type": "text", "text": "Fatto."}]}),
    ]


class TestSlugAndNaming(unittest.TestCase):
    def test_home_relative_slug(self):
        self.assertEqual(lib.project_slug("/home/u/addway/siarx", "/home/u"), "addway-siarx")

    def test_home_itself(self):
        self.assertEqual(lib.project_slug("/home/u", "/home/u"), "home")

    def test_outside_home(self):
        self.assertEqual(lib.project_slug("/srv/app/web", "/home/u"), "srv-app-web")

    def test_hidden_dirs_do_not_produce_hidden_folders(self):
        slug = lib.project_slug("/home/u/proj/.claude-worktrees/feat", "/home/u")
        self.assertEqual(slug, "proj-claude-worktrees-feat")
        self.assertFalse(slug.startswith("."))

    def test_distinct_projects_sharing_a_basename_do_not_collide(self):
        a = lib.project_slug("/home/u/a/web", "/home/u")
        b = lib.project_slug("/home/u/b/web", "/home/u")
        self.assertNotEqual(a, b)

    def test_basename_is_stable_as_the_session_grows(self):
        """The hook rewrites this file every turn; an unstable name would leave
        one orphan file per turn instead of one live file per session."""
        records = sample_records()
        first = lib.output_basename(records[:3], "abcd1234-ef56")
        later = lib.output_basename(records, "abcd1234-ef56")
        self.assertEqual(first, later)
        self.assertTrue(first.endswith("-abcd1234"), first)

    def test_basename_without_timestamps(self):
        self.assertEqual(lib.output_basename([{"type": "x"}], "abcd1234-ef"), "nodate-abcd1234")


class TestLoading(unittest.TestCase):
    def test_skips_malformed_and_blank_lines(self):
        text = '{"type":"user"}\n\n{not json}\n{"type":"assistant"}\n{"trunc'
        self.assertEqual(len(lib.load_records(text)), 2)

    def test_ignores_non_object_lines(self):
        self.assertEqual(lib.load_records('[1,2]\n"str"\n{"type":"user"}'), [{"type": "user"}])


class TestMeta(unittest.TestCase):
    def setUp(self):
        self.meta = lib.session_meta(sample_records(), session_id="abcd1234-ef56",
                                     trigger="Stop", home="/home/u")

    def test_title_prefers_ai_title(self):
        self.assertEqual(self.meta.title, "Titolo generato")

    def test_title_falls_back_to_first_prompt(self):
        records = [r for r in sample_records() if r.get("type") != "ai-title"]
        meta = lib.session_meta(records, home="/home/u")
        self.assertEqual(meta.title, "ciao, sistemami il build")

    def test_counts_exclude_meta_and_noise(self):
        self.assertEqual(self.meta.user_turns, 1)
        self.assertEqual(self.meta.assistant_turns, 2)
        self.assertEqual(self.meta.tool_calls, 1)

    def test_models_and_tokens(self):
        self.assertEqual(self.meta.models, ["claude-opus-5"])
        self.assertEqual(self.meta.input_tokens, 110)
        self.assertEqual(self.meta.output_tokens, 5)

    def test_project_and_branch(self):
        self.assertEqual(self.meta.project, "proj")
        self.assertEqual(self.meta.git_branch, "main")


class TestRendering(unittest.TestCase):
    def render(self, records=None, **opts):
        records = records or sample_records()
        meta = lib.session_meta(records, session_id="abcd1234-ef56", trigger="Stop",
                                home="/home/u")
        return lib.render_markdown(records, meta, lib.RenderOptions(**opts))

    def test_contains_frontmatter_and_title(self):
        md = self.render()
        self.assertTrue(md.startswith("---\n"))
        self.assertIn('title: "Titolo generato"', md)
        self.assertIn('saved_on: "Stop"', md)
        self.assertIn("# Titolo generato", md)

    def test_renders_both_speakers_in_order(self):
        md = self.render()
        self.assertLess(md.index("## 👤 Utente"), md.index("## 🤖 Claude"))
        self.assertIn("ciao, sistemami il build", md)
        self.assertIn("Guardo il progetto.", md)

    def test_tool_call_is_stitched_to_its_output(self):
        md = self.render()
        self.assertIn("**→ Bash** · Elenca i file", md)
        self.assertIn("ls -la", md)
        self.assertIn("↳ output", md)
        self.assertIn("totale 0", md)

    def test_thinking_collapsed_by_default_and_omittable(self):
        self.assertIn("💭 Ragionamento", self.render())
        self.assertNotIn("💭 Ragionamento", self.render(include_thinking=False))

    def test_meta_records_hidden_by_default(self):
        self.assertNotIn("system-reminder", self.render())
        self.assertIn("system-reminder", self.render(include_meta=True))

    def test_noise_records_are_dropped(self):
        md = self.render()
        self.assertNotIn("turn_duration", md)
        self.assertNotIn("aiTitle", md)

    def test_consecutive_assistant_records_share_one_heading(self):
        self.assertEqual(self.render().count("## 🤖 Claude"), 1)

    def test_error_results_are_flagged(self):
        records = sample_records()
        records[5]["message"]["content"][0]["is_error"] = True
        self.assertIn("⚠️ errore", self.render(records))

    def test_long_output_is_truncated(self):
        records = sample_records()
        records[5]["message"]["content"][0]["content"] = "x" * 5000
        md = self.render(records, max_result_chars=100)
        self.assertIn("tagliato, 5000 caratteri", md)
        self.assertNotIn("x" * 200, md)

    def test_backticks_in_output_cannot_escape_the_code_block(self):
        """Tool output containing ``` would otherwise break every heading below."""
        records = sample_records()
        records[5]["message"]["content"][0]["content"] = "prima\n```\nfinto\n```\ndopo"
        md = self.render(records)
        self.assertIn("````", md)

    def test_images_and_documents_are_noted(self):
        records = sample_records()
        records[1]["message"]["content"] = [{"type": "image", "source": {}}]
        self.assertIn("[immagine allegata]", self.render(records))

    def test_survives_unknown_record_and_block_types(self):
        records = sample_records() + [
            rec(type="brand-new-record-type-2027", payload={"a": 1}),
            rec(type="assistant", timestamp="2026-08-18T10:01:00.000Z",
                message={"role": "assistant", "content": [
                    {"type": "future_block", "data": "?"},
                    {"type": "text", "text": "ancora qui"}]}),
        ]
        md = self.render(records)
        self.assertIn("ancora qui", md)

    def test_survives_hostile_shapes(self):
        records = [
            {"type": "user", "message": None},
            {"type": "assistant", "message": {"content": "stringa invece di lista"}},
            {"type": "user", "message": {"content": [None, 42, {"type": "text", "text": "ok"}]}},
            {"type": "system", "subtype": "informational", "content": "avviso reale"},
        ]
        meta = lib.session_meta(records, home="/home/u")
        md = lib.render_markdown(records, meta)
        self.assertIn("ok", md)
        self.assertIn("avviso reale", md)


class TestHelpers(unittest.TestCase):
    def test_fence_grows_past_longest_backtick_run(self):
        self.assertEqual(lib.fence_for("nessuno"), "```")
        self.assertEqual(lib.fence_for("a ``` b"), "````")
        self.assertEqual(lib.fence_for("a ````` b"), "``````")

    def test_clip_noop_under_limit(self):
        self.assertEqual(lib.clip("breve", 100), "breve")

    def test_human_size(self):
        self.assertEqual(lib.human_size(512), "512 B")
        self.assertEqual(lib.human_size(2048), "2.0 KB")

    def test_yaml_values_are_escaped(self):
        md = lib._yaml_value('con "virgolette" e: due punti')
        self.assertEqual(json.loads(md), 'con "virgolette" e: due punti')


if __name__ == "__main__":
    unittest.main()


class TestLongMessageFolding(unittest.TestCase):
    """A generated prompt can be 100 KB (a /security-review payload). It must
    stay complete in the archive without burying the rest of the session."""

    def render(self, text, **opts):
        records = [rec(type="user", timestamp="2026-08-18T10:00:00.000Z",
                       message={"role": "user", "content": text})]
        meta = lib.session_meta(records, home="/home/u")
        return lib.render_markdown(records, meta, lib.RenderOptions(**opts))

    def test_long_message_is_folded_not_cut(self):
        text = ("riga unica molto lunga " * 400).strip()
        md = self.render(text)
        self.assertIn("<details>", md)
        self.assertIn(text, md, "il testo integrale deve restare nel documento")
        self.assertNotIn("tagliato", md)

    def test_short_message_is_not_folded(self):
        self.assertNotIn("<details>", self.render("due parole"))

    def test_fold_threshold_is_configurable(self):
        self.assertNotIn("<details>", self.render("x" * 4000, collapse_text_chars=0))

    def test_summary_cannot_inject_html(self):
        md = self.render("<script>alert(1)</script> " * 300)
        self.assertIn("&lt;script&gt;", md.split("</summary>")[0])


class TestTurnCounting(unittest.TestCase):
    def test_tool_only_turns_are_counted(self):
        """A turn spent entirely on tool calls is still a turn; reporting 0 for a
        session with 15 tool calls reads like a bug in the archiver."""
        records = [rec(type="assistant", timestamp="2026-08-18T10:00:00.000Z",
                       message={"role": "assistant", "content": [
                           {"type": "tool_use", "id": "a", "name": "Read", "input": {}}]})]
        self.assertEqual(lib.session_meta(records, home="/home/u").assistant_turns, 1)
