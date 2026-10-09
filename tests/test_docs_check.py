"""Tests for scripts/docs_check.py (design 6.5). Run: python3 -m unittest discover tests"""

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
from docs_check import RULES, check, front_matter, glob_re, looks_like_path  # noqa: E402

HEADER = "---\nowner: Software\ncovers: src/**\n---\n\n"


def repo(files):
    """A throwaway git repo with these files, for check()."""
    root = pathlib.Path(tempfile.mkdtemp())
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    return root


RULE_SET = {"owners": ["Software", "Brand"],
            "all": {"docs": ["docs/architecture/**/*.md"], "no_header": ["README.md"], "skip": []},
            "code": {"docs": ["packages/*/README.md"],
                     "exceptions": [{"path": "odd/NOTE.md", "why": "test"}]}}


class GlobTest(unittest.TestCase):
    def test_star_stays_in_a_folder_and_double_star_crosses(self):
        self.assertTrue(glob_re("packages/*/README.md").match("packages/sdk/README.md"))
        self.assertFalse(glob_re("packages/*/README.md").match("packages/sdk/x/README.md"))
        self.assertTrue(glob_re("docs/architecture/**/*.md").match("docs/architecture/a.md"))
        self.assertTrue(glob_re("docs/architecture/**/*.md").match("docs/architecture/x/y/a.md"))


class PathTokenTest(unittest.TestCase):
    def test_what_counts_as_a_path(self):
        for t in ["src/a.ts", "apps/api/src/x.ts:34", "docs/"]:
            self.assertTrue(looks_like_path(t), t)
        for t in ["CLAUDE.md", "@akenon/sdk", "https://x.y/z", "./sibling.js", "a = b/c", "~/x/y"]:
            self.assertFalse(looks_like_path(t), t)


class CheckTest(unittest.TestCase):
    def test_a_clean_repo_passes(self):
        root = repo({"README.md": "# x\n", "src/a.ts": "export const API_URL = 1;\n",
                     "docs/architecture/a.md": HEADER + "See `src/a.ts` and `API_URL`, [b](b.md).\n",
                     "docs/architecture/b.md": HEADER + "x\n"})
        self.assertEqual(check(root, "code", RULE_SET), [])

    def test_outside_the_map_and_missing_header(self):
        root = repo({"src/a.ts": "", "notes.md": "x\n", "docs/architecture/a.md": "# no header\n",
                     "odd/NOTE.md": HEADER + "x\n"})
        found = check(root, "code", RULE_SET)
        self.assertTrue(any(f.startswith("notes.md: outside the docs map") for f in found))
        self.assertTrue(any(f.startswith("docs/architecture/a.md: no owner/covers") for f in found))
        self.assertFalse(any(f.startswith("odd/NOTE.md") for f in found))  # a recorded exception

    def test_dead_path_env_var_link_and_bad_header(self):
        bad = "---\nowner: Sales\ncovers: lib/**\n---\n\n"
        root = repo({"src/a.ts": "",
                     "docs/architecture/a.md": bad + "`src/gone.ts`, `OLD_NAME`, [x](missing.md)\n"})
        found = " ".join(check(root, "code", RULE_SET))
        for want in ["`owner` is 'Sales'", "glob 'lib/**' matches no file", "path `src/gone.ts`",
                     "env var `OLD_NAME`", "link to `missing.md`"]:
            self.assertIn(want, found)

    def test_paths_outside_this_repo_and_in_fences_are_not_checked(self):
        root = repo({"src/a.ts": "", "docs/architecture/a.md": HEADER +
                     "`drivers/gpu/x.c` and `platform/apps/x.ts`\n```\nsrc/gone.ts OLD_NAME\n```\n"})
        self.assertEqual(check(root, "code", RULE_SET), [])

    def test_nested_fences_and_history_links(self):
        root = repo({"src/a.ts": "",
                     "docs/architecture/a.md": HEADER + "````\n```\nsrc/gone.ts\n```\n````\n`src/gone2.ts`\n",
                     "docs/decisions/d.md": HEADER + "[old](gone.md)\n"})
        found = " ".join(check(root, "code", dict(RULE_SET, all=dict(
            RULE_SET["all"], docs=["docs/architecture/**/*.md", "docs/decisions/**/*.md"]))))
        self.assertNotIn("src/gone.ts`", found)
        self.assertIn("src/gone2.ts", found)
        self.assertNotIn("gone.md", found)

    def test_crlf_bom_and_non_ascii_names(self):
        root = repo({"src/a.ts": "", "docs/architecture/\u00e9t\u00e9.md":
                     "\ufeff" + HEADER.replace("\n", "\r\n") + "x\r\n"})
        self.assertEqual(check(root, "code", RULE_SET), [])

    def test_front_matter(self):
        self.assertEqual(front_matter(HEADER)[0]["owner"], "Software")
        self.assertEqual(front_matter("# title\n"), (None, None))
        self.assertIn("not valid YAML", front_matter("---\ncovers: **/*.md\n---\n")[1])

    def test_exceptions_get_the_header_check_and_braces_and_code_span_links(self):
        root = repo({"src/a.ts": "", "src/b.ts": "", "odd/NOTE.md": "# no header\n",
                     "docs/architecture/a.md": HEADER + "`src/{a,b}.ts`, `src/{a,c}.ts` and "
                     "`[x](nowhere.md)` in code\n"})
        found = " ".join(check(root, "code", RULE_SET))
        self.assertIn("odd/NOTE.md: no owner/covers header", found)
        self.assertNotIn("src/{a,b}.ts", found)
        self.assertIn("src/{a,c}.ts", found)
        self.assertNotIn("nowhere.md", found)


class RulesFileTest(unittest.TestCase):
    def test_every_exception_says_why(self):
        rules = json.loads(RULES.read_text())
        for name, entry in rules.items():
            for e in (entry.get("exceptions", []) if isinstance(entry, dict) else []):
                self.assertTrue(e.get("path") and e.get("why"), (name, e))


if __name__ == "__main__":
    unittest.main()
