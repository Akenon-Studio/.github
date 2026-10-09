"""Tests for rulesets/bots.json and the closes-issue exemption it drives (design 6.8).
Run: python3 -m unittest discover tests"""

import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
from pr_closes_issue import exempt  # noqa: E402
from rules import RULES, bot_login, bot_sources  # noqa: E402

SOURCES = {"renovate": {"labels": ["dependencies"], "no_issue": True},
           "akenon-studio-automation": {"labels": ["settings-drift"], "no_issue": False}}


class ExemptTest(unittest.TestCase):
    def test_renovate_needs_no_issue(self):
        self.assertTrue(exempt("renovate[bot]", "Bot", SOURCES))

    def test_other_bots_and_people_still_close_an_issue(self):
        self.assertFalse(exempt("akenon-studio-automation[bot]", "Bot", SOURCES))
        self.assertFalse(exempt("peras[bot]", "Bot", SOURCES))
        self.assertFalse(exempt("renovate", "User", SOURCES))  # a person named like the bot


class BotsFileTest(unittest.TestCase):
    def test_logins_are_bare_and_lower_case(self):
        for login in bot_sources():
            self.assertEqual(login, bot_login(login))

    def test_every_source_label_exists(self):
        labels = json.loads((RULES / "labels.json").read_text())
        names = {l["name"] for group in labels.values() if isinstance(group, list) for l in group}
        for login, source in bot_sources().items():
            self.assertTrue(source["labels"], login)
            self.assertLessEqual(set(source["labels"]), names, login)


if __name__ == "__main__":
    unittest.main()
