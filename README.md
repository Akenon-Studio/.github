# .github

Org-wide defaults and rules for **Akenon Studio**. This repo is public because GitHub only applies
org-wide templates from a public `.github` repo. It holds templates, rules and policy text only:
never code, secrets or product plans.

| Path | What it is |
|---|---|
| `.github/ISSUE_TEMPLATE/` | The issue forms every repo uses (blank issues are off) |
| `.github/PULL_REQUEST_TEMPLATE.md` | The PR template every repo uses |
| `.github/workflows/pr-title.yml` | Reusable check: PR titles must be conventional commits |
| `rulesets/` | Branch and tag rules, repo merge settings, and which repos they apply to |
| `scripts/apply-rules.py` | Applies `rulesets/` to every managed repo (org owners only) |
| `scripts/verify-settings.py` | Checks live GitHub settings match `rulesets/` and the org settings |
| `profile/README.md` | The public org profile |
| `SECURITY.md`, `CONTRIBUTING.md` | Org-wide security policy and contribution rules |

Why it works this way: `handbook` → modernisation design, sections 6.2 and 6.6.

## Changing the rules

1. Edit the files in `rulesets/` in a PR. It needs one approval like any other change.
2. After merge, an org owner runs `scripts/apply-rules.py`.
3. `scripts/verify-settings.py` must then pass.

Org-level rulesets need GitHub Enterprise, so on GitHub Team every repo carries its own copy of the
same rules, applied by the script. The verify script catches any repo that drifts.
