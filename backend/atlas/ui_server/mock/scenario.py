"""Scenario files for the mock event server.

A scenario is JSON::

    {
      "name": "conversation",
      "description": "...",
      "baseline": true,                     # apply _baseline.json setup first (default true)
      "setup": [ {"type": ..., "payload": ...} ],   # applied to state before the snapshot
      "steps": [
        {"at": 0,   "emit": {"type": "state.changed", "payload": {"state": "listening"}}},
        {"at": 100, "gen": "mic_levels", "args": {"duration_ms": 2000}},
        {"at": 900, "type": "panel.update", "fixture": "portfolio_unlocked"}
      ]
    }

``at`` is milliseconds from scenario start. Generators (``gen``) expand into ``emit`` steps at load
time so validation sees every frame. Payload templates, resolved at send time:

* ``"@t0"``, ``"@t0+N"``, ``"@t0-N"``: scenario start plus N ms (scaled by ``--speed``).
* ``payload_canonical`` given as an object is serialized canonically; ``payload_sha256: "@auto"``
  is then computed from it.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

Json = dict[str, Any]

SCENARIO_DIR: Final = Path(__file__).parent / "scenarios"
BASELINE: Final = "_baseline"
FRAME_MS: Final = 16
_T0 = re.compile(r"^@t0(?:([+-])(\d+))?$")


@dataclass(frozen=True, slots=True)
class Step:
    at: int
    type: str
    payload: Json


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    description: str
    setup: tuple[Step, ...]
    steps: tuple[Step, ...]

    @property
    def duration_ms(self) -> int:
        return self.steps[-1].at if self.steps else 0


def available() -> list[str]:
    return sorted(p.stem for p in SCENARIO_DIR.glob("*.json") if not p.stem.startswith("_"))


def _read(name: str) -> Json:
    path = SCENARIO_DIR / f"{name}.json"
    if not path.is_file():
        raise FileNotFoundError(f"unknown scenario {name!r}; available: {', '.join(available())}")
    with path.open(encoding="utf-8") as f:
        data: Json = json.load(f)
    return data


def fixtures() -> Json:
    """Named payloads in _baseline.json (e.g. the unlocked portfolio used by portfolio.unlock)."""
    fx: Json = _read(BASELINE).get("fixtures", {})
    return fx


def load(name: str) -> Scenario:
    raw = _read(name)
    setup: list[Step] = []
    if raw.get("baseline", True) and name != BASELINE:
        setup += _emits(_read(BASELINE).get("setup", []))
    setup += _emits(raw.get("setup", []))
    steps: list[Step] = []
    for step in raw.get("steps", []):
        at = int(step["at"])
        if "emit" in step:
            steps.append(Step(at, step["emit"]["type"], step["emit"]["payload"]))
        elif "fixture" in step:
            steps.append(Step(at, step["type"], fixtures()[step["fixture"]]))
        elif "gen" in step:
            gen = GENERATORS.get(step["gen"])
            if gen is None:
                raise ValueError(f"{name}: unknown generator {step['gen']!r}")
            steps.extend(gen(at, **step.get("args", {})))
        else:
            raise ValueError(f"{name}: step at {at} has neither 'emit' nor 'gen'")
    steps.sort(key=lambda s: s.at)  # stable: equal times keep file order
    return Scenario(raw.get("name", name), raw.get("description", ""), tuple(setup), tuple(steps))


def _emits(items: list[Json]) -> list[Step]:
    return [Step(0, i["type"], i["payload"]) for i in items]


# --- Template resolution ------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    """RFC 8785-compatible for the values ATLAS uses (strings, integers, booleans, null, ASCII keys)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def resolve(value: Any, t0: int, speed: float) -> Any:
    if isinstance(value, str):
        m = _T0.match(value)
        if m:
            offset = int(m.group(2) or 0) * (-1 if m.group(1) == "-" else 1)
            return t0 + round(offset / speed)
        return value
    if isinstance(value, list):
        return [resolve(v, t0, speed) for v in value]
    if isinstance(value, dict):
        out = {k: resolve(v, t0, speed) for k, v in value.items()}
        if isinstance(out.get("payload_canonical"), (dict, list)):
            out["payload_canonical"] = canonical_json(out["payload_canonical"])
        if out.get("payload_sha256") == "@auto":
            out["payload_sha256"] = hashlib.sha256(out["payload_canonical"].encode()).hexdigest()
        return out
    return value


# --- Generators -----------------------------------------------------------------------------------


def _speech_envelope(n_frames: int, seed: int, floor: float) -> list[float]:
    """Deterministic speech-like envelope: syllable bumps, short gaps, occasional word pauses."""
    rng = random.Random(seed)  # noqa: S311 - deterministic test signal, not security
    out: list[float] = []
    while len(out) < n_frames:
        length = rng.randint(8, 14)  # 130 to 220 ms syllable
        peak = rng.uniform(0.45, 0.95)
        out += [max(floor, peak * math.sin(math.pi * (i + 0.5) / length) ** 0.7) for i in range(length)]
        gap = rng.randint(12, 22) if rng.random() < 0.18 else rng.randint(0, 4)
        out += [floor + rng.uniform(0, 0.03) for _ in range(gap)]
    return [round(min(1.0, v), 3) for v in out[:n_frames]]


def gen_tts_envelope(
    at: int, duration_ms: int, seed: int = 0, batch_frames: int = 8, lead_ms: int = 150
) -> Iterator[Step]:
    """TTS output envelope. ``at`` is when audio becomes audible; batches are sent ``lead_ms`` early (§4.4)."""
    levels = _speech_envelope(duration_ms // FRAME_MS, seed, floor=0.0)
    for i in range(0, len(levels), batch_frames):
        play = at + i * FRAME_MS
        payload = {
            "stream": "tts",
            "t_play": f"@t0+{play}",
            "frame_ms": FRAME_MS,
            "levels": levels[i : i + batch_frames],
        }
        yield Step(max(0, play - lead_ms), "audio.level", payload)


def gen_mic_levels(at: int, duration_ms: int, seed: int = 0, batch_frames: int = 2) -> Iterator[Step]:
    """Live mic envelope while listening; each batch is sent right after it was captured."""
    levels = _speech_envelope(duration_ms // FRAME_MS, seed + 1000, floor=0.04)
    for i in range(0, len(levels), batch_frames):
        captured = at + i * FRAME_MS
        chunk = levels[i : i + batch_frames]
        payload = {"stream": "mic", "t_play": f"@t0+{captured}", "frame_ms": FRAME_MS, "levels": chunk}
        yield Step(captured + len(chunk) * FRAME_MS, "audio.level", payload)


def gen_wake_progress(
    at: int, stages: Mapping[str, int], until_ms: int, deadline_ms: int = 60_000, tick_ms: int = 1000
) -> Iterator[Step]:
    """``waking_host`` updates every ``tick_ms``. ``stages`` maps stage -> elapsed ms when it is reached."""
    order = sorted(stages.items(), key=lambda kv: kv[1])
    for elapsed in range(0, until_ms + 1, tick_ms):
        stage = [s for s, start in order if start <= elapsed][-1]
        wake = {"stage": stage, "elapsed_ms": elapsed, "deadline_ms": deadline_ms}
        yield Step(at + elapsed, "state.changed", {"state": "waking_host", "wake": wake})


def gen_assistant_reply(
    at: int,
    utterance_id: str,
    text: str,
    source: str = "voice",
    chunk_chars: int = 14,
    interval_ms: int = 70,
    provenance: Json | None = None,
) -> Iterator[Step]:
    """Streams ``assistant.delta`` chunks, then the ``transcript.final``."""
    chunks = [text[i : i + chunk_chars] for i in range(0, len(text), chunk_chars)]
    for n, chunk in enumerate(chunks):
        yield Step(at + n * interval_ms, "assistant.delta", {"utterance_id": utterance_id, "text": chunk})
    final: Json = {"utterance_id": utterance_id, "role": "assistant", "text": text, "source": source}
    if provenance is not None:
        final["provenance"] = provenance
    yield Step(at + len(chunks) * interval_ms, "transcript.final", final)


GENERATORS: Final[dict[str, Callable[..., Iterator[Step]]]] = {
    "tts_envelope": gen_tts_envelope,
    "mic_levels": gen_mic_levels,
    "wake_progress": gen_wake_progress,
    "assistant_reply": gen_assistant_reply,
}
