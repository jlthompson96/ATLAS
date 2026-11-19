"""A small, strict, dependency-free schema language for tool arguments and reader output.

``jsonschema`` is a dev-only dependency, so atlas-core validates model output with this instead
(same approach as ``atlas.audit.record``). It is deliberately narrow:

* objects are closed (unknown keys are errors) and list their required keys;
* strings have a maximum length and an optional full-match pattern;
* integers are real integers (``bool`` is not an integer) with bounds;
* arrays have item limits;
* ``Ref`` is a string that must be exactly one ``$VAR`` name;
* ``RefOr`` accepts a ``$VAR`` name or the wrapped field; literal text that *embeds* a ``$VAR``-like
  token is refused rather than guessed at (§2.9).

Each field can also emit the equivalent JSON Schema, which is what the backend sees
(``ToolSpec.parameters`` and ``response_schema``).
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, NoReturn

from atlas.core.taint import EMBEDDED_VAR_RE, VAR_NAME_RE

MAX_JSON_BYTES: Final = 16 * 1024
MAX_JSON_DEPTH: Final = 8


class SchemaError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors[:5]))
        self.errors = errors


def _no_constants(name: str) -> NoReturn:
    raise ValueError(f"{name} is not valid JSON here")


def _no_float(text: str) -> NoReturn:
    raise ValueError("floating-point numbers are not accepted")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def strict_json_loads(text: str, *, max_bytes: int = MAX_JSON_BYTES, max_depth: int = MAX_JSON_DEPTH) -> Any:
    """Parse model-produced JSON: size-capped, no duplicate keys, no NaN/Infinity, no floats, depth-capped."""
    if not isinstance(text, str):
        raise ValueError("expected text")
    if len(text.encode("utf-8", "surrogatepass")) > max_bytes:
        raise ValueError("JSON too large")
    try:
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_no_constants, parse_float=_no_float)
    except RecursionError as exc:
        raise ValueError("JSON nested too deeply") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("malformed JSON") from exc
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > max_depth:
            raise ValueError("JSON nested too deeply")
        if isinstance(node, dict):
            stack.extend((v, depth + 1) for v in node.values())
        elif isinstance(node, list):
            stack.extend((v, depth + 1) for v in node)
    return value


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class Field(ABC):
    description: str = ""

    @abstractmethod
    def check(self, value: Any, path: str) -> list[str]: ...

    @abstractmethod
    def json_schema(self) -> dict[str, Any]: ...

    def _with_description(self, schema: dict[str, Any]) -> dict[str, Any]:
        if self.description:
            schema["description"] = self.description
        return schema


@dataclass(frozen=True)
class Str(Field):
    max_len: int
    min_len: int = 0
    pattern: str | None = None
    single_line: bool = False
    description: str = ""
    # Reader output only: the value is free text from untrusted input and becomes a tainted $VAR.
    tainted: bool = False

    def check(self, value: Any, path: str) -> list[str]:
        if not isinstance(value, str):
            return [f"{path}: must be a string"]
        if len(value) > self.max_len:
            return [f"{path}: longer than {self.max_len}"]
        if len(value) < self.min_len:
            return [f"{path}: shorter than {self.min_len}"]
        if self.single_line and any(c in value for c in "\r\n\x00"):
            return [f"{path}: must be a single line"]
        if self.pattern is not None and re.fullmatch(self.pattern, value) is None:
            return [f"{path}: does not match the required format"]
        return []

    def json_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": "string", "maxLength": self.max_len}
        if self.min_len:
            schema["minLength"] = self.min_len
        if self.pattern is not None:
            schema["pattern"] = f"^(?:{self.pattern})$"
        return self._with_description(schema)


@dataclass(frozen=True)
class PlainText(Str):
    """Planner-supplied literal text: refuses embedded ``$VAR`` tokens (use ``RefOr`` or parts)."""

    def check(self, value: Any, path: str) -> list[str]:
        errors = super().check(value, path)
        if not errors and EMBEDDED_VAR_RE.search(value) is not None:
            return [f"{path}: embeds a variable reference; pass the $VAR as its own value"]
        return errors


@dataclass(frozen=True)
class Enum(Field):
    values: tuple[str, ...]
    description: str = ""

    def check(self, value: Any, path: str) -> list[str]:
        return [] if isinstance(value, str) and value in self.values else [f"{path}: not one of {list(self.values)}"]

    def json_schema(self) -> dict[str, Any]:
        return self._with_description({"type": "string", "enum": list(self.values)})


@dataclass(frozen=True)
class Int(Field):
    minimum: int
    maximum: int
    description: str = ""

    def check(self, value: Any, path: str) -> list[str]:
        if not _is_int(value):
            return [f"{path}: must be an integer"]
        if not self.minimum <= value <= self.maximum:
            return [f"{path}: must be between {self.minimum} and {self.maximum}"]
        return []

    def json_schema(self) -> dict[str, Any]:
        return self._with_description({"type": "integer", "minimum": self.minimum, "maximum": self.maximum})


@dataclass(frozen=True)
class Bool(Field):
    description: str = ""

    def check(self, value: Any, path: str) -> list[str]:
        return [] if isinstance(value, bool) else [f"{path}: must be a boolean"]

    def json_schema(self) -> dict[str, Any]:
        return self._with_description({"type": "boolean"})


@dataclass(frozen=True)
class Ref(Field):
    """Exactly one ``$VAR`` name."""

    description: str = ""

    def check(self, value: Any, path: str) -> list[str]:
        if isinstance(value, str) and VAR_NAME_RE.fullmatch(value) is not None:
            return []
        return [f"{path}: must be a variable reference like $EMAIL_1"]

    def json_schema(self) -> dict[str, Any]:
        return self._with_description({"type": "string", "pattern": f"^{VAR_NAME_RE.pattern}$"})


@dataclass(frozen=True)
class RefOr(Field):
    """A ``$VAR`` name, or a value matching ``inner``."""

    inner: Field
    description: str = ""

    def check(self, value: Any, path: str) -> list[str]:
        if isinstance(value, str) and VAR_NAME_RE.fullmatch(value) is not None:
            return []
        return self.inner.check(value, path)

    def json_schema(self) -> dict[str, Any]:
        return self._with_description({"anyOf": [Ref().json_schema(), self.inner.json_schema()]})


@dataclass(frozen=True)
class Array(Field):
    item: Field
    max_items: int
    min_items: int = 0
    description: str = ""
    unique: bool = False  # scalar items only; reader enum lists use it so order and repeats carry no data

    def check(self, value: Any, path: str) -> list[str]:
        if not isinstance(value, list):
            return [f"{path}: must be an array"]
        if not self.min_items <= len(value) <= self.max_items:
            return [f"{path}: must have {self.min_items}..{self.max_items} items"]
        errors: list[str] = []
        for i, element in enumerate(value):
            errors += self.item.check(element, f"{path}[{i}]")
        if self.unique and not errors and len({json.dumps(v, sort_keys=True) for v in value}) != len(value):
            errors.append(f"{path}: items must be unique")
        return errors

    def json_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": "array", "items": self.item.json_schema(), "maxItems": self.max_items}
        if self.min_items:
            schema["minItems"] = self.min_items
        if self.unique:
            schema["uniqueItems"] = True
        return self._with_description(schema)


@dataclass(frozen=True)
class Obj(Field):
    fields: Mapping[str, Field]
    required: frozenset[str] = field(default_factory=frozenset)
    description: str = ""

    def __post_init__(self) -> None:
        unknown = self.required - set(self.fields)
        if unknown:
            raise ValueError(f"required keys not declared: {sorted(unknown)}")

    def check(self, value: Any, path: str) -> list[str]:
        if not isinstance(value, dict):
            return [f"{path}: must be an object"]
        errors = [f"{path}.{name}: required" for name in sorted(self.required) if name not in value]
        for name, member in value.items():
            spec = self.fields.get(name) if isinstance(name, str) else None
            if spec is None:
                errors.append(f"{path}: unknown key")
            else:
                errors += spec.check(member, f"{path}.{name}")
        return errors

    def json_schema(self) -> dict[str, Any]:
        return self._with_description(
            {
                "type": "object",
                "additionalProperties": False,
                "required": sorted(self.required),
                "properties": {name: spec.json_schema() for name, spec in self.fields.items()},
            }
        )

    def validate(self, value: Any, path: str = "args") -> dict[str, Any]:
        errors = self.check(value, path)
        if errors:
            raise SchemaError(errors)
        assert isinstance(value, dict)  # noqa: S101 - narrowed by check()
        return value


def collect_refs(value: Any) -> list[str]:
    """Every whole-string ``$VAR`` name inside an already-validated value."""
    found: list[str] = []
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            if VAR_NAME_RE.fullmatch(node) is not None:
                found.append(node)
        elif isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found
