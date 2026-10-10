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

    def test_no_types_is_empty(self):
        self.assertEqual(verify.triggers("on: push\n"), set())

    def test_this_repos_pr_caller_reruns_on_assignment(self):
        text = (ROOT / ".github/workflows/pr-title-caller.yml").read_text()
        self.assertTrue({"pull_request: assigned", "pull_request: unassigned"} <= verify.triggers(text))


if __name__ == "__main__":
    unittest.main()
