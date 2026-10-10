"""Tests for scripts/pr_closes_issue.py: a PR closes at least one issue, and only whole issues.
Run: python3 -m unittest discover tests"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from pr_closes_issue import CLOSES, only_bot_commits, problems, refs  # noqa: E402

REPO = "Akenon-Studio/platform"


class PassesTest(unittest.TestCase):
    def test_closes_an_issue_in_this_repo(self):
        self.assertEqual(problems("Closes #12", REPO), [])

    def test_closes_an_issue_in_another_repo(self):
        self.assertEqual(problems("Closes Akenon-Studio/handbook#67\n\nMore text.", REPO), [])

    def test_every_closing_keyword(self):
        for word in ["close", "closes", "closed", "fix", "fixes", "fixed", "resolve", "resolves",
                     "resolved", "CLOSES", "Fixes:"]:
            self.assertEqual(problems(f"{word} #3", REPO), [], word)

    def test_issue_url(self):
        self.assertEqual(problems("Fixes https://github.com/Akenon-Studio/peras/issues/4", REPO), [])

    def test_part_of_an_issue_it_also_closes(self):
        self.assertEqual(problems("Closes #5. This is part of #5's plan.", REPO), [])

    def test_closing_the_same_issue_by_short_and_long_name(self):
        self.assertEqual(problems("Closes #5, part of akenon-studio/platform#5", REPO), [])


class FailsTest(unittest.TestCase):
    def test_empty_body(self):
        self.assertEqual(len(problems("", REPO)), 1)
        self.assertEqual(len(problems(None, REPO)), 1)

    def test_template_left_blank(self):
        self.assertIn("closes no issue", problems("## What and why\n\nCloses #\n", REPO)[0])

    def test_mention_is_not_closing(self):
        self.assertIn("closes no issue", problems("See #12 and handbook#3", REPO)[0])

    def test_part_of_alone(self):
        found = problems("Part of #24", REPO)
        self.assertEqual(len(found), 2)
        self.assertIn("closes no issue", found[0])
        self.assertIn("akenon-studio/platform#24", found[1])

    def test_closes_one_but_does_part_of_another(self):
        for phrase in ["part of", "Partly", "partially", "towards", "part of issue"]:
            found = problems(f"Closes #5\n\nThis is {phrase} Akenon-Studio/handbook#24.", REPO)
            self.assertEqual(len(found), 1, phrase)
            self.assertIn("akenon-studio/handbook#24", found[0])
            self.assertIn("Split the issue", found[0])

    def test_keyword_inside_an_html_comment_does_not_count(self):
        self.assertIn("closes no issue", problems("<!-- Closes #1 -->", REPO)[0])

    def test_keyword_inside_code_does_not_count(self):
        self.assertIn("closes no issue", problems("Write `Closes #1` in the body", REPO)[0])
        self.assertIn("closes no issue", problems("```\nCloses #1\n```", REPO)[0])

    def test_part_of_inside_code_is_a_quote_not_a_claim(self):
        self.assertEqual(problems("Closes #5. It rejects `Part of #24` bodies.", REPO), [])

    def test_keyword_must_be_a_whole_word(self):
        self.assertIn("closes no issue", problems("prefixes #4", REPO)[0])


class RefsTest(unittest.TestCase):
    def test_refs_are_normalised(self):
        self.assertEqual(refs(CLOSES, "Closes #1, closes Akenon-Studio/Handbook#2", REPO),
                         ["akenon-studio/platform#1", "akenon-studio/handbook#2"])



class BotCommitsTest(unittest.TestCase):
    BOT = "akenon-studio-release[bot]"

    @staticmethod
    def commit(login, verified=True, parents=1):
        return {"author": {"login": login} if login else None, "parents": [{}] * parents,
                "commit": {"verification": {"verified": verified}}}

    def test_a_bots_own_signed_commits_only(self):
        bot = self.commit(self.BOT)
        self.assertTrue(only_bot_commits([bot, bot], self.BOT))
        # someone pushed their work onto the bot's branch: not exempt (.github#112)
        self.assertFalse(only_bot_commits([bot, self.commit("someone")], self.BOT))
        self.assertFalse(only_bot_commits([self.commit(None)], "renovate[bot]"))  # an unlinked email
        self.assertFalse(only_bot_commits([self.commit(self.BOT, verified=False)], self.BOT))  # a forged email
        self.assertFalse(only_bot_commits([], self.BOT))

    def test_merging_main_in_doesnt_count(self):
        merge = self.commit("akenon-studio-merge-lane[bot]", verified=False, parents=2)
        self.assertTrue(only_bot_commits([self.commit(self.BOT), merge], self.BOT))
        self.assertFalse(only_bot_commits([merge], self.BOT))  # nothing of the bot's own


class MainTest(unittest.TestCase):
    """main() as the workflow runs it, with a stub `gh` for the commits."""

    def run_main(self, commits, body="", exit_code=0):
        import json, os, subprocess, tempfile
        with tempfile.TemporaryDirectory() as d:
            gh = os.path.join(d, "gh")
            with open(gh, "w") as f:
                f.write(f"#!/bin/sh\necho '{json.dumps([commits])}'\nexit {exit_code}\n")
            os.chmod(gh, 0o755)
            env = dict(os.environ, GH=gh, PR_BODY=body, PR_AUTHOR=BotCommitsTest.BOT, PR_AUTHOR_TYPE="Bot",
                       PR_NUMBER="5", GITHUB_REPOSITORY="Akenon-Studio/platform")
            script = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "pr_closes_issue.py"
            return subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True)

    def test_a_bots_own_pr_passes_without_closes(self):
        p = self.run_main([BotCommitsTest.commit(BotCommitsTest.BOT)])
        self.assertEqual(p.returncode, 0, p.stdout)

    def test_a_persons_commit_on_it_needs_closes(self):
        p = self.run_main([BotCommitsTest.commit(BotCommitsTest.BOT), BotCommitsTest.commit("someone")])
        self.assertEqual(p.returncode, 1)
        self.assertIn("not every commit is its own", p.stdout)
        self.assertEqual(self.run_main([BotCommitsTest.commit("someone")], body="Closes #3").returncode, 0)

    def test_unreadable_commits_say_so(self):
        p = self.run_main([], exit_code=1)
        self.assertEqual(p.returncode, 1)
        self.assertIn("couldn't read the PR's commits", p.stdout)

if __name__ == "__main__":
    unittest.main()
