"""Tests for scripts/claude_md.py: the CLAUDE.md cap, a reason on every rule, no dead references.
Run: python3 -m unittest discover tests"""

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from claude_md import known_checks, problems, rules  # noqa: E402

CHECKS = known_checks("platform", [ROOT / ".github" / "workflows"])
SECTIONS = {"6", "6.6", "6.8"}

GOOD = """# CLAUDE.md: example

Read the design first. not code because: it is a pointer to design 6.8

## Rules

- Every PR closes an issue. enforced by: `pr-title / closes-issue`
- Scripts live in `scripts/rules.py` and friends; enforced by: `scripts/issue_fields.py`
- Wrapped rules are fine when the reason ends the item,
  not code because: taste can't be checked (see `tests/`)

```
├── a code block is not a rule
```
<!-- comments are not rules -->
"""


def run(text, sections=SECTIONS, in_ci=False):
    return problems(text, ROOT, ROOT, CHECKS, sections, in_ci)


class PassesTest(unittest.TestCase):
    def test_good_file_passes(self):
        self.assertEqual(run(GOOD), [])

    def test_sixty_lines_pass(self):
        self.assertEqual(run("# Title\n" + "\n" * 59), [])

    def test_known_checks_include_required_checks_and_jobs(self):
        self.assertIn("pr-title / checks", CHECKS)
        self.assertIn("pr-title / closes-issue", CHECKS)  # a step of that job
        self.assertIn("unit", CHECKS)  # tests.yml job
        self.assertIn("checks", CHECKS)  # platform's required check

    def test_home_paths_are_not_checked_in_ci(self):
        self.assertEqual(run("- See `~/nowhere/at/all.md`. not code because: a pointer", in_ci=True), [])

    def test_placeholders_and_commands_are_not_paths(self):
        text = "- Run `pnpm bundle animations/<name>` with `GH=/x/gh`. not code because: a command"
        self.assertEqual(run(text), [])

    def test_design_sections_skipped_without_the_design(self):
        self.assertEqual(run("- Per design 9.9. not code because: pointer", sections=None), [])


class FailsTest(unittest.TestCase):
    def test_over_sixty_lines(self):
        found = run("# Title\n" + "\n" * 60)
        self.assertEqual(len(found), 1)
        self.assertIn("61 lines", found[0])

    def test_rule_without_a_reason(self):
        self.assertEqual(run("- Always push after commit."),
                         ["line 1: ends with neither `enforced by:` nor `not code because:`."])

    def test_paragraph_without_a_reason(self):
        self.assertEqual(len(run("Some context with no reason.")), 1)

    def test_enforced_by_nothing_in_backticks(self):
        self.assertIn("names no check", run("- Push often. enforced by: the hooks")[0])

    def test_enforced_by_a_missing_script(self):
        found = run("- Push often. enforced by: `scripts/gone.py`")
        self.assertIn("neither a known check nor an existing script", found[0])

    def test_enforced_by_an_unknown_check(self):
        self.assertIn("`made-up / check`", run("- X. enforced by: `made-up / check`")[0])

    def test_dead_path(self):
        self.assertEqual(run("- See `docs/gone.md`. not code because: a pointer"),
                         ["line 1: `docs/gone.md` does not exist."])

    def test_dead_home_path_locally(self):
        self.assertEqual(len(run("- See `~/no/such/file.md`. not code because: a pointer")), 1)

    def test_dead_design_section(self):
        found = run("- Per design 6.6 and 9.9. not code because: pointer")
        self.assertEqual(found, ["line 1: design section 9.9 does not exist."])

    def test_dead_section_in_a_list_of_sections(self):
        self.assertEqual(len(run("- Per design 6.8, 7.13. not code because: pointer")), 1)


class RulesTest(unittest.TestCase):
    def test_wrapped_item_is_one_rule(self):
        self.assertEqual(rules("- one\n  two\n- three"), [(1, "- one two"), (3, "- three")])

    def test_headings_code_and_comments_are_not_rules(self):
        self.assertEqual(rules("# H\n```\n- x\n```\n<!-- - y\n-->\n"), [])


class WorkflowFileTest(unittest.TestCase):
    def test_a_check_named_by_a_repo_workflow_counts(self):
        with tempfile.TemporaryDirectory() as d:
            (pathlib.Path(d) / "ci.yml").write_text("name: ci\njobs:\n  lint: {}\n")
            checks = known_checks("platform", [d])
        self.assertIn("lint", checks)
        self.assertIn("ci / lint", checks)


if __name__ == "__main__":
    unittest.main()
