import os
from pathlib import Path
import subprocess

import pytest


PROJECT_ROOT = Path(__file__).parents[2]


def test_linux_demo_script_is_available_and_has_valid_bash_syntax():
    script = PROJECT_ROOT / "scripts" / "demo.sh"

    assert script.is_file(), "Linux demo script is missing"
    script_text = script.read_text(encoding="utf-8")
    assert script_text.startswith("#!/usr/bin/env bash\n")
    assert "--headless" in script_text
    assert "--use-existing-fixture" in script_text
    if os.name == "nt":
        bash = Path("C:/Program Files/Git/bin/bash.exe")
        if not bash.is_file():
            pytest.skip("Git Bash is required to syntax-check the Linux demo script on Windows")
    else:
        bash = Path("bash")

    result = subprocess.run([str(bash), "-n", str(script)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
