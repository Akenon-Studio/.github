"""Tests for scripts/pr_not_stacked.py: a PR is not stacked on another open PR, with fake API data.
Run: python3 -m unittest discover tests"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from pr_not_stacked import check  # noqa: E402

REPO = "akenon-studio/animations"


def pr(number, head_sha, ref, base="main", title=None):
    return {"number": number, "title": title or f"feat: pr {number}", "head": {"sha": head_sha,
            "ref": ref}, "base": {"ref": base}}


def fake_api(prs, branches):
    """A GET function over fake data. `branches` maps a PR number to its own commits, oldest
    first, as its compare against its base lists them. Records each path asked for."""
    by_head = {p["head"]["sha"]: p["number"] for p in prs}
    calls = []

    def get(path):
        calls.append(path)
        if path.startswith(f"repos/{REPO}/pulls?state=open"):
            return prs
        if "/compare/" in path:
            return {"commits": [{"sha": s} for s in branches[by_head[path.rsplit("...", 1)[1]]]]}
        if path.startswith(f"repos/{REPO}/pulls/") and "/commits" in path:
            return [{"sha": s} for s in branches[int(path.split("/")[4])]]
        raise AssertionError(f"unexpected call {path}")
    get.calls = calls
    return get


def run(get, prs, number):
    this = next(p for p in prs if p["number"] == number)
    return check(get, REPO, number, this["base"]["ref"], this["head"]["sha"])


class IndependentTest(unittest.TestCase):
    def test_alone(self):
        prs = [pr(1, "a1", "feat/a")]
        self.assertEqual(run(fake_api(prs, {1: ["a1"]}), prs, 1), [])

    def test_two_independent_prs(self):
        prs = [pr(1, "a2", "feat/a"), pr(2, "b1", "feat/b")]
        get = fake_api(prs, {1: ["a1", "a2"], 2: ["b1"]})
        self.assertEqual(run(get, prs, 1), [])
        self.assertEqual(run(get, prs, 2), [])

    def test_the_earlier_pr_is_not_blamed_for_a_later_one_stacked_on_it(self):
        prs = [pr(1, "a1", "feat/a"), pr(2, "b1", "feat/b")]
        get = fake_api(prs, {1: ["a1"], 2: ["a1", "b1"]})
        self.assertEqual(run(get, prs, 1), [])

    def test_a_pr_based_on_the_other_prs_branch_passes(self):
        # its compare is against feat/a, so a1 is on its base and not among its commits
        prs = [pr(1, "a1", "feat/a"), pr(2, "b1", "feat/b", base="feat/a")]
        self.assertEqual(run(fake_api(prs, {1: ["a1"], 2: ["b1"]}), prs, 2), [])


class StackedTest(unittest.TestCase):
    def test_branched_from_the_other_prs_head(self):
        prs = [pr(83, "a1", "feat/a", title="feat: first"), pr(84, "b1", "feat/b")]
        found = run(fake_api(prs, {83: ["a1"], 84: ["a1", "b1"]}), prs, 84)
        self.assertEqual(len(found), 1)
        self.assertIn("stacked on #83", found[0])
        self.assertIn("feat/a", found[0])
        self.assertIn("branch from the latest main", found[0])
        self.assertIn("wait for #83 to merge, then merge main", found[0])

    def test_branched_from_the_middle_of_an_earlier_pr_that_moved_on(self):
        prs = [pr(1, "a2", "feat/a"), pr(2, "b1", "feat/b")]
        get = fake_api(prs, {1: ["a1", "a2"], 2: ["a1", "b1"]})
        self.assertEqual(len(run(get, prs, 2)), 1)
        self.assertEqual(run(get, prs, 1), [])  # the older one passes

    def test_stacked_on_two(self):
        prs = [pr(1, "a1", "feat/a"), pr(2, "b1", "feat/b"), pr(3, "c1", "feat/c")]
        get = fake_api(prs, {1: ["a1"], 2: ["a1", "b1"], 3: ["a1", "b1", "c1"]})
        found = run(get, prs, 3)
        self.assertEqual([f.split(" (")[0] for f in found],
                         ["This PR is stacked on #1", "This PR is stacked on #2"])


class CostTest(unittest.TestCase):
    def test_no_commit_list_when_the_head_already_matches(self):
        prs = [pr(1, "a1", "feat/a"), pr(2, "b1", "feat/b")]
        get = fake_api(prs, {1: ["a1"], 2: ["a1", "b1"]})
        run(get, prs, 2)
        self.assertEqual(len(get.calls), 2)  # one compare, one list of open PRs

    def test_newer_prs_cost_nothing(self):
        prs = [pr(1, "a1", "feat/a"), pr(2, "b1", "feat/b"), pr(3, "c1", "feat/c")]
        get = fake_api(prs, {1: ["a1"], 2: ["b1"], 3: ["c1"]})
        run(get, prs, 1)
        self.assertEqual(len(get.calls), 2)


if __name__ == "__main__":
    unittest.main()
