"""Conformance with contracts/hostd.v1.yaml: routes in both directions, and every documented response validates."""

from __future__ import annotations

import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from helpers import PI, call, helper_body, post_helper, sleep_body
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from atlas_hostd import API_VERSION
from atlas_hostd.http import Response
from atlas_hostd.models import LlmServerKind
from atlas_hostd.platform.fake import FakePlatform
from atlas_hostd.service import ROUTES, HostdApp, Listener

SPEC_PATH = (
    Path(os.environ.get("ATLAS_CONTRACTS_DIR") or Path(__file__).resolve().parents[2] / "contracts") / "hostd.v1.yaml"
)
SPEC: dict[str, Any] = yaml.safe_load(SPEC_PATH.read_text())
BASE = "urn:atlas:hostd.v1"
REGISTRY = Registry().with_resource(BASE, Resource.from_contents(SPEC, default_specification=DRAFT202012))

_FORMATS = jsonschema.FormatChecker()
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


@_FORMATS.checks("date-time")
def _is_date_time(value: object) -> bool:
    return not isinstance(value, str) or _RFC3339.fullmatch(value) is not None


METHODS = ("get", "put", "post", "delete")


def spec_operations() -> dict[tuple[str, str], dict[str, Any]]:
    ops: dict[tuple[str, str], dict[str, Any]] = {}
    for path, item in SPEC["paths"].items():
        for method in METHODS:
            if method in item:
                ops[(method.upper(), path)] = item[method]
    return ops


def _resolve_ref(ref: str) -> dict[str, Any]:
    node: Any = SPEC
    for part in ref.removeprefix("#/").split("/"):
        node = node[part]
    return node  # type: ignore[no-any-return]


def response_schema(operation: dict[str, Any], status: int) -> dict[str, Any] | None:
    """The JSON Schema for ``operation``'s ``status`` response, anchored to the spec document; ``None`` for no body."""
    # Statuses not listed explicitly fall under the operation's `default` response (1.1.0).
    spec_response = operation["responses"].get(str(status), operation["responses"].get("default"))
    assert spec_response is not None, f"status {status} is not documented for {operation['operationId']}"
    if "$ref" in spec_response:
        spec_response = _resolve_ref(spec_response["$ref"])
    content = spec_response.get("content")
    if not content:
        return None
    schema = content["application/json"]["schema"]
    return {"$ref": BASE + schema["$ref"]}


def conforms(method: str, template: str, response: Response) -> None:
    operation = spec_operations()[(method, template)]
    schema = response_schema(operation, response.status)
    if schema is None:
        assert response.body is None, "response documented without a body must be empty"
        return
    assert response.body is not None
    jsonschema.Draft202012Validator(schema, registry=REGISTRY, format_checker=_FORMATS).validate(response.body)


# ---- routes ------------------------------------------------------------------------------------------------


def test_spec_is_the_approved_revision() -> None:
    assert SPEC["info"]["version"] == API_VERSION
    assert SPEC["openapi"].startswith("3.1")


def test_every_implemented_route_is_in_the_spec_and_vice_versa() -> None:
    implemented = {(r.method, r.template) for r in ROUTES}
    assert implemented == set(spec_operations())


def test_operation_ids_and_listeners_match_the_spec() -> None:
    ops = spec_operations()
    for route in ROUTES:
        operation = ops[(route.method, route.template)]
        assert operation["operationId"] == route.operation_id
        on_helper = any(s["url"].endswith(":8771") for s in operation.get("servers", []))
        assert (route.listener is Listener.HELPER) == on_helper


def test_spec_server_addresses_match_services_md() -> None:
    urls = {s["url"] for s in SPEC["servers"]}
    assert urls == {"http://10.77.0.2:8770", "http://127.0.0.1:8771"}


# ---- one case per (operationId, documented status) -------------------------------------------------------------

Case = Callable[[HostdApp, FakePlatform], Awaitable[tuple[str, str, Response]]]


async def _health_200(app: HostdApp, p: FakePlatform) -> tuple[str, str, Response]:
    return "GET", "/v1/health", await call(app, "GET", "/v1/health")


async def _health_403(app: HostdApp, p: FakePlatform) -> tuple[str, str, Response]:
    return "GET", "/v1/health", await call(app, "GET", "/v1/health", peer="127.0.0.1")


async def _status_200(app: HostdApp, p: FakePlatform) -> tuple[str, str, Response]:
    await call(app, "PUT", "/v1/leases/l1", {"ttl_s": 60, "reason": "backup"})
    await post_helper(app, idle=10, fullscreen=True, heavy=1)
    return "GET", "/v1/status", await call(app, "GET", "/v1/status")


async def _status_403(app: HostdApp, p: FakePlatform) -> tuple[str, str, Response]:
    return "GET", "/v1/status", await call(app, "GET", "/v1/status", peer="192.0.2.1")


async def _gpu_200(app: HostdApp, p: FakePlatform) -> tuple[str, str, Response]:
    return "GET", "/v1/gpu", await call(app, "GET", "/v1/gpu")


async def _gpu_403(app: HostdApp, p: FakePlatform) -> tuple[str, str, Response]:
    return "GET", "/v1/gpu", await call(app, "GET", "/v1/gpu", peer="10.77.0.2")


async def _gpu_503(app: HostdApp, p: FakePlatform) -> tuple[str, str, Response]:
    p.gpu = None
    return "GET", "/v1/gpu", await call(app, "GET", "/v1/gpu")


async def _activity_200(app: HostdApp, p: FakePlatform) -> tuple[str, str, Response]:
    await post_helper(app)
    return "GET", "/v1/activity", await call(app, "GET", "/v1/activity")


async def _activity_403(app: HostdApp, p: FakePlatform) -> tuple[str, str, Response]:
    return "GET", "/v1/activity", await call(app, "GET", "/v1/activity", peer="127.0.0.1")


async def _sleep_202(app: HostdApp, p: FakePlatform) -> tuple[str, str, Response]:
    return "POST", "/v1/sleep", await call(app, "POST", "/v1/sleep", sleep_body())


async def _sleep_409(app: HostdApp, p: FakePlatform) -> tuple[str, str, Response]:
    p.states = ()
    await post_helper(app, idle=1, fullscreen=True)
    return "POST", "/v1/sleep", await call(app, "POST", "/v1/sleep", sleep_body(first_packet_at=None))


async def _sleep_400(app: HostdApp, p: FakePlatform) -> tuple[str, str, Response]:
    return "POST", "/v1/sleep", await call(app, "POST", "/v1/sleep", {"request_id": "x"})


async def _sleep_403(app: HostdApp, p: FakePlatform) -> tuple[str, str, Response]:
    return "POST", "/v1/sleep", await call(app, "POST", "/v1/sleep", sleep_body(), peer="127.0.0.1")


async def _lease_put_200(app: HostdApp, p: FakePlatform) -> tuple[str, str, Response]:
    return (
        "PUT",
        "/v1/leases/{lease_id}",
        await call(app, "PUT", "/v1/leases/lease-1", {"ttl_s": 120, "reason": "interactive_session"}),
    )


async def _lease_put_400(app: HostdApp, p: FakePlatform) -> tuple[str, str, Response]:
    return "PUT", "/v1/leases/{lease_id}", await call(app, "PUT", "/v1/leases/lease-1", {"ttl_s": 5, "reason": "x"})


async def _lease_put_403(app: HostdApp, p: FakePlatform) -> tuple[str, str, Response]:
    body = {"ttl_s": 120, "reason": "backup"}
    return "PUT", "/v1/leases/{lease_id}", await call(app, "PUT", "/v1/leases/lease-1", body, peer="127.0.0.1")


async def _lease_delete_204(app: HostdApp, p: FakePlatform) -> tuple[str, str, Response]:
    await call(app, "PUT", "/v1/leases/lease-1", {"ttl_s": 120, "reason": "backup"})
    return "DELETE", "/v1/leases/{lease_id}", await call(app, "DELETE", "/v1/leases/lease-1")


async def _lease_delete_400(app: HostdApp, p: FakePlatform) -> tuple[str, str, Response]:
    return "DELETE", "/v1/leases/{lease_id}", await call(app, "DELETE", "/v1/leases/-bad")


async def _lease_delete_403(app: HostdApp, p: FakePlatform) -> tuple[str, str, Response]:
    return "DELETE", "/v1/leases/{lease_id}", await call(app, "DELETE", "/v1/leases/lease-1", peer="127.0.0.1")


async def _helper_204(app: HostdApp, p: FakePlatform) -> tuple[str, str, Response]:
    return await _helper(app, helper_body(), "127.0.0.1")


async def _helper_400(app: HostdApp, p: FakePlatform) -> tuple[str, str, Response]:
    return await _helper(app, {"session_id": 1}, "127.0.0.1")


async def _helper_403(app: HostdApp, p: FakePlatform) -> tuple[str, str, Response]:
    return await _helper(app, helper_body(), PI)


async def _helper(app: HostdApp, body: Any, peer: str) -> tuple[str, str, Response]:
    response = await call(app, "POST", "/v1/helper/activity", body, peer=peer, listener=Listener.HELPER)
    return "POST", "/v1/helper/activity", response


CASES: dict[tuple[str, int], Case] = {
    ("getHealth", 200): _health_200,
    ("getHealth", 403): _health_403,
    ("getStatus", 200): _status_200,
    ("getStatus", 403): _status_403,
    ("getGpu", 200): _gpu_200,
    ("getGpu", 403): _gpu_403,
    ("getGpu", 503): _gpu_503,
    ("getActivity", 200): _activity_200,
    ("getActivity", 403): _activity_403,
    ("requestSleep", 202): _sleep_202,
    ("requestSleep", 409): _sleep_409,
    ("requestSleep", 400): _sleep_400,
    ("requestSleep", 403): _sleep_403,
    ("putLease", 200): _lease_put_200,
    ("putLease", 400): _lease_put_400,
    ("putLease", 403): _lease_put_403,
    ("deleteLease", 204): _lease_delete_204,
    ("deleteLease", 400): _lease_delete_400,
    ("deleteLease", 403): _lease_delete_403,
    ("postHelperActivity", 204): _helper_204,
    ("postHelperActivity", 400): _helper_400,
    ("postHelperActivity", 403): _helper_403,
}


def _documented() -> list[tuple[str, int]]:
    return [
        (op["operationId"], int(code))
        for op in spec_operations().values()
        for code in op["responses"]
        if code != "default"
    ]


def test_every_operation_has_a_default_error_response() -> None:
    for op in spec_operations().values():
        assert "default" in op["responses"], op["operationId"]
        assert response_schema(op, 418) == {"$ref": BASE + "#/components/schemas/Error"}


@pytest.mark.parametrize(("operation_id", "status"), _documented())
async def test_documented_response_is_produced_and_validates(
    app: HostdApp, platform: FakePlatform, operation_id: str, status: int
) -> None:
    case = CASES.get((operation_id, status))
    assert case is not None, f"no conformance case for documented response {operation_id} {status}"
    method, template, response = await case(app, platform)
    assert response.status == status
    conforms(method, template, response)


def test_no_case_for_an_undocumented_response() -> None:
    assert set(CASES) == set(_documented())


async def test_status_without_gpu_validates(app: HostdApp, platform: FakePlatform) -> None:
    platform.gpu = None
    platform.resume = None
    platform.llm_kind = LlmServerKind.NONE
    conforms("GET", "/v1/status", await call(app, "GET", "/v1/status"))


async def test_status_with_sixteen_leases_validates(app: HostdApp) -> None:
    for i in range(16):
        await call(app, "PUT", f"/v1/leases/l{i}", {"ttl_s": 60, "reason": "backup"})
    response = await call(app, "GET", "/v1/status")
    assert response.body is not None and len(response.body["leases"]) == 16
    conforms("GET", "/v1/status", response)


async def test_every_refusal_reason_validates(app: HostdApp, platform: FakePlatform) -> None:
    platform.states = ()
    await post_helper(app, idle=1, fullscreen=True, heavy=1)
    await call(app, "PUT", "/v1/leases/l1", {"ttl_s": 60, "reason": "backup"})
    response = await call(app, "POST", "/v1/sleep", sleep_body(first_packet_at=None))
    assert response.body is not None and len(response.body["reasons"]) == 6
    conforms("POST", "/v1/sleep", response)


async def test_errors_outside_the_listed_statuses_validate_against_default(app: HostdApp) -> None:
    not_found = await call(app, "GET", "/v1/missing")
    wrong_method = await call(app, "POST", "/v1/status")
    assert (not_found.status, wrong_method.status) == (404, 405)
    assert not_found.body is not None and not_found.body["code"] == "NOT_FOUND"
    assert wrong_method.body is not None and wrong_method.body["code"] == "METHOD_NOT_ALLOWED"
    conforms("GET", "/v1/status", not_found)
    conforms("GET", "/v1/status", wrong_method)


async def test_invalid_lease_id_codes(app: HostdApp) -> None:
    put = await call(app, "PUT", "/v1/leases/-bad", {"ttl_s": 60, "reason": "backup"})
    delete = await call(app, "DELETE", "/v1/leases/-bad")
    for response in (put, delete):
        assert response.status == 400 and response.body is not None
        assert response.body["code"] == "INVALID_LEASE_ID"
    conforms("PUT", "/v1/leases/{lease_id}", put)
