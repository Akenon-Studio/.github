"""Tests for scripts/design_refs.py: the daily check of design sections named in every repo's
CLAUDE.md. Run: python3 -m unittest discover tests"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from design_refs import LABEL, TITLE, body, check, claude_md_paths, problems  # noqa: E402
from issue_fields import check as form_check, load_forms  # noqa: E402
from one_issue import new_issue  # noqa: E402
from rules import desired_labels  # noqa: E402

DESIGN = "# Design\n\n## 4. Repos\n\n### 4.2 Layout\n\n## 6. Standards\n\n### 6.8 Issues\n"
REPOS = {
    "handbook": {"design.md": DESIGN},
    "peras": {"core/workspace/CLAUDE.md": "- Rules per design 6.8. not code because: x\n",
              "README.md": "design 9.9 is not checked here"},
    "platform": {"CLAUDE.md": "# Rules\n\n- Layout: design 4.2 and 4.7. not code because: x\n"
                              "- Old: section 5.1. not code because: y\n",
                 "node_modules/x/CLAUDE.md": "design 9.9"},
}


def read(repo, path):
    return REPOS.get(repo, {}).get(path)


def files(repo):
    return set(REPOS[repo]) if repo in REPOS else None


OUTPUT = """FAIL: design sections named in CLAUDE.md files that the design no longer has:
  - platform/CLAUDE.md: line 3: design section 4.7 does not exist.
"""


class DesignRefsTest(unittest.TestCase):
    def test_finds_claude_md_files_outside_node_modules(self):
        self.assertEqual(claude_md_paths({"CLAUDE.md", "a/CLAUDE.md", "node_modules/b/CLAUDE.md",
                                          "NOT-CLAUDE.md", "README.md"}), ["CLAUDE.md", "a/CLAUDE.md"])

    def test_reports_only_dead_design_sections(self):
        found, unreadable = check(read, files, ["peras", "platform"])
        self.assertEqual(found, ["platform/CLAUDE.md: line 3: design section 4.7 does not exist.",
                                 "platform/CLAUDE.md: line 4: design section 5.1 does not exist."])
        self.assertEqual(unreadable, [])

    def test_a_repo_it_cannot_read_is_named(self):
        self.assertEqual(check(read, files, ["peras", "secret"]), ([], ["secret"]))

    def test_no_design_means_nothing_is_checked(self):
        self.assertEqual(check(lambda r, p: None, files, ["peras"]), ([], ["handbook"]))

    def test_body_lists_each_reference(self):
        self.assertEqual(problems(OUTPUT),
                         ["platform/CLAUDE.md: line 3: design section 4.7 does not exist."])
        self.assertIn("- `platform/CLAUDE.md`: line 3: design section 4.7 does not exist.",
                      body(OUTPUT))

    def test_body_is_a_complete_task_form(self):
        # so the issue-fields automation files it on the board without needs-fields
        self.assertEqual(form_check("Task", body(OUTPUT), load_forms()), [])

    def test_issue_carries_its_source_label(self):
        made = new_issue(TITLE, LABEL, body(OUTPUT))
        self.assertEqual(made["labels"], ["claude-md-drift"])
        self.assertEqual(made["assignees"], ["RuvinduH"])
        self.assertIn(LABEL, {l["name"] for l in desired_labels(".github")})


if __name__ == "__main__":
    unittest.main()
