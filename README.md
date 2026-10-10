# .github

Org-wide defaults and rules for **Akenon Studio**. This repo is public because GitHub only applies
org-wide templates from a public `.github` repo. It holds templates, rules and policy text only:
never code, secrets or product plans.

| Path | What it is |
|---|---|
| `.github/ISSUE_TEMPLATE/` | The issue forms every repo uses (blank issues are off) |
| `.github/PULL_REQUEST_TEMPLATE.md` | The PR template every repo uses |
| `.github/workflows/pr-title.yml` | Reusable PR checks: conventional-commit title, the body closes whole issues, every `CLAUDE.md` passes, and a PR a person opens has its author assigned |
| `.github/workflows/issue-fields.yml` | Reusable: puts each issue on the board with fields from its form answers; flags gaps, missing links and missing assignees with `needs-fields`; on a PR event, sets the issues it closes to In review (`scripts/pr_status.py`) |
| `scripts/issue_fields.py` | What that workflow runs; the forms are its only definition of what is required |
| `tests/` | Unit tests (`python3 -m unittest discover tests`), run by `.github/workflows/tests.yml` |
| `rulesets/` | Branch and tag rules, repo merge settings, labels, issue types, teams (`teams.json`: the automation owners and engineers), and which repos they apply to |
| `scripts/apply-rules.py` | Applies `rulesets/` to every managed repo (org owners only) |
| `rulesets/board.json` | The org project board: fields, linked repos and the views |
| `scripts/apply-board.py` | Creates or updates the board from `rulesets/board.json` |
| `.github/workflows/settings-check.yml` | Daily: runs `verify-settings.py` as a read-only app and keeps one `settings-drift` issue open while anything differs |
| `scripts/verify-settings.py` | Checks live GitHub settings match `rulesets/`, the org settings and the board |
| `scripts/issue_sweep.py` | Every 15 minutes: re-checks issues, and posts one comment on each open PR that conflicts with its base branch |
| `scripts/ai_review.py` | The advisory AI review on every PR (`.github/workflows/ai-review.yml`, design 6.4) |
| `.github/workflows/ai-scorecard.yml` | Monthly: `scripts/ai_scorecard.py` reports the AI review's late catches, false alarms, rounds and cost per PR as one `ai-scorecard` issue |
| `scripts/bot_pr_review.py` | Run by the issue sweep: asks the automation owners to review each bot's open PR |
| `profile/README.md` | The public org profile |
| `SECURITY.md`, `CONTRIBUTING.md` | Org-wide security policy and contribution rules |

Why it works this way: `handbook` → modernisation design, sections 6.2 and 6.6.

## Changing the rules

1. Edit the files in `rulesets/` in a PR. It needs one approval like any other change.
2. After merge, an org owner runs `scripts/apply-rules.py`.
3. `scripts/verify-settings.py` must then pass.

Org-level rulesets need GitHub Enterprise, so on GitHub Team every repo carries its own copy of the
same rules, applied by the script. The verify script catches any repo that drifts.

## New repos

Every repo carries two small caller workflows, copied from this repo: `pr-title-caller.yml` and
`issue-fields-caller.yml` (in the copy, `uses:` points at
`akenon-studio/.github/.github/workflows/<name>.yml@main`, and the triggers stay the same as
here: the PR checks also run on `assigned` and `unassigned`). The issue automation logs in as the
org's GitHub App: org variable `AKENON_APP_ID`, org secret `AKENON_APP_PRIVATE_KEY`.
