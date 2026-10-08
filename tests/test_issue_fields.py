"""Tests for scripts/issue_fields.py: reading the issue forms, parsing issue bodies, and checking
them. Run: python3 -m unittest discover tests"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from issue_fields import (board_values, check, link_problems, load_forms, open_sub_issues,  # noqa: E402
                          parse_body, problems_text, reopen_text)

FORMS = load_forms()


def body(**answers):
    """An issue body as GitHub renders a submitted form: '### Label' then the answer."""
    return "\n\n".join(f"### {label}\n\n{value}" for label, value in answers.items())


TASK = {
    "What": "Write the onboarding guide",
    "Why": "New partners cannot set up",
    "Done when": "- a partner sets up alone",
    "Discipline": "Software",
    "Priority": "High",
    "Links": "none",
    "Design decisions": "_No response_",
}

INTENT = {
    "Kind": "Feature",
    "What and why": "Weather signal",
    "Done when": "- shows rain",
    "Area": "Signals, Runtime",
    "Protected paths": "```text\nnone\n```",
    "Protected behaviour": "none",
    "Risk": "Low",
    "Limits": "_No response_",
    "Bug details (required when Kind is Bug fix)": "_No response_",
    "Animation details (required when Kind is Animation)": "_No response_",
    "Feature details (required when Kind is Feature)": "For everyone with a display",
    "Discipline": "Software",
    "Priority": "Medium",
    "Design decisions": "_No response_",
}


class FormsTest(unittest.TestCase):
    def test_every_issue_type_has_a_form(self):
        self.assertEqual(sorted(FORMS), ["Audit finding", "Bug", "Decision", "Intent", "Task"])

    def test_required_fields_come_from_the_yaml(self):
        task = {f["label"]: f for f in FORMS["Task"]}
        self.assertTrue(task["What"]["required"])
        self.assertFalse(task["Design decisions"]["required"])

    def test_conditional_requirement_is_read_from_the_label(self):
        intent = {f["label"]: f for f in FORMS["Intent"]}
        self.assertEqual(intent["Bug details (required when Kind is Bug fix)"]["required_when"],
                         ("Kind", "Bug fix"))


class ParseTest(unittest.TestCase):
    def test_reads_answers_under_headings(self):
        self.assertEqual(parse_body(body(**TASK))["What"], "Write the onboarding guide")

    def test_no_response_is_empty(self):
        self.assertEqual(parse_body(body(**TASK))["Design decisions"], "")

    def test_code_fence_is_unwrapped(self):
        self.assertEqual(parse_body(body(**INTENT))["Protected paths"], "none")

    def test_empty_or_missing_body(self):
        self.assertEqual(parse_body(None), {})
        self.assertEqual(parse_body("free text, no headings"), {})


class CheckTest(unittest.TestCase):
    def test_complete_task_passes(self):
        self.assertEqual(check("Task", body(**TASK), FORMS), [])

    def test_complete_intent_passes(self):
        self.assertEqual(check("Intent", body(**INTENT), FORMS), [])

    def test_no_issue_type(self):
        self.assertEqual(len(check(None, body(**TASK), FORMS)), 1)
        self.assertIn("type", check(None, body(**TASK), FORMS)[0])

    def test_unknown_issue_type(self):
        self.assertIn("Feature", check("Feature", body(**TASK), FORMS)[0])

    def test_missing_required_answer(self):
        problems = check("Task", body(**{**TASK, "Why": "_No response_"}), FORMS)
        self.assertEqual(problems, ["**Why** is required but empty."])

    def test_missing_section(self):
        answers = dict(TASK)
        del answers["Links"]
        self.assertEqual(check("Task", body(**answers), FORMS), ["**Links** is required but empty."])

    def test_invalid_dropdown_value(self):
        problems = check("Task", body(**{**TASK, "Priority": "Whenever"}), FORMS)
        self.assertEqual(len(problems), 1)
        self.assertIn("Whenever", problems[0])

    def test_multiple_choice_dropdown(self):
        self.assertEqual(check("Intent", body(**{**INTENT, "Area": "Web, Nope"}), FORMS),
                         ["**Area** has `Nope`, which is not one of the options: Desktop app, Web, "
                          "API, SDK, Runtime, Signals, Animations, Peras, Hardware, Docs, "
                          "CI and tooling."])

    def test_conditional_requirement(self):
        problems = check("Intent", body(**{**INTENT, "Kind": "Bug fix"}), FORMS)
        self.assertEqual(problems, ["**Bug details (required when Kind is Bug fix)** is required "
                                    "when Kind is Bug fix, but empty."])

    def test_issue_made_without_the_form(self):
        problems = check("Task", "Just do the thing", FORMS)
        self.assertEqual(len(problems), 6)


class BoardValuesTest(unittest.TestCase):
    def test_dropdowns_named_like_board_fields(self):
        fields = {"Discipline", "Priority", "Audit", "Severity", "Status"}
        self.assertEqual(board_values("Task", body(**TASK), FORMS, fields),
                         {"Discipline": "Software", "Priority": "High"})

    def test_invalid_values_are_left_out(self):
        fields = {"Discipline", "Priority"}
        self.assertEqual(board_values("Task", body(**{**TASK, "Priority": "Someday"}), FORMS, fields),
                         {"Discipline": "Software"})


class LinkProblemsTest(unittest.TestCase):
    def test_task_with_a_parent_passes(self):
        self.assertEqual(link_problems("Task", True, 0, "Todo", 0), [])

    def test_task_without_a_parent_fails(self):
        problems = link_problems("Task", False, 0, "Todo", 0)
        self.assertEqual(len(problems), 1)
        self.assertIn("no parent", problems[0])

    def test_top_level_issues_need_no_parent(self):
        self.assertEqual(link_problems("Intent", False, 0, "Todo", 0), [])
        self.assertEqual(link_problems("Task", False, 3, "Todo", 0), [])  # a step or workstream

    def test_blocked_without_a_blocked_by_link_fails(self):
        problems = link_problems("Task", True, 0, "Blocked", 0)
        self.assertEqual(len(problems), 1)
        self.assertIn("Blocked", problems[0])

    def test_blocked_with_a_blocked_by_link_passes(self):
        self.assertEqual(link_problems("Task", True, 0, "Blocked", 2), [])

    def test_both_problems_at_once(self):
        self.assertEqual(len(link_problems("Task", False, 0, "Blocked", 0)), 2)

    def test_deferred_findings_parent_is_top_level_even_when_empty(self):
        self.assertEqual(link_problems("Task", False, 0, "Todo", 0, "handbook", "Deferred findings"), [])

    def test_deferred_findings_title_must_match_exactly_in_handbook(self):
        self.assertEqual(len(link_problems("Task", False, 0, "Todo", 0, "handbook",
                                           "Deferred findings for later")), 1)
        self.assertEqual(len(link_problems("Task", False, 0, "Todo", 0, "platform",
                                           "Deferred findings")), 1)

    def test_untriaged_bug_needs_no_parent(self):
        self.assertEqual(link_problems("Bug", False, 0, "Todo", 0), [])

    def test_bug_is_still_checked_for_blocked(self):
        self.assertEqual(len(link_problems("Bug", False, 0, "Blocked", 0)), 1)

    def test_issue_opened_by_the_automation_needs_no_parent(self):
        self.assertEqual(link_problems("Task", False, 0, "Todo", 0, ".github", "Live settings "
                                       "differ from rulesets/", by_bot=True), [])

    def test_issue_opened_by_a_person_still_needs_one(self):
        self.assertEqual(len(link_problems("Task", False, 0, "Todo", 0, ".github", "x",
                                           by_bot=False)), 1)


def sub(number, state, repo="Akenon-Studio/handbook"):
    return {"number": number, "state": state, "repository": {"nameWithOwner": repo}}


class EarlyCloseTest(unittest.TestCase):
    def test_open_sub_issues_are_named(self):
        subs = [sub(1, "CLOSED"), sub(2, "OPEN"), sub(7, "OPEN", "Akenon-Studio/platform")]
        self.assertEqual(open_sub_issues(subs),
                         ["Akenon-Studio/handbook#2", "Akenon-Studio/platform#7"])

    def test_all_closed_means_nothing_open(self):
        self.assertEqual(open_sub_issues([sub(1, "CLOSED")]), [])
        self.assertEqual(open_sub_issues([]), [])

    def test_reopen_comment_names_them(self):
        text = reopen_text(["Akenon-Studio/handbook#2"])
        self.assertIn("- Akenon-Studio/handbook#2", text)
        self.assertIn("Reopened", text)


class CommentTest(unittest.TestCase):
    def test_lists_each_problem(self):
        text = problems_text(["**Why** is required but empty.", "The issue has no parent."])
        self.assertIn("- **Why** is required but empty.\n- The issue has no parent.", text)

    def test_no_problems(self):
        self.assertIn("All required answers are filled in", problems_text([]))


if __name__ == "__main__":
    unittest.main()
