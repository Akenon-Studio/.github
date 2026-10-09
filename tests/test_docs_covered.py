"""Tests for scripts/docs_covered.py (design 6.5 item 3). Run: python3 -m unittest discover tests"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
from docs_covered import problems, stale_docs  # noqa: E402

DOCS = {"docs/architecture/signals.md": ["packages/signals/**"],
        "packages/sdk/README.md": ["packages/sdk/**"],
        "docs/architecture/api.md": ["apps/api/src/{a,b}.ts"]}


class StaleTest(unittest.TestCase):
    def test_covered_code_changed_without_its_doc(self):
        stale = stale_docs(["packages/signals/src/fft.ts", "README.md"], DOCS)
        self.assertEqual(stale, {"docs/architecture/signals.md": ["packages/signals/src/fft.ts"]})

    def test_non_doc_markdown_counts_as_covered_code(self):
        docs = {"docs/runbooks/agents.md": ["CLAUDE.md"]}
        self.assertEqual(stale_docs(["CLAUDE.md"], docs), {"docs/runbooks/agents.md": ["CLAUDE.md"]})

    def test_changing_the_doc_too_is_enough(self):
        self.assertEqual(stale_docs(["packages/sdk/src/x.ts", "packages/sdk/README.md"], DOCS), {})

    def test_brace_globs_and_unrelated_files(self):
        self.assertIn("docs/architecture/api.md", stale_docs(["apps/api/src/b.ts"], DOCS))
        self.assertEqual(stale_docs(["apps/web/x.ts"], DOCS), {})
        self.assertEqual(stale_docs(["apps/api/src/c.ts"], DOCS), {})  # not in {a,b}


class TickTest(unittest.TestCase):
    STALE = {"docs/architecture/signals.md": ["x"], "packages/sdk/README.md": ["y"]}

    def test_a_ticked_line_per_doc_clears_it(self):
        body = "## Docs\n- [x] Still accurate: docs/architecture/signals.md\n- [ ] Still accurate: packages/sdk/README.md\n"
        self.assertEqual(list(problems(self.STALE, body)), ["packages/sdk/README.md"])

    def test_ticks_hidden_in_comments_or_code_dont_count(self):
        body = "<!-- - [x] Still accurate: docs/architecture/signals.md -->\n```\n- [x] Still accurate: packages/sdk/README.md\n```"
        self.assertEqual(sorted(problems(self.STALE, body)), sorted(self.STALE))

    def test_a_tick_after_an_unclosed_comment_doesnt_count(self):
        body = "- [x] Still accurate: packages/sdk/README.md\n<!-- note\n- [x] Still accurate: docs/architecture/signals.md"
        self.assertEqual(list(problems(self.STALE, body)), ["docs/architecture/signals.md"])

    def test_backticks_and_capital_x(self):
        body = "* [X] Still accurate: `packages/sdk/README.md`\n- [x] Still accurate: docs/architecture/signals.md"
        self.assertEqual(problems(self.STALE, body), {})


if __name__ == "__main__":
    unittest.main()
