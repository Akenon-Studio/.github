"""Tests for scripts/bot_pr_review.py: which open PRs the automation owners are asked to review.
Run: python3 -m unittest discover tests"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from bot_pr_review import needs_review  # noqa: E402

MANAGED = {"platform", "handbook"}


def pr(author="Bot", asked=(), repo="Akenon-Studio/platform"):
    return {"number": 1, "author": {"__typename": author, "login": "renovate"},
            "repository": {"nameWithOwner": repo},
            "timelineItems": {"nodes": [{"requestedReviewer": {"__typename": "Team", "slug": s}}
                                        for s in asked]}}


class NeedsReviewTest(unittest.TestCase):
    def test_bot_pr_never_asked(self):
        self.assertTrue(needs_review(pr(), MANAGED))

    def test_bot_pr_already_asked(self):
        self.assertFalse(needs_review(pr(asked=["automation-owners"]), MANAGED))

    def test_another_team_asked_still_needs_ours(self):
        self.assertTrue(needs_review(pr(asked=["engineers"]), MANAGED))

    def test_a_persons_pr_is_left_alone(self):
        self.assertFalse(needs_review(pr(author="User"), MANAGED))

    def test_only_managed_repos_in_the_org(self):
        self.assertFalse(needs_review(pr(repo="Akenon-Studio/old-thing"), MANAGED))
        self.assertFalse(needs_review(pr(repo="someone/platform"), MANAGED))

    def test_a_person_requested_is_not_the_team(self):
        p = pr()
        p["timelineItems"]["nodes"].append({"requestedReviewer": {"__typename": "User"}})
        p["timelineItems"]["nodes"].append({"requestedReviewer": None})
        self.assertTrue(needs_review(p, MANAGED))


if __name__ == "__main__":
    unittest.main()
