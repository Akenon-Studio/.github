import importlib.util
import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("verify", ROOT / "scripts" / "verify-settings.py")
import sys
sys.path.insert(0, str(ROOT / "scripts"))
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)
LISTS = {l["path"]: l for l in json.loads((ROOT / "rulesets" / "repo-lists.json").read_text())["lists"]}


class MissingFrom(unittest.TestCase):
    def test_profile_table(self):
        l = LISTS["profile/README.md"]
        text = "| [platform](https://github.com/Akenon-Studio/platform) | x |\n| [.github](https://github.com/Akenon-Studio/.github) | y |"
        self.assertEqual(verify.missing_from(text, ["platform", ".github", "firmware", ".github-private"],
                                             l["pattern"], l["skip"]), ["firmware"])

    def test_design_tree(self):
        l = LISTS["design.md"]
        text = "├── hardware        boards\n├── handbook        docs\n└── .github-private private"
        self.assertEqual(verify.missing_from(text, ["hardware", "firmware", ".github-private"],
                                             l["pattern"], l["skip"]), ["firmware"])

    def test_name_inside_another_word_does_not_count(self):
        l = LISTS["design.md"]
        self.assertEqual(verify.missing_from("├── platformx  x", ["platform"], l["pattern"]), ["platform"])


class CallerTriggers(unittest.TestCase):
    def test_reads_the_types_list(self):
        text = "on:\n  pull_request:\n    types: [opened, edited, assigned]\n"
        self.assertEqual(verify.triggers(text), {"pull_request: opened", "pull_request: edited",
                                                 "pull_request: assigned"})

    def test_every_event_counts(self):
        # .github#90: issue-fields also listens to PR events; a repo without them must show
        text = ("on:\n  issues:\n    types: [opened]\n  # why\n  pull_request_target:  # note\n"
                "    # more\n    types: [closed, opened]\n")
        self.assertEqual(verify.triggers(text), {"issues: opened", "pull_request_target: closed",
                                                 "pull_request_target: opened"})
        self.assertNotEqual(verify.triggers(text), verify.triggers("on:\n  issues:\n    types: [opened]\n"))

    def test_no_event_is_empty(self):
        self.assertEqual(verify.triggers("name: x\n"), set())

    def test_an_event_without_types_counts(self):
        # .github#203: a caller that loses its push trigger must show
        self.assertEqual(verify.triggers("on: push\n"), {"push"})
        self.assertEqual(verify.triggers("on: [push, pull_request]\n"), {"push", "pull_request"})
        self.assertEqual(verify.triggers("on:\n  pull_request:\n  push:\n    branches: [main]\n"),
                         {"pull_request", "push: branch main"})
        self.assertNotEqual(verify.triggers("on:\n  pull_request:\n"),
                            verify.triggers("on:\n  pull_request:\n  push:\n    branches: [main]\n"))

    def test_push_branches_and_dispatch_inputs_count(self):
        peras = ("on:\n  pull_request:\n    types: [opened]\n  push:\n    branches: [main, 'peras/**']\n"
                 "  workflow_dispatch: # why\n    inputs:\n      sha:\n        required: true\n")
        self.assertEqual(verify.triggers(peras), {"pull_request: opened", "push: branch main",
                                                  "push: branch peras/**", "workflow_dispatch: input sha"})
        no_branch = peras.replace(", 'peras/**'", "")
        no_input = peras.replace("    inputs:\n      sha:\n        required: true\n", "")
        self.assertNotEqual(verify.triggers(no_branch), verify.triggers(peras))
        self.assertEqual(verify.triggers(no_input) - verify.triggers(peras), {"workflow_dispatch"})

    def test_a_lone_type_is_a_list_of_one(self):
        self.assertEqual(verify.triggers("on:\n  issue_comment:\n    types: created\n"),
                         {"issue_comment: created"})

    def test_invalid_yaml_never_matches(self):
        self.assertEqual(verify.triggers("on: [\n"), {"(not valid YAML)"})


class ExpectedTriggers(unittest.TestCase):
    """.github#203: the shared checks' callers run on Peras branches and by commit only where Peras
    delivers (design 6.3, 7.13 Stage 10)."""

    def test_a_peras_repos_check_callers_add_the_peras_triggers(self):
        for path in verify.CHECK_CALLERS:
            own = verify.triggers((ROOT / path).read_text())
            for repo in ("peras", "platform", "animations"):
                self.assertEqual(verify.expected_triggers(path, repo), own | verify.PERAS_TRIGGERS)
            for repo in ("handbook", "hardware", "firmware", ".github"):
                self.assertEqual(verify.expected_triggers(path, repo), own)

    def test_the_issue_automation_caller_is_the_same_everywhere(self):
        path = ".github/workflows/issue-fields-caller.yml"
        own = verify.triggers((ROOT / path).read_text())
        self.assertEqual(verify.expected_triggers(path, "peras"), own)
        self.assertEqual(verify.expected_triggers(path, "handbook"), own)

    def test_a_copy_without_its_push_or_dispatch_differs(self):
        path = ".github/workflows/security-scan-caller.yml"
        text = (ROOT / path).read_text()
        self.assertEqual(verify.triggers(text), verify.expected_triggers(path, "handbook"))
        self.assertNotEqual(verify.triggers(text), verify.expected_triggers(path, "platform"))
        no_push = text.replace("  push:\n    branches: [main]\n", "")
        self.assertNotEqual(verify.triggers(no_push), verify.expected_triggers(path, "handbook"))

    def test_a_correct_peras_copy_matches(self):
        # R1.1: this repo's copy plus the Peras triggers, as peras#169 wrote them
        extra = ("  push:\n    branches: [main, 'peras/**']\n  workflow_dispatch:\n    inputs:\n"
                 "      sha:\n        required: true\n        type: string\n")
        for path in verify.CHECK_CALLERS:
            text = (ROOT / path).read_text().replace("  push:\n    branches: [main]\n", "")
            copy = text.replace("\npermissions:", extra + "\npermissions:", 1)
            self.assertEqual(verify.triggers(copy), verify.expected_triggers(path, "platform"), path)

    def test_a_bare_event_here_is_narrowed_in_a_peras_repo(self):
        # R1.1: a bare `workflow_dispatch:` or `push:` here mustn't make every Peras copy drift
        path = verify.CHECK_CALLERS[0]
        original = verify.triggers
        verify.triggers = lambda text: {"pull_request", "push", "workflow_dispatch"}
        try:
            want = verify.expected_triggers(path, "peras", peras=["peras"])
        finally:
            verify.triggers = original
        self.assertEqual(want, {"pull_request"} | verify.PERAS_TRIGGERS)

    def test_every_peras_repo_is_managed(self):
        managed = [l for l in (ROOT / "rulesets/repos.txt").read_text().split()
                   if l and not l.startswith("#")]
        self.assertTrue(verify.peras_repos())
        self.assertTrue(set(verify.peras_repos()) <= set(managed))

    def test_the_security_scan_caller_is_checked(self):
        self.assertIn(".github/workflows/security-scan-caller.yml", verify.CALLERS)

    def test_this_repos_pr_caller_reruns_on_assignment(self):
        text = (ROOT / ".github/workflows/pr-title-caller.yml").read_text()
        self.assertTrue({"pull_request: assigned", "pull_request: unassigned"} <= verify.triggers(text))


if __name__ == "__main__":
    unittest.main()
