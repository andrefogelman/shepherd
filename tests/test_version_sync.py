"""The version is one number, written in five files a bump must touch.

AGENTS.md's "Versions and releases" names them. CI's `lock` job checks
uv.lock against pyproject.toml; this test checks the other three against it,
so a bump that misses one fails here instead of shipping a plugin that
reports the old version.
"""

from __future__ import annotations

import json
import sys
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import shepherd_dev  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


class TestVersionSync(unittest.TestCase):
    def test_init_and_plugin_manifests_agree_with_pyproject(self):
        with (REPO / "pyproject.toml").open("rb") as fh:
            version = tomllib.load(fh)["project"]["version"]
        self.assertEqual(shepherd_dev.__version__, version, "src/shepherd_dev/__init__.py")
        for rel in (".claude-plugin/plugin.json", "kimi.plugin.json"):
            stated = json.loads((REPO / rel).read_text(encoding="utf-8"))["version"]
            self.assertEqual(stated, version, rel)


if __name__ == "__main__":
    unittest.main()
