"""Tests for scripts/gaming_check.py (design 7.13, stage 6). Run: python3 -m unittest discover tests

The patterns the check blocks are built from parts here, so this file's own diff passes it."""

import contextlib
import io
import pathlib
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
from gaming_check import check, main, parse, run_diff  # noqa: E402

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
        self.assertEqual(blocking, ["tests/test_old.py: test file deleted; it imports no repo module "
                                    "deleted with it"])
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


class DeletedTestFileTest(unittest.TestCase):
    """A deleted test is flagged, not blocked, only when every repo-local module it imports goes in
    the same PR (the code and its tests retired together); anything else still blocks."""

    def deleted(self, path, lines):
        return diff(path, removed=lines, deleted=True)

    def test_a_test_deleted_with_its_module_is_flagged(self):
        # Shaped like animations#161: tools/capture/frame-metrics.test.mjs goes with frame-metrics.mjs.
        blocking, flagged = run(
            self.deleted("tools/capture/frame-metrics.test.mjs", [
                "import test from 'node:test';", "import assert from 'node:assert/strict';",
                "import zlib from 'node:zlib';",
                "import { frameMetrics, decodePng } from './frame-metrics.mjs';",
                "test('metrics', () => { assert.ok(frameMetrics(decodePng(zlib))); });"]),
            self.deleted("tools/capture/frame-metrics.mjs", ["export function frameMetrics() {}"]))
        self.assertEqual(blocking, [])
        self.assertEqual(flagged, ["tools/capture/frame-metrics.test.mjs: test file deleted with what it "
                                   "tests (tools/capture/frame-metrics.mjs)"])

    def test_a_test_whose_module_survives_blocks(self):
        blocking, flagged = run(self.deleted("tools/capture/frame-metrics.test.mjs", [
            "import { frameMetrics } from './frame-metrics.mjs';"]))
        self.assertEqual(flagged, [])
        self.assertEqual(blocking, ["tools/capture/frame-metrics.test.mjs: test file deleted; "
                                    "./frame-metrics.mjs not deleted with it"])

    def test_a_test_with_no_local_import_blocks(self):
        blocking, _ = run(self.deleted("src/a.test.ts", ["import { test } from 'vitest';",
                                                         "import fs from 'node:fs';"]),
                          self.deleted("src/a.ts", ["export const a = 1;"]))
        self.assertEqual(blocking, ["src/a.test.ts: test file deleted; it imports no repo module "
                                    "deleted with it"])

    def test_a_test_where_one_of_two_modules_survives_blocks(self):
        blocking, _ = run(self.deleted("src/a.test.ts", ["import { a } from './a';",
                                                         "const { b } = require('../lib/b.js');"]),
                          self.deleted("src/a.ts", ["export const a = 1;"]))
        self.assertEqual(blocking, ["src/a.test.ts: test file deleted; ../lib/b.js not deleted with it"])

    def test_js_forms_resolve_with_or_without_extension_or_index(self):
        blocking, flagged = run(
            self.deleted("src/x/a.spec.ts", [
                "import type { A } from './a.js';", "import {", "  b,", "} from '../b';",
                "const c = require('./c');", "const d = await import('./d');", "import './e';"]),
            *[self.deleted(p, ["x"]) for p in ("src/x/a.ts", "src/b/index.ts", "src/x/c.cjs",
                                               "src/x/d.mjs", "src/x/e.js")])
        self.assertEqual(blocking, [])
        self.assertEqual(len(flagged), 1)

    def test_an_unreadable_or_aliased_js_import_blocks(self):
        for line in ("const m = require(name);", "const m = await import(`./${name}.js`);",
                     "import { a } from '@/a';"):
            blocking, _ = run(self.deleted("src/a.test.ts", ["import { a } from './a';", line]),
                              self.deleted("src/a.ts", ["export const a = 1;"]))
            self.assertEqual(len(blocking), 1, line)

    def test_python_relative_and_absolute_imports(self):
        blocking, flagged = run(
            self.deleted("pkg/sub/test_x.py", [
                "import os, unittest", "from __future__ import annotations",
                "from ..x import f  # noqa: E402", "from . import (", "    helpers,", ")",
                "from pkg.y import g"]),
            *[self.deleted(p, ["x = 1"]) for p in ("pkg/x.py", "pkg/sub/helpers.py", "pkg/y.py")])
        self.assertEqual(blocking, [])
        self.assertEqual(flagged, ["pkg/sub/test_x.py: test file deleted with what it tests "
                                   "(pkg/sub/helpers.py, pkg/x.py, pkg/y.py)"])

    def test_python_module_not_deleted_blocks(self):
        for lines in (["from .x import f"], ["import gaming_check"], ["import importlib",
                                                                     "m = importlib.import_module('x')"]):
            blocking, _ = run(self.deleted("src/test_x.py", lines + ["from .y import g"]),
                              self.deleted("src/y.py", ["g = 1"]))
            self.assertEqual(len(blocking), 1, lines)

    def test_a_deleted_test_without_its_content_blocks(self):
        bare = "diff --git a/src/a.test.ts b/src/a.test.ts\ndeleted file mode 100644\n"
        blocking, _ = run(bare, self.deleted("src/a.ts", ["export const a = 1;"]))
        self.assertEqual(blocking, ["src/a.test.ts: test file deleted; its content isn't in the diff"])


class OffAPrTest(unittest.TestCase):
    """Off a PR, pr-title.yml passes `origin/main` as the base: the diff is from where the commit left
    main, so a branch that main has moved past shows only its own changes (design 7.13 Stage 10)."""

    def git(self, *args):
        subprocess.run(["git", "-C", self.dir, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       check=True, capture_output=True)

    def write(self, path, text):
        (pathlib.Path(self.dir) / path).parent.mkdir(parents=True, exist_ok=True)
        (pathlib.Path(self.dir) / path).write_text(text)

    def setUp(self):
        self.dir = self.enterContext(tempfile.TemporaryDirectory())
        self.git("init", "-q", "-b", "main")
        self.write("tests/test_a.py", "def test_a(): assert True\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "start")
        self.git("switch", "-q", "-c", "peras/1-1")
        self.write("src/b.py", "x = 1\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "patch")
        self.git("switch", "-q", "main")  # main moves on: a new test file the branch doesn't have
        self.write("tests/test_c.py", "def test_c(): assert True\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "main moves")

    def test_a_branch_behind_main_shows_only_its_own_change(self):
        self.git("switch", "-q", "peras/1-1")
        files = parse(run_diff("main", self.dir))
        self.assertEqual(list(files), ["src/b.py"])
        self.assertEqual(check(files)[0], [])

    def test_a_commit_of_main_has_nothing_to_check(self):
        self.assertEqual(parse(run_diff("main", self.dir)), {})

    def gaming(self, base="main"):
        out = pathlib.Path(self.dir) / "summary.md"
        with unittest.mock.patch.dict("os.environ", {"GITHUB_STEP_SUMMARY": str(out)}), \
                contextlib.redirect_stdout(io.StringIO()):
            code = main([base, self.dir])
        return code, out.read_text()

    def test_with_a_base_sha_a_surviving_python_module_blocks_and_a_package_does_not(self):
        self.git("switch", "-q", "-c", "retire")
        self.write("src/c.py", "c = 1\n")
        self.write("src/test_d.py", "import pytest\nimport c\nimport a_mod\n")
        self.write("src/a_mod.py", "a = 1\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "more")
        self.git("tag", "base")
        (pathlib.Path(self.dir) / "src/test_d.py").unlink()
        (pathlib.Path(self.dir) / "src/a_mod.py").unlink()
        self.git("commit", "-q", "-am", "drop the test and one module")
        code, summary = self.gaming("base")
        self.assertEqual(code, 1)
        self.assertIn("src/test_d.py: test file deleted; c not deleted with it", summary)
        (pathlib.Path(self.dir) / "src/c.py").unlink()
        self.git("commit", "-q", "-am", "drop the other module")
        code, summary = self.gaming("base")  # pytest isn't in the repo: a package, not local
        self.assertEqual(code, 0)
        self.assertIn("look at: src/test_d.py: test file deleted with what it tests", summary)

    def test_with_a_base_sha_a_deletion_left_out_of_the_diff_is_read_at_the_base(self):
        self.write("src/m.py", "m = 1\n")
        self.write("src/test_m.py", "from .m import m\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "m")
        self.git("tag", "base")
        self.git("rm", "-q", "src/m.py", "src/test_m.py")
        self.git("commit", "-q", "-m", "retire m")
        no_content = subprocess.run(["git", "-C", self.dir, "diff", "-M", "-D", "--unified=0",
                                     "base...HEAD"], capture_output=True, text=True).stdout
        self.assertEqual(parse(no_content)["src/test_m.py"]["removed"], [])
        with unittest.mock.patch("gaming_check.run_diff", return_value=no_content):
            code, summary = self.gaming("base")
        self.assertEqual(code, 0)
        self.assertIn("test file deleted with what it tests (src/m.py)", summary)


if __name__ == "__main__":
    unittest.main()
