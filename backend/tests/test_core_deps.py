"""The core and executor Pi locks: strict-validator clean, and the same reviewed pins as the audit lock."""

from __future__ import annotations

import tomllib

import pytest

from tests.test_review_deploy import REPO, bash

BACKEND = REPO / "backend"


def _pins(name: str) -> list[str]:
    return [
        line for line in (BACKEND / f"requirements-{name}.lock").read_text().splitlines() if not line.startswith("#")
    ]


@pytest.mark.parametrize("name", ["core", "executor"])
def test_lock_passes_the_release_validator_and_matches_the_audit_lock(name: str) -> None:
    result = bash(f'validate_lock "{BACKEND / f"requirements-{name}.lock"}"')
    assert result.returncode == 0, result.stderr
    assert _pins(name) == _pins("audit")


def test_extras_pin_the_same_cryptography_range() -> None:
    extras = tomllib.loads((BACKEND / "pyproject.toml").read_text())["project"]["optional-dependencies"]
    assert extras["core"] == extras["executor"] == extras["audit"] == ["cryptography>=46,<48"]
    assert tomllib.loads((BACKEND / "pyproject.toml").read_text())["project"]["dependencies"] == []
