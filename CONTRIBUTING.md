# Contributing

These rules apply to every repo in the org. They are enforced by GitHub, not by trust.

## Workflow

1. Branch from `main`: `feat/…`, `fix/…`, `chore/…`, `docs/…`.
2. Open a PR. Its **title must be a conventional commit** (`feat: add pairing screen`), because
   PRs are squash-merged and the title becomes the commit and the changelog entry. Its **body
   closes at least one whole issue** (`Closes #12` or `Closes akenon-studio/handbook#12`); work that
   does only part of an issue splits it into sub-issues first (handbook design 6.8).
3. All required checks pass, the branch is up to date with `main`, and every conversation is
   resolved.
4. **One approval from another person.** Never the author, never a bot or agent. A new push
   dismisses earlier approvals.
5. Squash-merge. The branch is deleted automatically.

Nobody can bypass these rules, including org owners.

## Emergencies

Use a **fast-track PR**: the checks still run and any one partner can approve. Tick the
fast-track box in the PR and post a short write-up (what broke, what was done, what follows) within
two days.

## Commit identity

Commit with your `@akenon-studio.com` address. Git can do this automatically for every repo in the
org; add to `~/.gitconfig`:

```
[includeIf "hasconfig:remote.*.url:https://github.com/akenon-studio/**"]
	path = ~/.gitconfig-akenon
[includeIf "hasconfig:remote.*.url:https://github.com/Akenon-Studio/**"]
	path = ~/.gitconfig-akenon
```

and in `~/.gitconfig-akenon`:

```
[user]
	email = you@akenon-studio.com
```

Add the same address to your GitHub account as a verified email. When merging on github.com, pick
it in the merge box.

## Issues

Use the issue forms; blank issues are turned off. Every required field must be filled in.

## Secrets

Never commit secrets, keys or `.env` files. Each repo documents its settings in `.env.example`.
