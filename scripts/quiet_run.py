#!/usr/bin/env python3
"""Run a command for a public workflow log without letting its errors show a weakness (design 6.2).

The daily settings check runs in the public `.github` repo, so anyone can read its logs. An error
message there can name a repo, a setting or an alert. This runs the command, and prints its output
only when it succeeds; when it fails, it prints the exit code and the Python exception's type,
never its message.

Usage: scripts/quiet_run.py <command> [args...]
Exit: the command's own exit code.
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


def main(argv):
    if not argv:
        print("Usage: scripts/quiet_run.py <command> [args...]", file=sys.stderr)
        return 2
    run = subprocess.run(argv, capture_output=True, text=True)
    if run.returncode == 0:
        sys.stdout.write(run.stdout)
    else:
        print(f"{' '.join(argv[:2])} failed (exit {run.returncode}): {error_type(run.stdout + run.stderr)}")
    return run.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
