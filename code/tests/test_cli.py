"""Command-line tests.

The README documents `--out`, `--requests`, `--quiet`, `--no-validate` and
`--ocr`; `code/main.py` accepts `--out` as an alias of `--output`.  These tests
keep the documented commands and the parser in agreement.
"""

from __future__ import annotations

import shlex
import sys
import unittest
from pathlib import Path

_CODE_DIR = Path(__file__).resolve().parent.parent
if str(_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(_CODE_DIR))

from main import build_parser  # noqa: E402

REPO_ROOT = _CODE_DIR.parent


class CliTests(unittest.TestCase):
    def test_out_alias_and_output_are_the_same_destination(self):
        parser = build_parser()
        short = parser.parse_args(["--out", "/tmp/x.csv"])
        long = parser.parse_args(["--output", "/tmp/x.csv"])
        self.assertEqual(short.output, Path("/tmp/x.csv"))
        self.assertEqual(short.output, long.output)
        self.assertIsNone(parser.parse_args([]).output)

    def test_documented_readme_commands_parse(self):
        parser = build_parser()
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        commands = [
            line.strip()
            for line in readme.splitlines()
            if line.startswith("python3 code/")
        ]
        self.assertTrue(commands, "README documents no runnable commands")
        checked = 0
        for command in commands:
            tokens = shlex.split(command.split("#", 1)[0].strip())
            if not tokens or not tokens[0].startswith("python3"):
                continue
            if tokens[1] != "code/main.py":
                continue
            parser.parse_args(tokens[2:])
            checked += 1
        self.assertGreaterEqual(checked, 3, "expected several documented main.py commands")


if __name__ == "__main__":
    unittest.main()
