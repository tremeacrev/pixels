"""Corpus-based fleet sizing and coverage; no model calls."""

import json
from pathlib import Path
import sys
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / "tools"))
from ceps_fleet import MAX_WORKERS, MIN_WORKERS, build_fleet


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def test_empty_and_small_corpora_have_multiple_workers(self):
        self.assertEqual(build_fleet(self.root).workers, MIN_WORKERS)
        self.write("specification/a.md", "# A\nSmall specification.\n")
        self.assertEqual(build_fleet(self.root).workers, MIN_WORKERS)

    def test_each_structural_metric_can_increase_fleet_size(self):
        cases = {
            "documents": {f"specification/{index}.md": "A" for index in range(5)},
            "words": {"specification/a.md": "requirement " * 4801},
            "headings": {"specification/a.md": "# Topic\n" * 49},
            "cross_file_links": {"specification/a.md": "[dependency](b.md)\n" * 49},
        }
        for metric, documents in cases.items():
            with self.subTest(metric=metric), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for path, text in documents.items():
                    target = root / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(text)
                profile = build_fleet(root)
                self.assertEqual(profile.workers, 7)
                self.assertGreater(profile.summary[metric], 0)

    def test_large_corpora_are_capped(self):
        self.write("specification/a.md", "word " * 100_000)
        self.assertEqual(build_fleet(self.root).workers, MAX_WORKERS)

    def test_corpus_includes_nested_metaspecification_and_is_refreshed(self):
        self.write("specification/a.md", "# Product\nA requirement.")
        before = build_fleet(self.root)
        self.write("metaspecification/nested/style.md", "# Style\n" * 49)
        self.write("tools/unrelated.md", "ignored " * 100_000)
        after = build_fleet(self.root)
        self.assertEqual(before.summary["documents"], 1)
        self.assertEqual(after.summary["documents"], 2)
        self.assertEqual(after.workers, 7)
        self.assertEqual(json.loads(json.dumps(after.summary)), after.summary)

    def test_only_cross_file_relative_links_count(self):
        self.write("specification/a.md", "\n".join((
            "[other](b.md#section)",
            "[conventions](../metaspecification/style.md)",
            "[title](<other file.md> 'label')",
            "[self](a.md#section)",
            "[self](./a.md)",
            "[heading](#section)",
            "[web](https://example.test/b.md)",
            "[web](//example.test/b.md)",
            "[absolute](/tmp/other.md)",
            "![image](image.png)",
        )))
        self.assertEqual(build_fleet(self.root).summary["cross_file_links"], 3)

    def test_assignments_cover_every_document_and_preserve_task_in_every_phase(self):
        paths = [f"specification/domain-{index}.md" for index in range(35)]
        paths += ["metaspecification/style.md"]
        for index, path in enumerate(paths):
            self.write(path, "requirement " * (index + 1))
        profile = build_fleet(self.root)
        task = "First requirement.\nSecond requirement with exact details."
        for phase in ("understand", "plan", "review"):
            with self.subTest(phase=phase):
                prompts = profile.assignments(task, phase)
                self.assertEqual(len(prompts), profile.workers)
                self.assertEqual(len(set(prompts)), profile.workers)
                for prompt in prompts:
                    self.assertTrue(prompt.endswith(task))
                    self.assertIn(f"phase {phase}", prompt)
                for path in paths:
                    self.assertIn(path, "\n".join(prompts[:-2]))
                self.assertTrue(all("Cross-cutting perspective:" in prompt for prompt in prompts[-2:]))

    def test_focused_assignments_balance_documents_by_weight(self):
        self.write("specification/large.md", "requirement " * 36_000)
        for index in range(29):
            self.write(f"specification/small-{index}.md", "Small")
        profile = build_fleet(self.root)
        prompts = profile.assignments("Understand", "understand")
        self.assertTrue(all("specification/large.md (words " in prompt for prompt in prompts[:-2]))
        for index in range(29):
            self.assertIn(f"specification/small-{index}.md", "\n".join(prompts[:-2]))

    def test_extra_workers_get_distinct_document_spans_and_perspectives(self):
        self.write("specification/complex.md", "requirement " * 6001)
        profile = build_fleet(self.root)
        prompts = profile.assignments("Review the whole document", "review")
        self.assertEqual(profile.workers, 8)
        self.assertTrue(all("specification/complex.md" in prompt for prompt in prompts[:-2]))
        bodies = [prompt.split("\n", 1)[1] for prompt in prompts]
        self.assertEqual(len(set(bodies)), profile.workers)
        self.assertNotEqual(prompts, profile.assignments("Review the whole document", "plan"))

    def test_maximum_fleet_has_distinct_spans_beyond_its_repeated_lenses(self):
        self.write("specification/complex.md", "requirement " * 60_000)
        profile = build_fleet(self.root)
        prompts = profile.assignments("Review", "review")
        self.assertEqual(profile.workers, MAX_WORKERS)
        bodies = [prompt.split("\n", 1)[1] for prompt in prompts]
        self.assertEqual(len(set(bodies)), MAX_WORKERS)
        self.assertIn("words 1-2000 of 60000", prompts[0])
        self.assertIn("words 58001-60000 of 60000", prompts[-3])

    def test_heading_and_compact_character_spans_are_supported(self):
        self.write("specification/lines.md", "# Topic\nContent.\n" * 30)
        prompts = build_fleet(self.root).assignments("Understand", "understand")
        self.assertTrue(all("(words " in prompt for prompt in prompts[:-2]))
        self.write("specification/lines.md", "[dependency](other.md)" * 360)
        prompts = build_fleet(self.root).assignments("Understand", "understand")
        self.assertTrue(all("(characters " in prompt for prompt in prompts[:-2]))
        self.assertEqual(len({prompt.split("\n", 1)[1] for prompt in prompts}), MAX_WORKERS)

    def test_one_large_line_is_spread_across_the_fleet(self):
        self.write("specification/skew.md", "word " * 60_000 + "\n# Small\n" * 29)
        prompts = build_fleet(self.root).assignments("Understand", "understand")
        self.assertTrue(all("(words " in prompt for prompt in prompts[:-2]))
        self.assertEqual(len({prompt.split("\n", 1)[1] for prompt in prompts}), MAX_WORKERS)

    def test_small_documents_still_get_independent_overlapping_perspectives(self):
        self.write("specification/tiny.md", "A requirement.")
        prompts = build_fleet(self.root).assignments("Understand", "understand")
        self.assertTrue(all("specification/tiny.md" in prompt for prompt in prompts[:-2]))
        self.assertIn("additional independent perspective", prompts[1])

    def test_assignments_are_deterministic_and_validate_phase_and_task(self):
        self.write("specification/a.md", "# A\nA requirement.")
        self.write("specification/b.md", "# B\nAnother requirement.")
        first = build_fleet(self.root)
        second = build_fleet(self.root)
        self.assertEqual(first, second)
        self.assertEqual(first.assignments("Understand", "understand"),
                         second.assignments("Understand", "understand"))
        with self.assertRaises(ValueError):
            first.assignments("Understand", "unknown")
        with self.assertRaises(ValueError):
            first.assignments(" ", "understand")


if __name__ == "__main__":
    unittest.main()
