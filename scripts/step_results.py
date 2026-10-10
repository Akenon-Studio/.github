#!/usr/bin/env python3
"""The result of a job whose checks are steps (design 6.3: one job per check workflow).

Each check step runs with continue-on-error, so every one runs; this last step fails the job if any
blocking step failed, and lists every outcome in the job summary. An advisory step's failure is a
warning only.

Usage: scripts/step_results.py   (reads OUTCOMES, "name=outcome" separated by spaces, from
`steps.<id>.outcome`; ADVISORY, the names whose failure doesn't fail the job; GITHUB_STEP_SUMMARY)
"""

import os
import sys

MARK = {"success": "✅", "failure": "❌", "cancelled": "⏹", "skipped": "⏭"}


def parse(outcomes):
    """[(name, outcome)] in order, from "a=success b=failure"."""
    return [tuple(pair.split("=", 1)) for pair in outcomes.split()]


def failed(results, advisory):
    """(blocking failures, advisory failures). A step that didn't finish (cancelled, or skipped
    after setup failed) fails too: only success passes."""
    bad = [name for name, outcome in results if outcome != "success"]
    return [n for n in bad if n not in advisory], [n for n in bad if n in advisory]


def main():
    results = parse(os.environ["OUTCOMES"])
    advisory = set(os.environ.get("ADVISORY", "").split())
    blocking, warnings = failed(results, advisory)
    lines = ["| Check | Result |", "|---|---|"]
    lines += [f"| {name}{' (advisory)' if name in advisory else ''} | {MARK.get(outcome, '')} {outcome} |"
              for name, outcome in results]
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as out:
            out.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    for name in warnings:
        print(f"::warning::{name} failed (advisory): see its step above")
    for name in blocking:
        print(f"::error::{name} failed: see its step above")
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
