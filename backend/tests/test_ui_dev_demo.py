"""The dev server never passes sample data off as real (2026-11-25: a sample said the desktop was asleep while it
answered through LM Studio)."""

from __future__ import annotations

from typing import Any

import pytest

from atlas.ui_server.dev import DEMO_LABEL, demo_frames, main


def kinds(frames: list[tuple[str, dict[str, Any]]]) -> set[str]:
    return {t for t, _ in frames}


def weather(frames: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    (payload,) = [p for t, p in frames if t == "panel.update" and p["panel_id"] == "weather"]
    return payload


def test_scripted_model_shows_the_samples_with_the_weather_labelled() -> None:
    frames = demo_frames(real_model=False, demo_panels=None)
    assert kinds(frames) == {"host.status", "panel.update"}
    assert weather(frames)["data"]["location_label"] == DEMO_LABEL


def test_real_model_shows_no_samples_by_default() -> None:
    assert demo_frames(real_model=True, demo_panels=None) == []


def test_real_model_never_gets_the_sample_host_status() -> None:
    frames = demo_frames(real_model=True, demo_panels=True)
    assert kinds(frames) == {"panel.update"}  # panels on request, labelled
    assert weather(frames)["data"]["location_label"] == DEMO_LABEL


def test_samples_can_be_turned_off_with_the_scripted_model() -> None:
    assert kinds(demo_frames(real_model=False, demo_panels=False)) == {"host.status"}


@pytest.mark.parametrize(("argv", "expected"), [([], None), (["--demo-panels"], True), (["--no-demo-panels"], False)])
def test_cli_flag(monkeypatch: pytest.MonkeyPatch, argv: list[str], expected: bool | None) -> None:
    seen: dict[str, Any] = {}

    async def fake_run(args: Any) -> int:
        seen["demo_panels"] = args.demo_panels
        return 0

    monkeypatch.setattr("atlas.ui_server.dev.run", fake_run)
    assert main(argv) == 0
    assert seen["demo_panels"] is expected
