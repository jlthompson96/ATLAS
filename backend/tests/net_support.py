"""A tiny scriptable HTTP/1.1 server on 127.0.0.1 for the LM Studio and hostd client tests.

``Handler`` gets a ``Request`` and returns a ``Reply``. A reply can be a plain body, a chunked
stream (``chunks``; ``truncate`` drops the connection before the final chunk), or a stall (the
server just never answers, to test deadlines). Nothing here is a real LM Studio: tests assert on
what the *client* sent (headers, JSON body) and on how it maps what comes back.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

TOKEN = "lms-test-token-0000"  # noqa: S105 - fake token


@dataclass
class Request:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


@dataclass
class Reply:
    status: int = 200
    body: bytes = b""
    chunks: Sequence[bytes] | None = None
    truncate: bool = False
    stall: bool = False
    hang_after_chunks: bool = False  # send ``chunks``, then neither more data nor the end of the body
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def of_json(cls, obj: Any, status: int = 200) -> Reply:
        return cls(status, json.dumps(obj).encode(), headers={"Content-Type": "application/json"})


Handler = Callable[[Request], Reply]


class FakeServer:
    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[Request] = []
        self.port = 0
        self._server: asyncio.Server | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> FakeServer:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def close(self) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            request = Request(method, path, headers, body)
            self.requests.append(request)
            reply = self.handler(request)
            if reply.stall:
                await reader.read()  # until the client gives up and hangs up
                return
            out = [f"HTTP/1.1 {reply.status} X".encode(), b"Connection: close"]
            out += [f"{k}: {v}".encode() for k, v in reply.headers.items()]
            if reply.chunks is None:
                out.append(f"Content-Length: {len(reply.body)}".encode())
                writer.write(b"\r\n".join(out) + b"\r\n\r\n" + reply.body)
            else:
                out.append(b"Transfer-Encoding: chunked")
                writer.write(b"\r\n".join(out) + b"\r\n\r\n")
                for chunk in reply.chunks:
                    writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    await writer.drain()
                    await asyncio.sleep(0)
                if reply.hang_after_chunks:
                    await reader.read()  # until the client gives up and hangs up
                    return
                if not reply.truncate:
                    writer.write(b"0\r\n\r\n")
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()


@contextlib.contextmanager
def threaded(handler: Handler) -> Iterator[FakeServer]:
    """The same server on its own event loop in a thread, for tests that call a blocking ``main()``."""
    loop = asyncio.new_event_loop()
    server = FakeServer(handler)
    started = threading.Event()

    def target() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.start())
        started.set()
        loop.run_forever()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    started.wait(5)
    try:
        yield server
    finally:
        asyncio.run_coroutine_threadsafe(server.close(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()


def closed_port() -> int:
    """A port nothing listens on (bind, read the number, close)."""
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def sse(*events: Any) -> list[bytes]:
    """SSE chunks: each event on its own chunk, JSON objects framed ``data: ...`` and ``[DONE]`` as a string."""
    out = []
    for e in events:
        data = e if isinstance(e, str) else json.dumps(e)
        out.append(f"data: {data}\n\n".encode())
    return out
