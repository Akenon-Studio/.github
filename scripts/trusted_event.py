#!/usr/bin/env python3
"""Decide whether the issue-fields job may run with the Automation app's key (design 6.2): in a
private repo always; in a public one only if everyone the event comes from is a person in the org
or one of the org's own bots (rules.acts_for): the issue's or PR's author, a comment's author, and
whoever triggered it. Runs with no secrets, before the job that holds the key.

Usage: trusted_event.py <event JSON path>. Prints `trusted=true` or `trusted=false`, and appends it
to $GITHUB_OUTPUT when that is set.
"""

import json
import os
import sys

from rules import acts_for


def actors(event):
    """Everyone whose text or action the event carries."""
    item = event.get("issue") or event.get("pull_request") or {}
    found = [item.get("user"), event.get("sender")]
    if "comment" in event:
        found.append(event["comment"].get("user"))
    return found


def trusted(event):
    private = bool((event.get("repository") or {}).get("private"))
    return all(acts_for(actor, private) for actor in actors(event))


def main(argv):
    with open(argv[0]) as f:
        line = f"trusted={'true' if trusted(json.load(f)) else 'false'}"
    print(line)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as out:
            out.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
