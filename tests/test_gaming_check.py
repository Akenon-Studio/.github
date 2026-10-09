"""Tests for scripts/gaming_check.py (design 7.13, stage 6). Run: python3 -m unittest discover tests

The patterns the check blocks are built from parts here, so this file's own diff passes it."""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
from gaming_check import check, parse  # noqa: E402

ONLY, SKIP = "it." + "only(", "it." + "skip("
TS_IGNORE, LINT_OFF = "// @ts-" + "ignore", "// eslint-" + "disable-next-line no-console"
MARK = "gaming-" + "check: flaky on CI, platform#1"


def diff(path, added=(), removed=(), deleted=False):
    head = f"diff --git a/{path} b/{path}\n" + ("deleted file mode 100644\n" if deleted else "")
    head += f"--- a/{path}\n+++ b/{path}\n@@ -1,{len(removed)} +1,{len(added)} @@\n"
    return head + "".join(f"-{r}\n" for r in removed) + "".join(f"+{a}\n" for a in added)


def run(*diffs):
    return check(parse("".join(diffs)))


class BlockingTest(unittest.TestCase):
    def test_focused_and_skipped_tests_block(self):
        blocking, _ = run(diff("src/a.test.ts", [f"{ONLY}'x', () => {{}})", f"{SKIP}'y', () => {{}})",
                                                 "x" + "describe('z', () => {})"]))
        self.assertEqual(len(blocking), 3)
        self.assertTrue(blocking[0].startswith("src/a.test.ts:1:"))

    def test_python_skip_blocks_in_tests(self):
        blocking, _ = run(diff("tests/test_a.py", ["@unittest." + "skip('later')"]))
        self.assertEqual(len(blocking), 1)

    def test_only_and_skip_are_fine_outside_tests(self):
        blocking, _ = run(diff("src/menu.ts", [f"const x = {ONLY})"]))
        self.assertEqual(blocking, [])

    def test_type_and_lint_escapes_block_in_js_and_ts(self):
        blocking, _ = run(diff("src/a.ts", [TS_IGNORE, LINT_OFF]), diff("src/b.js", [LINT_OFF]))
        self.assertEqual(len(blocking), 3)

    def test_they_mean_nothing_in_python(self):
        blocking, _ = run(diff("scripts/x.py", [f"PATTERN = '{TS_IGNORE}'"]))
        self.assertEqual(blocking, [])

    def test_any_blocks_in_code_but_not_in_tests(self):
        blocking, _ = run(diff("src/a.ts", ["function f(x: any) {}", "const y = z as any;"]),
                          diff("src/a.test.ts", ["const m: any = {};"]))
        self.assertEqual([b.split(": ", 1)[1] for b in blocking], ["`any` loosens a type"] * 2)

    def test_words_containing_any_are_fine(self):
        blocking, _ = run(diff("src/a.ts", ["const company: Company = anything;", "// many: any of"]))
        self.assertEqual(blocking, [])

    def test_deleted_test_file_blocks_but_rename_does_not(self):
        blocking, _ = run(diff("tests/test_old.py", removed=["def test_x(): pass"], deleted=True))
        self.assertEqual(blocking, ["tests/test_old.py: test file deleted"])
        rename = ("diff --git a/tests/test_a.py b/tests/test_b.py\nsimilarity index 100%\n"
                  "rename from tests/test_a.py\nrename to tests/test_b.py\n")
        self.assertEqual(run(rename), ([], []))

    def test_marker_lets_a_line_through(self):
        blocking, _ = run(diff("src/a.test.ts", [f"{SKIP}'y', () => {{}})  // {MARK}"]))
        self.assertEqual(blocking, [])

    def test_vendored_and_built_paths_are_ignored(self):
        blocking, _ = run(diff("node_modules/x/index.js", [LINT_OFF]), diff("dist/a.js", [LINT_OFF]),
                          diff("animations/arbor/index.js", [LINT_OFF]))
        self.assertEqual(blocking, [])
        blocking, _ = run(diff("animations/arbor/src/index.js", [LINT_OFF]))
        self.assertEqual(len(blocking), 1)

    def test_import_order_noqa_is_not_flagged(self):
        _, flagged = run(diff("tests/test_x.py", ["from x import y  # " + "noqa: E402"]))
        self.assertEqual(flagged, [])


class FlaggedTest(unittest.TestCase):
    def test_borderline_cases_are_flagged_not_blocked(self):
        blocking, flagged = run(diff("src/a.ts", ["// @ts-" + "expect-error wrong lib types"]),
                                diff("src/b.py", ["import x  # " + "noqa"]),
                                diff("src/c.test.ts", ["it." + "skipIf(isWindows)('z', () => {})"]))
        self.assertEqual(blocking, [])
        self.assertEqual(len(flagged), 3)

    def test_lost_assertions_are_flagged(self):
        _, flagged = run(diff("src/a.test.ts", added=["const r = f();"],
                              removed=["expect(f()).toBe(1);", "expect(g()).toBe(2);"]))
        self.assertEqual(flagged, ["src/a.test.ts: 2 fewer assertion line(s) than before"])

    def test_bulk_snapshot_updates_are_flagged(self):
        _, flagged = run(*[diff(f"src/__snapshots__/s{i}.test.ts.snap", ["x"]) for i in range(3)])
        self.assertEqual(flagged, ["3 snapshot files updated: check each change is intended"])


if __name__ == "__main__":
    unittest.main()
