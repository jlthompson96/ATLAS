"""The Windows module must be unimportable (not merely broken) on other platforms, and must always compile."""

from __future__ import annotations

import importlib
import py_compile
import sys
from pathlib import Path

import pytest

WINDOWS_MODULE = Path(__file__).resolve().parents[1] / "atlas_hostd" / "platform" / "windows.py"


def test_windows_module_compiles_everywhere() -> None:
    py_compile.compile(str(WINDOWS_MODULE), doraise=True, cfile=str(WINDOWS_MODULE.with_suffix(".tmpc")))
    WINDOWS_MODULE.with_suffix(".tmpc").unlink()


@pytest.mark.skipif(sys.platform == "win32", reason="guard only applies off Windows")
def test_windows_module_refuses_import_off_windows() -> None:
    with pytest.raises(ImportError, match="Windows-only"):
        importlib.import_module("atlas_hostd.platform.windows")


@pytest.mark.skipif(sys.platform == "win32", reason="guard only applies off Windows")
def test_real_platform_is_refused_by_the_entry_point() -> None:
    from atlas_hostd.__main__ import build_platform

    with pytest.raises(SystemExit):
        build_platform(fake=False)
