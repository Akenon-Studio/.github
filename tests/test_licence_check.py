"""Tests for scripts/licence_check.py (design 6.4). Run: python3 -m unittest discover tests"""

import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
from licence_check import RULES, problems, satisfied  # noqa: E402

ALLOWED = {"MIT", "ISC", "Apache-2.0"}.__contains__
RULE_SET = {
    "allowed": ["MIT", "ISC"],
    "exceptions": [{"package": "@img/sharp-libvips-*", "licence": "LGPL-3.0-or-later",
                    "why": "test", "approved_in": "test"}],
}


def listing(**by_licence):
    return {lic: [{"name": n, "versions": ["1.0.0"]} for n in names]
            for lic, names in by_licence.items()}


class SatisfiedTest(unittest.TestCase):
    def test_single_ids(self):
        self.assertTrue(satisfied("MIT", ALLOWED))
        self.assertFalse(satisfied("GPL-3.0-only", ALLOWED))

    def test_or_needs_one_side(self):
        self.assertTrue(satisfied("(WTFPL OR MIT)", ALLOWED))
        self.assertTrue(satisfied("WTFPL OR ISC", ALLOWED))
        self.assertFalse(satisfied("WTFPL OR GPL-2.0-only", ALLOWED))

    def test_and_needs_every_side(self):
        self.assertTrue(satisfied("MIT AND ISC", ALLOWED))
        self.assertFalse(satisfied("MIT AND GPL-3.0-only", ALLOWED))

    def test_and_binds_tighter_than_or(self):
        self.assertTrue(satisfied("GPL-3.0-only OR MIT AND ISC", ALLOWED))
        self.assertFalse(satisfied("(GPL-3.0-only OR MIT) AND WTFPL", ALLOWED))

    def test_with_and_plus_keep_the_licence(self):
        self.assertTrue(satisfied("Apache-2.0 WITH LLVM-exception", ALLOWED))
        self.assertTrue(satisfied("MIT+", ALLOWED))

    def test_unknown_or_broken_fails(self):
        for expr in ["", "Unknown", "(MIT", "MIT OR"]:
            self.assertFalse(satisfied(expr, ALLOWED), expr)


class ProblemsTest(unittest.TestCase):
    def test_allowed_and_excepted_pass(self):
        found = problems(listing(**{"MIT": ["a"], "(WTFPL OR MIT)": ["b"],
                                    "LGPL-3.0-or-later": ["@img/sharp-libvips-linux-x64"]}),
                         RULE_SET)
        self.assertEqual(found, [])

    def test_exception_is_for_its_package_only(self):
        found = problems(listing(**{"LGPL-3.0-or-later": ["some-other-lib"]}), RULE_SET)
        self.assertEqual(found, ["some-other-lib 1.0.0: LGPL-3.0-or-later"])

    def test_disallowed_and_unknown_fail(self):
        found = problems(listing(**{"GPL-3.0-only": ["x"], "Unknown": ["y"]}), RULE_SET)
        self.assertEqual(found, ["x 1.0.0: GPL-3.0-only", "y 1.0.0: Unknown"])


class RulesFileTest(unittest.TestCase):
    def test_every_exception_says_why_and_where_approved(self):
        rules = json.loads(RULES.read_text())
        for e in rules["exceptions"]:
            self.assertEqual(set(e), {"package", "licence", "why", "approved_in"}, e)
            self.assertTrue(e["why"].strip() and e["approved_in"].strip(), e)
            self.assertNotIn(e["licence"], rules["allowed"], e)

    def test_no_copyleft_in_the_allowed_list(self):
        allowed = json.loads(RULES.read_text())["allowed"]
        self.assertFalse([a for a in allowed if a.startswith(("GPL", "AGPL", "LGPL", "SSPL"))])


if __name__ == "__main__":
    unittest.main()
