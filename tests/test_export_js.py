from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_export_helpers_with_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    result = subprocess.run(
        [node, "--test", "tests/export.test.js"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
