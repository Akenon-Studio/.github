"""Tests for scripts/action_pins.py: every action a PR adds is pinned to a commit on a known branch,
with fake API data and a scratch git repo. Run: python3 -m unittest discover tests"""

import pathlib
import subprocess
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from action_pins import allowed_branches, check, classify, error, run, uses_values  # noqa: E402

GOOD = "a" * 40
TAG_ONLY = "b" * 40
RELEASE = "c" * 40
TIP = "e" * 40


def fake_api(branches, missing=(), tags=None):
    """A GET function over fake repos. `branches` maps owner/repo to {branch: [commits, oldest
    first]}, the first branch being the default. `tags` maps owner/repo to {tag name: commit}: the
    compare API would resolve a bare name to a tag first, so the check must not pass one. Repos in
    `missing` answer 404."""
    tags = tags or {}

    def get(path):
        parts = path.split("/")
        repo = f"{parts[1]}/{parts[2]}"
        if repo in missing:
            raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
        if len(parts) == 3:
            return {"default_branch": next(iter(branches[repo]))}
        if parts[3] == "branches":
            name = "/".join(parts[4:]).replace("%2F", "/")
            if name not in branches[repo]:
                raise urllib.error.HTTPError(path, 404, "Branch not found", {}, None)
            return {"commit": {"sha": branches[repo][name][-1]}}
        base, sha = parts[4].split("...")
        base = tags.get(repo, {}).get(base, base)
        history = next((c for c in branches[repo].values() if base in c), [])
        if sha == base:
            return {"status": "identical"}
        if sha in history and history.index(sha) < history.index(base):
            return {"status": "behind"}
        return {"status": "ahead" if sha.startswith("f") else "diverged"}
    return get


class UsesValues(unittest.TestCase):
    def test_every_yaml_spelling(self):
        text = f"""
jobs:
  a:
    uses: org/reusable/.github/workflows/x.yml@{GOOD}
    steps:
      - uses: plain/one@v1
      - "uses": quoted/key@v2
      - uses : spaced/colon@v3
      - uses: >-
          folded/value@v4
      - {{uses: flow/map@v5}}
      - &step
        uses: anchored/one@v6
      - *step
      # - uses: commented/out@v7
      - run: 'echo "uses: in/a-string@v8"'
"""
        self.assertEqual(set(uses_values(text)), {
            f"org/reusable/.github/workflows/x.yml@{GOOD}", "plain/one@v1", "quoted/key@v2",
            "spaced/colon@v3", "folded/value@v4", "flow/map@v5", "anchored/one@v6"})

    def test_line_numbers(self):
        self.assertEqual(uses_values("steps:\n  - run: x\n  - uses: a/b@v1\n"), {"a/b@v1": 3})


class Classify(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(classify(f"Actions/Checkout@{GOOD.upper()}"), (("actions/checkout", GOOD), None))
        self.assertEqual(classify(f"o/r/sub/dir@{GOOD}"), (("o/r", GOOD), None))
        self.assertEqual(classify("./local"), (None, None))
        self.assertEqual(classify("docker://alpine:3"), (None, None))
        self.assertEqual(classify("Akenon-Studio/.github/.github/workflows/pr-title.yml@main"), (None, None))
        self.assertIn("tag or branch", classify("o/r@v4")[1])
        self.assertIn("short commit", classify("o/r@3d3c42e")[1])
        self.assertIn("can read", classify("not a ref")[1])


class Check(unittest.TestCase):
    def setUp(self):
        self.get = fake_api({
            "actions/checkout": {"main": [GOOD, TIP]},
            "some/action": {"main": [TIP]},
            "rel/action": {"master": [TIP], "releases/v4": [RELEASE]},
        })

    def problems(self, pins, extra=None, get=None):
        return {pin[0]: problem for pin, _, problem, _ in check(get or self.get, pins, extra or {})}

    def test_default_branch_passes_tag_only_fails(self):
        problems = self.problems({("actions/checkout", GOOD): ["a:1"], ("some/action", TAG_ONLY): ["b:1"]})
        self.assertIsNone(problems["actions/checkout"])
        self.assertIn("pushed only to a tag", problems["some/action"])

    def test_a_listed_branch_passes_only_when_listed(self):
        pins = {("rel/action", RELEASE): ["a:1"]}
        self.assertIn("default branch (master)", self.problems(pins)["rel/action"])
        self.assertIsNone(self.problems(pins, {"rel/action": ["releases/v4"]})["rel/action"])

    def test_a_deleted_listed_branch_doesnt_hide_a_later_one(self):
        pins = {("rel/action", RELEASE): ["a:1"]}
        self.assertIsNone(self.problems(pins, {"rel/action": ["releases/v3", "releases/v4"]})["rel/action"])

    def test_a_fork_commit_ahead_of_the_branch_fails(self):
        fork = "f" * 40
        self.assertIn("pushed only to a tag", self.problems({("actions/checkout", fork): ["a:1"]})["actions/checkout"])

    def test_a_tag_named_like_the_branch_cant_stand_in(self):
        get = fake_api({"some/action": {"main": [TIP]}}, tags={"some/action": {"main": TAG_ONLY}})
        self.assertIn("pushed only to a tag", self.problems({("some/action", TAG_ONLY): ["a:1"]}, get=get)["some/action"])

    def test_a_commit_it_cant_look_up_fails(self):
        get = fake_api({}, missing={"actions/checkout"})
        self.assertIn("couldn't look it up", self.problems({("actions/checkout", GOOD): ["a:1"]}, get=get)["actions/checkout"])


class Run(unittest.TestCase):
    """The whole check over a scratch git repo: only `uses:` values new at the head count."""

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)

    def write(self, path, text):
        (self.root / path).parent.mkdir(parents=True, exist_ok=True)
        (self.root / path).write_text(text)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.write(".github/workflows/ci.yml", "jobs:\n  a:\n    steps:\n      - uses: old/tag@v1\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root, capture_output=True,
                                   text=True).stdout.strip()

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-qm", "head")

    def test_new_values_only_in_workflows_and_actions(self):
        self.write(".github/workflows/ci.yml",
                   f"jobs:\n  a:\n    steps:\n      - uses: old/tag@v1\n      - uses: actions/checkout@{GOOD}\n")
        self.write(".github/workflows/nightly build.yml", "jobs:\n  b:\n    steps:\n      - \"uses\": new/tag@v2\n")
        self.write(".github/actions/x/action.yml", "runs:\n  steps:\n    - uses: inner/tag@v3\n")
        self.write("docs/example.yml", "uses: not/checked@v4\n")
        self.commit()
        lines, ok = run(self.root, self.base, fake_api({"actions/checkout": {"main": [GOOD]}}), {})
        self.assertFalse(ok)
        text = "\n".join(lines)
        self.assertIn(f"OK actions/checkout@{GOOD[:12]}: on main", text)
        self.assertIn("file=.github/workflows/nightly build.yml,line=4::new/tag@v2", text)
        self.assertIn("file=.github/actions/x/action.yml,line=3::inner/tag@v3", text)
        self.assertNotIn("old/tag", text)
        self.assertNotIn("not/checked", text)

    def test_nothing_new_passes(self):
        self.write("README.md", "hi\n")
        self.commit()
        self.assertEqual(run(self.root, self.base, fake_api({}), {}), (["No outside action added or changed."], True))

    def test_a_symlinked_workflows_folder_fails(self):
        (self.root / "elsewhere").mkdir()
        (self.root / "elsewhere/x.yml").write_text("jobs:\n  a:\n    steps:\n      - uses: evil/a@v1\n")
        self.git("rm", "-rq", ".github")
        (self.root / ".github").symlink_to("elsewhere")
        self.commit()
        lines, ok = run(self.root, self.base, fake_api({}), {})
        self.assertFalse(ok)
        self.assertIn("file=.github::is a symlink", lines[0])

    def test_unreadable_yaml_fails(self):
        self.write(".github/workflows/ci.yml", "jobs: [\n")
        self.commit()
        lines, ok = run(self.root, self.base, fake_api({}), {})
        self.assertFalse(ok)
        self.assertIn("couldn't be read as YAML", lines[0])


class Error(unittest.TestCase):
    def test_pr_text_stays_on_one_line(self):
        self.assertEqual(error("a b,c:d.yml:7", "x\ny 100%"),
                         "::error file=a b%2Cc%3Ad.yml,line=7::x%0Ay 100%25")
        self.assertEqual(error("f.yml", "bad"), "::error file=f.yml::bad")


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
