"""Interface only (Phase 5, PLAN 2.13): the research fetch/search API on ``/run/atlas/egress-research.sock``.

NOT IMPLEMENTED. This module fixes the shape so ``atlas-research`` (a ``PrivateNetwork=yes`` sandbox) and
``atlas-egress`` can be built against it. Nothing here is imported by the proxy, and no listener exists
until Phase 5. Binding rules for the implementation:

* The socket peer must be ``atlas-research`` (peer credentials, as for the audit socket).
* Budgets (steps, pages, bytes, wall time), the denylist, the private-address block and provenance
  tagging are enforced here, never by the caller or the LLM.
* Every request is audited as ``kind=egress`` with ``mode=fetch`` or ``mode=search`` (audit-record.v1:
  ``url`` for fetches only, ``query_sha256`` for searches, never the query text).
* Fetches reuse the same address policy as the CONNECT proxy (``atlas.egress.addresses``); port 80 is
  allowed for research only (contracts/services.md section 3), and redirects are re-checked per hop.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class FetchRequest:
    task_id: str
    url: str


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: int
    content_sha256: str
    body: bytes
    tainted: bool = True  # fetched web content is always untrusted (PLAN 2.9)


@dataclass(frozen=True)
class SearchRequest:
    task_id: str
    query: str  # audited as a hash only; the text goes to the research query log


@dataclass(frozen=True)
class SearchHit:
    url: str
    title: str
    snippet: str


class ResearchGateway(ABC):
    """What ``atlas-egress`` will offer ``atlas-research`` in Phase 5."""

    @abstractmethod
    async def fetch(self, request: FetchRequest) -> FetchResult: ...

    @abstractmethod
    async def search(self, request: SearchRequest) -> list[SearchHit]: ...
