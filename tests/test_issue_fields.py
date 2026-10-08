"""Tests for scripts/issue_fields.py: reading the issue forms, parsing issue bodies, and checking
them. Run: python3 -m unittest discover tests"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from issue_fields import (board_values, check, link_problems, load_forms, open_sub_issues,  # noqa: E402
                          parse_body, problems_text, reopen_reasons, reopen_text)

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
        self.assertEqual(sorted(FORMS), ["Audit finding", "Bug", "Decision", "Intent",
                                        "Process failure", "Task"])

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


PREVENTION = "What now prevents this, by code?"
CHECK = "The check (required when What now prevents this, by code? is A check was added)"
NOT_CODE = "Why it can't be code (required when What now prevents this, by code? is It can't be code)"
PROCESS_FAILURE = {
    "What went wrong": "An agent kept its progress in private notes",
    "Where it happened": "handbook#59",
    PREVENTION: "A check was added",
    CHECK: "Akenon-Studio/.github#22",
    NOT_CODE: "_No response_",
    "Discipline": "Software",
    "Priority": "High",
}


class ProcessFailureTest(unittest.TestCase):
    def test_complete_process_failure_passes(self):
        self.assertEqual(check("Process failure", body(**PROCESS_FAILURE), FORMS), [])

    def test_check_added_needs_the_link(self):
        problems = check("Process failure", body(**{**PROCESS_FAILURE, CHECK: "_No response_"}), FORMS)
        self.assertEqual(len(problems), 1)
        self.assertIn("The check", problems[0])

    def test_cant_be_code_needs_the_reason(self):
        answers = {**PROCESS_FAILURE, PREVENTION: "It can't be code", CHECK: "_No response_"}
        problems = check("Process failure", body(**answers), FORMS)
        self.assertEqual(len(problems), 1)
        self.assertIn("Why it can't be code", problems[0])

    def test_not_decided_is_a_valid_answer_while_open(self):
        answers = {**PROCESS_FAILURE, PREVENTION: "Not decided yet", CHECK: "_No response_"}
        self.assertEqual(check("Process failure", body(**answers), FORMS), [])

    def test_closed_while_not_decided_must_reopen(self):
        answers = {**PROCESS_FAILURE, PREVENTION: "Not decided yet", CHECK: "_No response_"}
        reasons = reopen_reasons("Process failure", body(**answers), [])
        self.assertEqual(len(reasons), 1)
        self.assertIn("Not decided yet", reasons[0])

    def test_closed_once_decided_may_stay_closed(self):
        self.assertEqual(reopen_reasons("Process failure", body(**PROCESS_FAILURE), []), [])


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
        self.assertEqual(len(link_problems("Bug", False, 0, "Blocked", 0)), 2)


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

    def test_parent_with_open_sub_issues_must_reopen(self):
        reasons = reopen_reasons("Task", body(**TASK), [sub(2, "OPEN")])
        self.assertEqual(len(reasons), 1)
        self.assertIn("Akenon-Studio/handbook#2", reasons[0])

    def test_parent_with_closed_sub_issues_may_stay_closed(self):
        self.assertEqual(reopen_reasons("Task", body(**TASK), [sub(2, "CLOSED")]), [])

    def test_reopen_comment_names_them(self):
        text = reopen_text(reopen_reasons("Task", "", [sub(2, "OPEN")]))
        self.assertIn("- A parent can't close", text)
        self.assertIn("Akenon-Studio/handbook#2", text)
        self.assertIn("Reopened", text)


class CommentTest(unittest.TestCase):
    def test_lists_each_problem(self):
        text = problems_text(["**Why** is required but empty.", "The issue has no parent."])
        self.assertIn("- **Why** is required but empty.\n- The issue has no parent.", text)

    def test_no_problems(self):
        self.assertIn("All required answers are filled in", problems_text([]))


if __name__ == "__main__":
    unittest.main()
