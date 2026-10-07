"""Tests for scripts/issue_fields.py: reading the issue forms, parsing issue bodies, and checking
them. Run: python3 -m unittest discover tests"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from issue_fields import board_values, check, load_forms, parse_body  # noqa: E402

FORMS = load_forms()


def body(**answers):
    """An issue body as GitHub renders a submitted form: '### Label' then the answer."""
    return "\n\n".join(f"### {label}\n\n{value}" for label, value in answers.items())


TASK = {
    "What": "Write the onboarding guide",
    "Why": "New partners cannot set up",
    "Done when": "- a partner sets up alone",
    "Discipline": "Software",
    "Phase": "1",
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
    "Phase": "3",
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
        self.assertEqual(len(problems), 7)


class BoardValuesTest(unittest.TestCase):
    def test_dropdowns_named_like_board_fields(self):
        fields = {"Discipline", "Phase", "Priority", "Audit", "Severity", "Status"}
        self.assertEqual(board_values("Task", body(**TASK), FORMS, fields),
                         {"Discipline": "Software", "Phase": "1", "Priority": "High"})

    def test_invalid_values_are_left_out(self):
        fields = {"Discipline", "Phase", "Priority"}
        self.assertEqual(board_values("Task", body(**{**TASK, "Phase": "9"}), FORMS, fields),
                         {"Discipline": "Software", "Priority": "High"})


if __name__ == "__main__":
    unittest.main()
