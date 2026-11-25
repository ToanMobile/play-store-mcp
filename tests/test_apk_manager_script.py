"""Regression tests for apk_manager.sh package-name validation.

Package names from an XAPK manifest.json or an OBB file name flow into
``adb shell ...``, which the device shell re-parses, so every name must be
validated before any adb call. The script is interactive (it connects to a
device at import), so the helper is extracted and exercised in isolation.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "apk_manager.sh"
# Prefer the system /bin/bash (3.2 on macOS) — the script must work there, not only on Homebrew bash 5.
BASH = "/bin/bash" if Path("/bin/bash").exists() else shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")


def _extract_function(name: str) -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.MULTILINE | re.DOTALL)
    assert match, f"{name}() not found in {SCRIPT.name}"
    return match.group(0)


def _run_validator(func: str, value: str) -> subprocess.CompletedProcess[str]:
    helpers = _extract_function("is_valid_package_name") + _extract_function(
        "require_valid_package_name"
    )
    assert BASH is not None
    return subprocess.run(  # noqa: S603 - fixed interpreter, value passed as argv
        [BASH, "-c", f'{helpers}\n{func} "$1"', "bash", value],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def test_script_syntax_is_valid() -> None:
    assert BASH is not None
    result = subprocess.run(  # noqa: S603
        [BASH, "-n", str(SCRIPT)], capture_output=True, text=True, check=False, timeout=10
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "name",
    ["com.waze", "vn.vietmap.live", "com.example.my_app2", "a.b", "Com.Example.App"],
)
def test_valid_package_names_accepted(name: str) -> None:
    assert _run_validator("is_valid_package_name", name).returncode == 0
    assert _run_validator("require_valid_package_name", name).returncode == 0


@pytest.mark.parametrize(
    "name",
    [
        "",
        "com",
        "1com.example",
        "com..example",
        "com.example.",
        ".com.example",
        "com.example;reboot",
        "com.example && rm -rf /sdcard",
        "com.example app",
        "com.$(id)",
        "com.`id`",
        "com.example|sh",
        "com.example\nreboot",
        "../../data",
        "com.example'",
        'com.example"',
    ],
)
def test_invalid_package_names_rejected(name: str) -> None:
    assert _run_validator("is_valid_package_name", name).returncode != 0
    result = _run_validator("require_valid_package_name", name)
    assert result.returncode != 0
    assert "không hợp lệ" in result.stderr


def test_every_adb_shell_package_use_is_guarded() -> None:
    """Each function interpolating a package into ``adb shell`` validates it first."""
    text = SCRIPT.read_text(encoding="utf-8")
    uninstall = _extract_function("uninstall_pkg")
    assert uninstall.index("is_valid_package_name") < uninstall.index("shell pm uninstall")
    install_xapk = _extract_function("install_xapk")
    assert install_xapk.index('require_valid_package_name "$obb_pkg"') < install_xapk.index(
        "shell mkdir"
    )
    assert install_xapk.index('require_valid_package_name "$pkg"') < install_xapk.index(
        "run_install install-multiple"
    )
    assert 'require_valid_package_name "$PACKAGE"' in text


def test_xapk_with_symlink_is_rejected_before_any_adb_use() -> None:
    """A malicious XAPK can carry a symlink (e.g. main.1.x.obb -> ~/.ssh/id_rsa) that
    `adb push` would follow, copying a host file to world-readable /sdcard."""
    text = SCRIPT.read_text(encoding="utf-8")
    guard = text.index('find "$temp_dir" -type l -print -quit')
    first_find = text.index('find "$temp_dir" -maxdepth 3 -type f -name "*.apk"')
    assert guard < first_find
    assert 'find "$temp_dir" -type f -name "*.obb"' in text


def test_symlink_guard_detects_links(tmp_path: Path) -> None:
    (tmp_path / "x.obb").symlink_to("/etc/hosts")
    assert BASH is not None
    out = subprocess.run(  # noqa: S603 - fixed interpreter and script
        [
            BASH,
            "-c",
            '[ -n "$(find "$1" -type l -print -quit)" ] && echo LINK',
            "bash",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert out.stdout.strip() == "LINK"


def test_obb_package_fallback_strips_prefix_with_bsd_sed() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "sed -E 's/^(main|patch)\\.[0-9]+\\.//'" in text
    out = subprocess.run(
        ["sed", "-E", r"s/^(main|patch)\.[0-9]+\.//"],
        input="main.12.com.foo.bar\n",
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert out.stdout.strip() == "com.foo.bar"
