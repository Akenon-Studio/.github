"""Tests for scripts/action_pins.py: every action commit a PR adds is on a known branch, with fake
API data. Run: python3 -m unittest discover tests"""

import pathlib
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from action_pins import added_pins, allowed_branches, check  # noqa: E402

GOOD = "a" * 40
TAG_ONLY = "b" * 40
RELEASE = "c" * 40

DIFF = f"""diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -10,1 +10,2 @@ jobs:
-      - uses: actions/checkout@{'0' * 40} # v6
+      - uses: actions/checkout@{GOOD} # v7
+      - uses: Some/Action/sub/dir@{TAG_ONLY}
@@ -30,0 +31,3 @@
+      - uses: ./local-action
+      - uses: docker://alpine@sha256:{'d' * 64}
+      - uses: other/action@v4
+      - uses: Akenon-Studio/.github/.github/workflows/pr-title.yml@main
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,0 +2,1 @@
+uses: not/yaml@{GOOD}
diff --git a/action.yaml b/action.yaml
--- a/action.yaml
+++ b/action.yaml
@@ -0,0 +1,1 @@
+    steps: [{{ uses: "rel/action@{RELEASE}" }}]
"""


def fake_api(branches, missing=()):
    """A GET function over fake repos: `branches` maps owner/repo to {branch: [commits on it]},
    the first branch being the default. Repos in `missing` answer 404."""
    def get(path):
        parts = path.split("/")
        repo = f"{parts[1]}/{parts[2]}"
        if repo in missing:
            raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
        if len(parts) == 3:
            return {"default_branch": next(iter(branches[repo]))}
        branch, sha = parts[4].split("...")
        on = branches[repo].get(branch.replace("%2F", "/"), [])
        return {"status": "identical" if on and on[-1] == sha else "behind" if sha in on else "diverged"}
    return get


class AddedPins(unittest.TestCase):
    def test_reads_only_added_commit_pins_in_yaml(self):
        pins = added_pins(DIFF)
        self.assertEqual(pins, {
            ("actions/checkout", GOOD): [".github/workflows/ci.yml:10"],
            ("some/action", TAG_ONLY): [".github/workflows/ci.yml:11"],
            ("rel/action", RELEASE): ["action.yaml:1"],
            ("other/action", "v4"): [".github/workflows/ci.yml:33"],
        })


class Check(unittest.TestCase):
    def setUp(self):
        self.get = fake_api({
            "actions/checkout": {"main": [GOOD, "e" * 40]},
            "some/action": {"main": ["e" * 40]},
            "rel/action": {"master": [], "releases/v4": [RELEASE]},
        })

    def problems(self, extra):
        return {pin[0]: problem for pin, _, problem, _ in check(self.get, added_pins(DIFF), extra)}

    def test_default_branch_passes_tag_only_fails(self):
        problems = self.problems({})
        self.assertIsNone(problems["actions/checkout"])
        self.assertIn("pushed only to a tag", problems["some/action"])
        self.assertIn("default branch (master)", problems["rel/action"])

    def test_a_tag_fails_without_a_lookup(self):
        self.assertIn("a tag or branch name", self.problems({})["other/action"])

    def test_a_listed_branch_passes(self):
        self.assertIsNone(self.problems({"rel/action": ["releases/v4"]})["rel/action"])

    def test_a_commit_it_cant_look_up_fails(self):
        get = fake_api({}, missing={"actions/checkout"})
        [(_, _, problem, _)] = check(get, {("actions/checkout", GOOD): ["x.yml:1"]}, {})
        self.assertIn("couldn't look it up", problem)


class AllowedBranches(unittest.TestCase):
    def test_missing_file_allows_nothing_more(self):
        self.assertEqual(allowed_branches(pathlib.Path("/nonexistent/action-branches.json")), {})

    def test_reads_actions_lowercased(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "action-branches.json"
            path.write_text('{"_comment": "x", "actions": {"Rel/Action": ["releases/v4"]}}')
            self.assertEqual(allowed_branches(path), {"rel/action": ["releases/v4"]})


if __name__ == "__main__":
    unittest.main()
