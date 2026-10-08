import importlib.util
import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("verify", ROOT / "scripts" / "verify-settings.py")
import sys
sys.path.insert(0, str(ROOT / "scripts"))
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)
LISTS = {l["path"]: l for l in json.loads((ROOT / "rulesets" / "repo-lists.json").read_text())["lists"]}


class MissingFrom(unittest.TestCase):
    def test_profile_table(self):
        l = LISTS["profile/README.md"]
        text = "| [platform](https://github.com/Akenon-Studio/platform) | x |\n| [.github](https://github.com/Akenon-Studio/.github) | y |"
        self.assertEqual(verify.missing_from(text, ["platform", ".github", "firmware", ".github-private"],
                                             l["pattern"], l["skip"]), ["firmware"])

    def test_design_tree(self):
        l = LISTS["design.md"]
        text = "├── hardware        boards\n├── handbook        docs\n└── .github-private private"
        self.assertEqual(verify.missing_from(text, ["hardware", "firmware", ".github-private"],
                                             l["pattern"], l["skip"]), ["firmware"])

    def test_name_inside_another_word_does_not_count(self):
        l = LISTS["design.md"]
        self.assertEqual(verify.missing_from("├── platformx  x", ["platform"], l["pattern"]), ["platform"])


if __name__ == "__main__":
    unittest.main()
