"""Tests for scripts/issue_fields.py: reading the issue forms, parsing issue bodies, and checking
them. Run: python3 -m unittest discover tests"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from issue_fields import (all_problems, assignee_problems, board_values, check,  # noqa: E402
                          confirmation_problems, confirmed_by_person, finished_parent_problems,
                          link_problems, load_forms, reclose_due, source_problems,
                          open_sub_issues, parse_body, people_assigned, problems_text,
                          reopen_reasons, reopen_text, start_status, ancestors, epic_of, epic_options)

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


AUDIT = {
    "Audit": "Secrets and access",
    "Severity": "Critical",
    "Finding": "An API key is committed",
    "Evidence": "apps/api/.env.old line 3",
    "Recommended fix": "Rotate the key and delete the file",
    "Discipline": "Software",
    "Priority": "Urgent",
    "Design decisions": "_No response_",
}


class FormsTest(unittest.TestCase):
    def test_every_issue_type_has_a_form(self):
        self.assertEqual(sorted(FORMS), ["Audit finding", "Bug", "Decision", "Epic", "Intent",
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
        self.assertEqual(link_problems("Task", True, "Todo", 0), [])

    def test_task_without_a_parent_fails(self):
        problems = link_problems("Task", False, "Todo", 0)
        self.assertEqual(len(problems), 1)
        self.assertIn("no parent", problems[0])

    def test_epics_and_intents_need_no_parent(self):
        self.assertEqual(link_problems("Epic", False, "Todo", 0), [])
        self.assertEqual(link_problems("Intent", False, "Todo", 0), [])

    def test_an_epic_with_a_parent_fails(self):
        problems = link_problems("Epic", True, "Todo", 0)
        self.assertEqual(len(problems), 1)
        self.assertIn("top level", problems[0])

    def test_blocked_without_a_blocked_by_link_fails(self):
        problems = link_problems("Task", True, "Blocked", 0)
        self.assertEqual(len(problems), 1)
        self.assertIn("Blocked", problems[0])

    def test_blocked_with_a_blocked_by_link_passes(self):
        self.assertEqual(link_problems("Task", True, "Blocked", 2), [])

    def test_both_problems_at_once(self):
        self.assertEqual(len(link_problems("Task", False, "Blocked", 0)), 2)

    def test_untriaged_bug_needs_no_parent(self):
        self.assertEqual(link_problems("Bug", False, "Todo", 0), [])

    def test_bug_is_still_checked_for_blocked(self):
        self.assertEqual(len(link_problems("Bug", False, "Blocked", 0)), 1)

    def test_issue_opened_by_the_automation_needs_no_parent(self):
        self.assertEqual(link_problems("Task", False, "Todo", 0, by_bot=True), [])

    def test_issue_opened_by_a_person_still_needs_one(self):
        self.assertEqual(len(link_problems("Task", False, "Todo", 0, by_bot=False)), 1)


class FinishedParentTest(unittest.TestCase):
    def test_open_parent_with_every_sub_issue_closed_fails(self):
        problems = finished_parent_problems("OPEN", 3, 3)
        self.assertEqual(len(problems), 1)
        self.assertIn("All its sub-issues are closed", problems[0])
        self.assertIn("add a sub-issue for the work left", problems[0])

    def test_open_parent_with_a_sub_issue_still_open_passes(self):
        self.assertEqual(finished_parent_problems("OPEN", 3, 2), [])

    def test_issue_without_sub_issues_passes(self):
        self.assertEqual(finished_parent_problems("OPEN", 0, 0), [])

    def test_closed_parent_passes(self):
        self.assertEqual(finished_parent_problems("CLOSED", 3, 3), [])

    def test_whole_issue_is_checked(self):
        issue = {"issueType": {"name": "Task"}, "body": body(**TASK), "state": "OPEN",
                 "author": {"__typename": "User", "login": "RuvinduH"},
                 "repository": {"nameWithOwner": "Akenon-Studio/platform"}, "number": 48,
                 "parent": {"number": 24}, "subIssues": {"nodes": [sub(1, "CLOSED")]},
                 "subIssuesSummary": {"total": 1, "completed": 1},
                 "blockedBy": {"totalCount": 0},
                 "assignedActors": {"nodes": [{"__typename": "User", "login": "RuvinduH"}]},
                 "timelineItems": {"nodes": []}, "labels": {"nodes": []},
                 "labelEvents": {"nodes": []}}
        self.assertEqual(all_problems(issue, FORMS, "Todo"),
                         [finished_parent_problems("OPEN", 1, 1)[0]])
        issue["subIssuesSummary"] = {"total": 2, "completed": 1}
        self.assertEqual(all_problems(issue, FORMS, "Todo"), [])


class AssigneeProblemsTest(unittest.TestCase):
    def test_author_assigned_passes(self):
        self.assertEqual(assignee_problems("RuvinduH", False, ["RuvinduH"], ["RuvinduH"], "Todo"), [])

    def test_author_never_assigned_fails(self):
        problems = assignee_problems("RuvinduH", False, [], [], "Todo")
        self.assertEqual(len(problems), 1)
        self.assertIn("@RuvinduH, is not assigned", problems[0])

    def test_author_assigned_then_handed_to_someone_else_passes(self):
        self.assertEqual(assignee_problems("RuvinduH", False, ["Thytus777"],
                                           ["RuvinduH", "Thytus777"], "Todo"), [])

    def test_only_someone_else_ever_assigned_fails(self):
        self.assertEqual(len(assignee_problems("RuvinduH", False, ["Thytus777"], ["Thytus777"],
                                               "Todo")), 1)

    def test_logins_compare_without_case(self):
        self.assertEqual(assignee_problems("ruvinduh", False, ["RuvinduH"], [], "Todo"), [])

    def test_issue_a_bot_opened_needs_no_author_assignee(self):
        self.assertEqual(assignee_problems("akenon-studio-automation", True, [], [], "Todo"), [])

    def test_waiting_for_human_with_nobody_assigned_fails(self):
        problems = assignee_problems("RuvinduH", False, [], ["RuvinduH"], "Waiting for human")
        self.assertEqual(len(problems), 1)
        self.assertIn("Waiting for human", problems[0])

    def test_waiting_for_human_applies_to_a_bots_issue_too(self):
        self.assertEqual(len(assignee_problems("peras", True, [], [], "Waiting for human")), 1)

    def test_waiting_for_human_with_a_person_passes(self):
        self.assertEqual(assignee_problems("peras", True, ["RuvinduH"], [], "Waiting for human"), [])

    def test_bots_assigned_are_not_people(self):
        issue = {"assignedActors": {"nodes": [{"__typename": "Bot", "login": "Copilot"},
                                              {"__typename": "User", "login": "RuvinduH"}]},
                 "timelineItems": {"nodes": [{"assignee": {"__typename": "User", "login": "Thytus777"}},
                                             {"assignee": None}, {}]}}
        self.assertEqual(people_assigned(issue), (["RuvinduH"], ["Thytus777"]))
        issue["assignedActors"]["nodes"].pop()
        now, _ = people_assigned(issue)
        self.assertEqual(len(assignee_problems("peras", True, now, [], "Waiting for human")), 1)


def labelled(name, actor_type, login="RuvinduH"):
    """A LabeledEvent node as the timeline query returns it."""
    return {"label": {"name": name}, "actor": {"__typename": actor_type, "login": login}}


class ConfirmationTest(unittest.TestCase):
    def test_critical_and_high_findings_start_waiting_for_a_human(self):
        self.assertEqual(start_status("Audit finding", body(**AUDIT)), "Waiting for human")
        self.assertEqual(start_status("Audit finding", body(**{**AUDIT, "Severity": "High"})),
                         "Waiting for human")

    def test_medium_and_low_findings_start_in_todo(self):
        for severity in ("Medium", "Low"):
            self.assertEqual(start_status("Audit finding", body(**{**AUDIT, "Severity": severity})),
                             "Todo")

    def test_other_types_start_as_before(self):
        self.assertEqual(start_status("Decision", ""), "Waiting for human")
        self.assertEqual(start_status("Task", body(**TASK)), "Todo")

    def test_unconfirmed_critical_finding_fails(self):
        problems = confirmation_problems("Audit finding", body(**AUDIT), False)
        self.assertEqual(len(problems), 1)
        self.assertIn("**Critical**", problems[0])
        self.assertIn("`confirmed`", problems[0])

    def test_confirmed_finding_passes(self):
        self.assertEqual(confirmation_problems("Audit finding", body(**AUDIT), True), [])

    def test_medium_finding_needs_no_confirmation(self):
        self.assertEqual(confirmation_problems(
            "Audit finding", body(**{**AUDIT, "Severity": "Medium"}), False), [])

    def test_other_types_need_no_confirmation(self):
        self.assertEqual(confirmation_problems("Task", body(**{**TASK, "Severity": "Critical"}),
                                               False), [])

    def test_label_a_person_applied_counts(self):
        self.assertTrue(confirmed_by_person({"audit", "confirmed"},
                                            [labelled("audit", "Bot"), labelled("confirmed", "User")]))

    def test_label_a_bot_or_app_applied_does_not_count(self):
        self.assertFalse(confirmed_by_person({"confirmed"}, [labelled("confirmed", "Bot", "peras")]))

    def test_the_last_application_decides(self):
        events = [labelled("confirmed", "User"), labelled("confirmed", "Bot", "peras")]
        self.assertFalse(confirmed_by_person({"confirmed"}, events))
        self.assertTrue(confirmed_by_person({"confirmed"}, events[::-1]))

    def test_label_removed_since_does_not_count(self):
        self.assertFalse(confirmed_by_person({"audit"}, [labelled("confirmed", "User")]))

    def test_label_with_no_event_does_not_count(self):
        self.assertFalse(confirmed_by_person({"confirmed"}, []))

    def test_whole_issue_is_checked(self):
        issue = {"issueType": {"name": "Audit finding"}, "body": body(**AUDIT),
                 "author": {"__typename": "User", "login": "RuvinduH"},
                 "repository": {"nameWithOwner": "Akenon-Studio/platform"}, "number": 9,
                 "state": "OPEN", "parent": {"number": 24}, "subIssues": {"nodes": []},
                 "subIssuesSummary": {"total": 0, "completed": 0},
                 "blockedBy": {"totalCount": 0},
                 "assignedActors": {"nodes": [{"__typename": "User", "login": "RuvinduH"}]},
                 "timelineItems": {"nodes": []},
                 "labels": {"nodes": [{"name": "audit"}]},
                 "labelEvents": {"nodes": [labelled("audit", "User")]}}
        self.assertEqual(len(all_problems(issue, FORMS, "Waiting for human")), 1)
        issue["labels"]["nodes"].append({"name": "confirmed"})
        issue["labelEvents"]["nodes"].append(labelled("confirmed", "User"))
        self.assertEqual(all_problems(issue, FORMS, "Waiting for human"), [])


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


def node(number, issue_type="Task", parent=None, title=None, repo="Akenon-Studio/handbook"):
    return {"number": number, "title": title or f"Issue {number}", "issueType": {"name": issue_type},
            "repository": {"nameWithOwner": repo}, "parent": parent}


class EpicOfTest(unittest.TestCase):
    def test_work_two_levels_under_an_epic(self):
        epic = node(146, "Epic", title="Phase 3: the first release")
        work = node(5, parent=node(37, parent=epic), repo="Akenon-Studio/hardware")
        self.assertEqual(epic_of(work), ("handbook#146", "Phase 3: the first release"))

    def test_an_epic_is_its_own_epic(self):
        self.assertEqual(epic_of(node(136, "Epic", title="Phase 2")), ("handbook#136", "Phase 2"))

    def test_no_epic_above(self):
        self.assertIsNone(epic_of(node(5, parent=node(37))))
        self.assertIsNone(epic_of(node(5)))

    def test_the_topmost_epic_wins(self):
        inner = node(2, "Epic", parent=node(1, "Epic", title="Top"))
        self.assertEqual(epic_of(node(3, parent=inner))[1], "Top")

    def test_ancestors_query_goes_deep_enough(self):
        self.assertEqual(ancestors(3).count("parent {"), 2)


LIVE = [{"id": "a", "name": "Phase 2", "color": "GRAY", "description": "handbook#136"}]


class EpicOptionsTest(unittest.TestCase):
    def test_the_board_query_reads_what_finds_and_keeps_an_option(self):
        # Without descriptions no option is ever found, so each sync adds another (.github#50).
        import inspect
        import board
        self.assertIn("options { id name color description }", inspect.getsource(board.find_board_fields))

    def test_up_to_date_changes_nothing(self):
        self.assertIsNone(epic_options(LIVE, "handbook#136", "Phase 2"))

    def test_new_epic_is_added_and_existing_ids_kept(self):
        options = epic_options(LIVE, "handbook#147", "The display")
        self.assertEqual(options[0]["id"], "a")
        self.assertEqual(options[1], {"name": "The display", "color": "GRAY",
                                      "description": "handbook#147"})

    def test_renamed_epic_keeps_its_option(self):
        options = epic_options(LIVE, "handbook#136", "Phase 2: the working flow")
        self.assertEqual(options, [dict(LIVE[0], name="Phase 2: the working flow")])


class SourceLabelTest(unittest.TestCase):
    SOURCES = {"dependencies", "settings-drift"}

    def test_a_bots_issue_needs_a_source_label(self):
        self.assertEqual(len(source_problems("renovate", True, {"task"}, self.SOURCES)), 1)
        self.assertEqual(source_problems("renovate", True, {"dependencies"}, self.SOURCES), [])

    def test_a_persons_issue_needs_none(self):
        self.assertEqual(source_problems("RuvinduH", False, set(), self.SOURCES), [])


def parent(state="OPEN", total=3, completed=3, reopened_by="akenon-studio-automation"):
    nodes = [{"actor": {"__typename": "Bot", "login": reopened_by}}] if reopened_by else []
    return {"state": state, "subIssuesSummary": {"total": total, "completed": completed},
            "reopenEvents": {"nodes": nodes}}


class RecloseTest(unittest.TestCase):
    def test_a_parent_the_automation_reopened_closes_once_its_subs_are_done(self):
        self.assertTrue(reclose_due(parent()))

    def test_not_while_a_sub_issue_is_open(self):
        self.assertFalse(reclose_due(parent(completed=2)))

    def test_a_parent_a_person_or_another_bot_reopened_stays_open(self):
        self.assertFalse(reclose_due(parent(reopened_by="RuvinduH")))
        self.assertFalse(reclose_due(parent(reopened_by="dependabot")))

    def test_never_reopened_or_no_subs_or_closed_is_left_alone(self):
        self.assertFalse(reclose_due(parent(reopened_by=None)))
        self.assertFalse(reclose_due(dict(parent(), reopenEvents={"nodes": [{"actor": None}]})))
        self.assertFalse(reclose_due(parent(total=0, completed=0)))
        self.assertFalse(reclose_due(parent(state="CLOSED")))
