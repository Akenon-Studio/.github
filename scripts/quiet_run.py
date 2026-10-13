#!/usr/bin/env python3
"""Run a command for a public workflow log without letting its errors show a weakness (design 6.2).

The daily settings check runs in the public `.github` repo, so anyone can read its logs. An error
message there can name a repo, a setting or an alert. This runs the command, and prints its output
only when it succeeds; when it fails, it prints the exit code and the Python exception's type,
never its message.

Usage: scripts/quiet_run.py <command> [args...]
       scripts/quiet_run.py --verdict <verify.txt> <code>   the verify step's verdict line
Exit: the command's own exit code; for --verdict, 0 pass, 1 drift, 2 crash.
"""

import re
import subprocess
import sys

# The last Python exception named in the output: `KeyError`, `json.decoder.JSONDecodeError`, ...
EXCEPTION = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt))\b", re.M)


def error_type(output):
    """The type of the last exception in `output`, or "unknown"."""
    found = EXCEPTION.findall(output)
    return found[-1] if found else "unknown"


def verdict(code, output):
    """(code, line) for a verify-settings.py run: exit 1 is drift only with its FAIL line, any
    other failure a crash (2), logged by the exception's type alone."""
    if code == 0:
        return 0, "PASS"
    if code == 1 and re.search(r"^FAIL: ", output, re.M):
        return 1, "FAIL: live settings differ; see the settings-drift issue in .github-private"
    return 2, f"verify-settings.py crashed: {error_type(output)}"


def main(argv):
    if argv[:1] == ["--verdict"]:
        with open(argv[1]) as f:
            code, line = verdict(int(argv[2]), f.read())
        print(line)
        return code
    if not argv:
        print("Usage: scripts/quiet_run.py <command> [args...]", file=sys.stderr)
        return 2
    run = subprocess.run(argv, capture_output=True, text=True)
    if run.returncode == 0:
        sys.stdout.write(run.stdout)
    else:
        print(f"{' '.join(argv[:3])} failed (exit {run.returncode}): {error_type(run.stdout + run.stderr)}")
    return run.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
