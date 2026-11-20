"""UW-1 (PLAN.md §8.8): the unattended registry contains no tool tagged ``write``. CI fails otherwise.

Checked four ways, so a regression in any one layer still fails:

1. every registry atlas-core can build, for every context, holds no write tool (and unattended
   holds exactly the expected read/reader/propose tools);
2. ``ToolRegistry`` refuses to be constructed with a write tool in any context, or a tool outside
   the context's tags;
3. no class under atlas.core/policy/memory is a write tool, and the only write tools anywhere are
   the executor's senders;
4. importing the whole planner side never imports ``atlas.executor.senders`` / ``service`` /
   ``server`` (checked in a fresh interpreter), and no planner module imports them statically.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
import subprocess
import sys
from pathlib import Path
from typing import Any, ClassVar

import pytest

import atlas
from atlas.core.registry import REGISTRY_BUILDERS, RegistryError, ToolRegistry
from atlas.core.schema import Obj
from atlas.core.tools import Decision, Prepared, RequestCtx, Tool, ToolOutput, text_output
from atlas.executor.senders import FakeSender, Sender
from atlas.policy.types import CONTEXT_TAGS, Context, ToolTag
from tests.core_support import Harness

BACKEND = Path(__file__).resolve().parents[1]
PLANNER_PACKAGES = ("atlas.core", "atlas.policy", "atlas.memory")
FORBIDDEN_FOR_PLANNER = (
    "atlas.executor.senders",
    "atlas.executor.service",
    "atlas.executor.server",
    "atlas.executor.verify",
)


def _deps() -> Any:
    return Harness().runtime.deps


@pytest.mark.parametrize("context", list(Context))
def test_uw1_no_registry_contains_a_write_tool(context: Context) -> None:
    registry = REGISTRY_BUILDERS[context](_deps())
    for tool in registry.tools():
        assert tool.tag is not ToolTag.WRITE
        assert tool.tag in CONTEXT_TAGS[context]


def test_uw1_unattended_registry_is_exactly_read_reader_and_propose() -> None:
    registry = REGISTRY_BUILDERS[Context.UNATTENDED](_deps())
    assert registry.names() == ["ask_reader", "get_time", "propose_action", "read_index", "search_memory"]
    assert {t.tag for t in registry.tools()} == {ToolTag.READ, ToolTag.READER, ToolTag.PROPOSE}


def test_quarantined_and_system_contexts_have_no_tools() -> None:
    assert REGISTRY_BUILDERS[Context.QUARANTINED](_deps()).names() == []
    assert REGISTRY_BUILDERS[Context.SYSTEM](_deps()).names() == []


def test_write_appears_in_no_context_tags() -> None:
    assert all(ToolTag.WRITE not in tags for tags in CONTEXT_TAGS.values())


class _SneakyWriteTool(Tool):
    name = "send_email"
    tag = ToolTag.WRITE
    description = "would send"
    args: ClassVar[Obj] = Obj({})

    async def run(self, args: Any, prepared: Prepared, decision: Decision, rc: RequestCtx) -> ToolOutput:
        return text_output("sent")


class _QueueTool(_SneakyWriteTool):
    name = "queue_x"
    tag = ToolTag.QUEUE


@pytest.mark.parametrize("context", list(Context))
def test_uw1_a_write_tool_cannot_be_constructed_into_any_registry(context: Context) -> None:
    with pytest.raises(RegistryError):
        ToolRegistry(context, [_SneakyWriteTool()])


def test_uw1_a_sender_cannot_be_registered_as_a_tool() -> None:
    with pytest.raises(RegistryError):
        ToolRegistry(Context.INTERACTIVE, [FakeSender("email.send")])  # type: ignore[list-item]


def test_unattended_registry_refuses_queue_tools() -> None:
    with pytest.raises(RegistryError):
        ToolRegistry(Context.UNATTENDED, [_QueueTool()])
    ToolRegistry(Context.INTERACTIVE, [_QueueTool()])  # allowed interactively


def _modules(package: str) -> list[str]:
    pkg = importlib.import_module(package)
    return [package] + [m.name for m in pkgutil.walk_packages(pkg.__path__, package + ".")]


def test_uw1_no_planner_side_class_is_tagged_write() -> None:
    for package in PLANNER_PACKAGES:
        for name in _modules(package):
            module = importlib.import_module(name)
            for _, cls in inspect.getmembers(module, inspect.isclass):
                if getattr(cls, "tag", None) is ToolTag.WRITE:
                    pytest.fail(f"{cls.__module__}.{cls.__qualname__} is a write tool on the planner side")


def test_the_only_write_tools_are_executor_senders() -> None:
    writers = set()
    for name in _modules("atlas"):
        if name.endswith("__main__") or name.startswith("atlas.ui_server.mock"):
            continue
        module = importlib.import_module(name)
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if getattr(cls, "tag", None) is ToolTag.WRITE and cls.__module__ == name:
                writers.add(name)
    assert writers == {"atlas.executor.senders"}
    assert issubclass(FakeSender, Sender)


def test_uw1_importing_the_planner_side_never_imports_executor_internals() -> None:
    planner = [m for p in PLANNER_PACKAGES for m in _modules(p)]
    code = (
        f"import importlib, sys\nsys.path.insert(0, {str(BACKEND)!r})\n"
        f"for m in {planner!r}: importlib.import_module(m)\n"
        "print(sorted(m for m in sys.modules if m.startswith('atlas.executor')))\n"
    )
    out = subprocess.run(  # noqa: S603 - fixed interpreter, fixed code
        [sys.executable, "-I", "-c", code], cwd=BACKEND, capture_output=True, text=True, check=True
    )
    loaded = set(eval(out.stdout.strip()))  # noqa: S307 - our own repr of a list of names
    assert not loaded & set(FORBIDDEN_FOR_PLANNER), loaded
    assert loaded <= {"atlas.executor", "atlas.executor.approval_record"}


def test_uw1_no_planner_module_imports_executor_internals_statically() -> None:
    offenders = []
    for package in PLANNER_PACKAGES:
        root = BACKEND / package.replace(".", "/")
        for path in root.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
                elif isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                if any(n.startswith(FORBIDDEN_FOR_PLANNER) for n in names):
                    offenders.append(str(path))
    assert offenders == []


def test_the_planner_side_has_no_handle_on_approvals() -> None:
    """Nothing the model can reach (orchestrator, tools, registry, reader, runtime) imports approvals."""
    for name in ("orchestrator", "tools", "registry", "reader", "runtime", "prompt", "pending", "index"):
        tree = ast.parse((BACKEND / "atlas" / "core" / f"{name}.py").read_text(encoding="utf-8"))
        imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        assert not imported & {"atlas.core.approvals", "atlas.executor.approval_record", "atlas.core.executor_link"}, (
            name
        )
    assert atlas.__name__ == "atlas"
