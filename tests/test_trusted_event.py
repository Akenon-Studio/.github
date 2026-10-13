"""Tests for scripts/trusted_event.py: in a public repo, only members' and the org's bots' events
reach the job holding the Automation app's key (design 6.2)."""

import json
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import trusted_event  # noqa: E402

MEMBER = {"login": "RuvinduH", "type": "User"}
OUTSIDER = {"login": "someone", "type": "User"}
OUR_BOT = {"login": "akenon-studio-automation[bot]", "type": "Bot"}
MERGE_LANE = {"login": "akenon-studio-merge-lane[bot]", "type": "Bot"}
OTHER_BOT = {"login": "someapp[bot]", "type": "Bot"}


def event(author, sender=None, comment=None, private=False, kind="issue"):
    e = {"repository": {"private": private}, kind: {"user": author}, "sender": sender or author}
    if comment:
        e["comment"] = {"user": comment}
    return e


class TrustedEventTest(unittest.TestCase):
    def test_members_and_our_bots_in_a_public_repo(self):
        self.assertTrue(trusted_event.trusted(event(MEMBER)))
        self.assertTrue(trusted_event.trusted(event(OUR_BOT)))
        self.assertTrue(trusted_event.trusted(event(MEMBER, kind="pull_request")))
        # the merge lane merging a member's PR
        self.assertTrue(trusted_event.trusted(event(MEMBER, sender=MERGE_LANE, kind="pull_request")))

    def test_outsiders_in_a_public_repo(self):
        self.assertFalse(trusted_event.trusted(event(OUTSIDER)))
        self.assertFalse(trusted_event.trusted(event(OTHER_BOT)))
        self.assertFalse(trusted_event.trusted(event(OUTSIDER, kind="pull_request")))

    def test_a_member_acting_on_an_outsiders_issue(self):
        # the outsider's text would still reach the job
        self.assertFalse(trusted_event.trusted(event(OUTSIDER, sender=MEMBER)))
        self.assertFalse(trusted_event.trusted(event(OUTSIDER, sender=MEMBER, comment=MEMBER)))

    def test_an_outsiders_comment_on_a_members_issue(self):
        self.assertFalse(trusted_event.trusted(event(MEMBER, sender=OUTSIDER, comment=OUTSIDER)))

    def test_a_missing_author_is_not_trusted(self):
        self.assertFalse(trusted_event.trusted(event(None, sender=MEMBER)))

    def test_anyone_in_a_private_repo(self):
        self.assertTrue(trusted_event.trusted(event(OUTSIDER, private=True)))

    def test_logins_match_whatever_the_case(self):
        self.assertTrue(trusted_event.trusted(event({"login": "ruvinduh", "type": "User"})))

    def test_main_writes_the_output(self):
        with tempfile.TemporaryDirectory() as d:
            path, out = pathlib.Path(d, "event.json"), pathlib.Path(d, "out")
            path.write_text(json.dumps(event(OUTSIDER)))
            os.environ["GITHUB_OUTPUT"] = str(out)
            try:
                self.assertEqual(trusted_event.main([str(path)]), 0)
            finally:
                del os.environ["GITHUB_OUTPUT"]
            self.assertEqual(out.read_text(), "trusted=false\n")


if __name__ == "__main__":
    unittest.main()
