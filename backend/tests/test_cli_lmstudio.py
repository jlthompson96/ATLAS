"""``python -m atlas.core.cli --backend lmstudio`` end to end against a fake LM Studio and a fake hostd.

The CLI runs its own event loop, so the fake servers live on a background thread (``threaded``).
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from atlas.core.cli import main
from tests.net_support import TOKEN, Reply, Request, closed_port, sse, threaded
from tests.test_power_hostd import HEALTH

MODEL = "qwen-test-9b"


def lmstudio(request: Request) -> Reply:
    if request.headers.get("authorization") != f"Bearer {TOKEN}":
        return Reply.of_json({"error": {"message": "invalid api key"}}, 401)
    if request.path == "/v1/models":
        return Reply.of_json({"data": [{"id": MODEL}]})
    if request.path == "/api/v0/models":
        return Reply.of_json({"data": [{"id": MODEL, "type": "llm", "state": "loaded"}]})
    if request.path == "/v1/chat/completions":
        sent = request.json()
        if sent.get("max_tokens") == 1:
            return Reply.of_json({"choices": [{"message": {"content": "p"}}]})
        roles = [m["role"] for m in sent["messages"]]
        if "tools" in sent and roles[-1] == "user" and "weather" in sent["messages"][-1]["content"]:
            call = {"index": 0, "id": "call_1", "function": {"name": "get_time", "arguments": "{}"}}
            return Reply(
                chunks=sse(
                    {"choices": [{"index": 0, "delta": {"tool_calls": [call]}}]},
                    {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
                    "[DONE]",
                )
            )
        text = "Done." if roles[-1] == "tool" else "Hello from the fake model."
        return Reply(
            chunks=sse(
                {"choices": [{"index": 0, "delta": {"content": text}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                "[DONE]",
            )
        )
    return Reply.of_json({}, 404)


def hostd(request: Request) -> Reply:
    if request.path == "/v1/health":
        return Reply.of_json(HEALTH)
    return Reply.of_json({"code": "NOT_FOUND", "message": "x"}, 404)


def token_file(tmp_path: Path, token: str = TOKEN) -> str:
    f = tmp_path / "lmstudio-api-token"
    f.write_text(token + "\n", encoding="utf-8")
    return str(f)


def chat(argv: list[str], lines: str) -> str:
    out = io.StringIO()
    assert main(argv, io.StringIO(lines + "/quit\n"), out) == 0
    return out.getvalue()


def test_chat_with_the_real_backend_path_runs_the_full_pipeline(tmp_path: Path) -> None:
    with threaded(lmstudio) as lms:
        text = chat(
            ["--backend", "lmstudio", "--base-url", lms.url, "--token-file", token_file(tmp_path), "--no-hostd"],
            "hello\nwhat's the weather\n",
        )
        chats = [r for r in lms.requests if r.path == "/v1/chat/completions" and r.json().get("max_tokens") != 1]
    assert f"model {MODEL}" in text  # discovered the loaded model
    assert "Hello from the fake model." in text
    assert "Done." in text  # tool call -> get_time ran in the policy engine -> model answered
    assert any(c.json()["messages"][0]["role"] == "system" for c in chats)
    assert all(c.headers["authorization"] == f"Bearer {TOKEN}" for c in chats)
    assert TOKEN not in text


def test_grammar_answers_locally_and_never_reaches_the_model(tmp_path: Path) -> None:
    with threaded(lmstudio) as lms:
        text = chat(
            [
                "--backend",
                "lmstudio",
                "--base-url",
                lms.url,
                "--token-file",
                token_file(tmp_path),
                "--no-hostd",
                "--model",
                MODEL,
            ],
            "what time is it\nwhat's the date\nis the desktop awake\n",
        )
        assert [r for r in lms.requests if r.path == "/v1/chat/completions"] == []
    assert "It's " in text and "Today is " in text and "no desktop connected" in text


def test_with_hostd_a_ready_core_is_used_without_waking(tmp_path: Path) -> None:
    with threaded(lmstudio) as lms, threaded(hostd) as hd:
        text = chat(
            [
                "--backend",
                "lmstudio",
                "--base-url",
                lms.url,
                "--token-file",
                token_file(tmp_path),
                "--hostd-url",
                hd.url,
                "--model",
                MODEL,
            ],
            "hello\nis the desktop awake\n",
        )
    assert "Hello from the fake model." in text
    assert "waking" not in text.lower() and "awake" in text  # "The core is awake."


def test_rotated_token_gives_a_clear_error_and_saves_the_request(tmp_path: Path) -> None:
    with threaded(lmstudio) as lms:
        text = chat(
            [
                "--backend",
                "lmstudio",
                "--base-url",
                lms.url,
                "--token-file",
                token_file(tmp_path, "stale"),
                "--hostd-url",
                f"http://127.0.0.1:{closed_port()}",
                "--model",
                MODEL,
            ],
            "hello\n/queue\n",
        )
        assert [r for r in lms.requests if r.path == "/v1/chat/completions"] == []
    assert "rejected my credentials" in text and "[alert critical LLM_AUTH_FAILED]" in text
    assert "1 saved request(s)" in text


def test_model_not_ready_without_hostd_is_reported_not_raised(tmp_path: Path) -> None:
    text = chat(
        [
            "--backend",
            "lmstudio",
            "--base-url",
            f"http://127.0.0.1:{closed_port()}",
            "--token-file",
            token_file(tmp_path),
            "--no-hostd",
            "--model",
            MODEL,
        ],
        "hello\n",
    )
    assert "The model isn't ready (packet: unreachable)." in text


def test_a_missing_model_and_a_bad_token_file_are_configuration_errors(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="no model"):
        chat(["--backend", "lmstudio", "--base-url", f"http://127.0.0.1:{closed_port()}", "--no-hostd"], "")
    with pytest.raises(SystemExit, match="token"):
        chat(["--backend", "lmstudio", "--token-file", str(tmp_path / "nope"), "--no-hostd"], "")
    with pytest.raises(SystemExit, match=r"MAC|http"):
        chat(["--backend", "lmstudio", "--mac", "nonsense", "--model", MODEL, "--token-file", token_file(tmp_path)], "")


def test_the_fake_backend_remains_the_default() -> None:
    text = chat([], "what time is it\n")
    assert "scripted model" in text and "now: " in text
    assert json.dumps(text)
