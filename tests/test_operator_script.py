"""Regression tests for the shared operator console's executable JavaScript."""
import re
import shutil
import subprocess

import pytest

from portfolio_tracker.web import OPERATOR_PAGE_TEMPLATE


def test_shared_operator_script_parses(tmp_path):
    if not shutil.which("node"):
        pytest.skip("node is required for JavaScript syntax validation")
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", OPERATOR_PAGE_TEMPLATE, re.S)
    assert scripts, "shared shell must include its operator script"
    script = re.sub(r"\{\{.*?\}\}", "'audit'", scripts[0])
    path = tmp_path / "operator.js"
    path.write_text(script)
    result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_agent_toggle_uses_supported_method():
    script = re.search(r"<script(?:\s[^>]*)?>(.*?)</script>", OPERATOR_PAGE_TEMPLATE, re.S).group(1)
    assert "action==='toggle'?'PUT':'POST'" in script
