"""The near end of the socket.

Small on purpose: it is both what :meth:`MindControl.connect` runs on and the
reference for writing one of these in another language. Everything it does is a
line of JSON in and lines of JSON out.

The one part worth copying carefully is the reader. Replies and pushed stream
frames share the connection, so reading cannot be done from the calling thread --
a consumer blocked waiting for the answer to ``status.get`` would otherwise be
the thing holding up the frames it subscribed to. So one thread reads every line
and sorts it: replies go to whoever is waiting on that ``id``, stream frames go
into a bounded buffer, and both are woken rather than polled.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Any

from . import contract as api
from .contract import ApiError
from .hub import DEFAULT_DEPTH, Delivery
from .server import socket_path


class Client:
    """A connection to a running MindControl."""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        timeout: float = 5.0,
        depth: int = DEFAULT_DEPTH,
    ) -> None:
        self.path = Path(path) if path is not None else socket_path()
        self.timeout = timeout
        self.greeting: dict[str, Any] = {}
        self._socket: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()
        self._pending: dict[int, _Pending] = {}
        self._next_id = 0
        self._events: deque[Delivery] = deque()
        self._depth = max(int(depth), 1)
        self._dropped = 0
        self._arrived = threading.Condition(self._lock)
        self._closed = False
        self.error: str | None = None

    # ------------------------------------------------------------------ lifecycle

    def open(self) -> Client:
        handle = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        handle.settimeout(self.timeout)
        try:
            handle.connect(str(self.path))
        except OSError as problem:
            handle.close()
            raise ApiError(
                api.UNAVAILABLE,
                f"nothing is listening on {self.path}: {problem}. Is MindControl running, "
                "and is [api] enabled in config.toml?",
            ) from problem
        # Cleared once connected: a read must be free to block for as long as the
        # consumer is willing to wait for a frame, which is not the same budget as
        # opening the socket.
        handle.settimeout(None)
        self._socket = handle
        self._closed = False
        self._reader = threading.Thread(target=self._read, name="api-client-read", daemon=True)
        self._reader.start()
        return self

    def close(self) -> None:
        self._closed = True
        handle, self._socket = self._socket, None
        if handle is not None:
            with contextlib.suppress(OSError):
                handle.shutdown(socket.SHUT_RDWR)
            handle.close()
        with self._lock:
            for pending in self._pending.values():
                pending.fail(ApiError(api.UNAVAILABLE, "the connection was closed"))
            self._pending.clear()
            self._arrived.notify_all()

    @property
    def connected(self) -> bool:
        return self._socket is not None and not self._closed

    def __enter__(self) -> Client:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        self.close()

    # --------------------------------------------------------------------- calls

    def call(
        self, verb: str, params: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> Any:
        """Send one request and wait for its reply.

        The verb is checked here as well as at the far end, so a typo fails
        immediately and locally rather than after a round trip. How long to wait
        comes from the verb's own published budget unless overridden: reading the
        status and reopening the cameras do not deserve the same patience.
        """
        declared = api.lookup(verb)
        checked = api.coerce(declared, params)
        deadline = timeout if timeout is not None else max(self.timeout, declared.budget)
        handle = self._socket
        if handle is None or self._closed:
            raise ApiError(api.UNAVAILABLE, "not connected")

        with self._lock:
            self._next_id += 1
            identifier = self._next_id
            pending = _Pending()
            self._pending[identifier] = pending

        payload: dict[str, Any] = {"verb": verb, "id": identifier}
        # Send what the contract resolved rather than what was passed, so defaults
        # are explicit on the wire and the two ends cannot disagree about them. A
        # resolved None means "not given", which is not the same as null.
        given = {key: value for key, value in checked.items() if value is not None}
        if given:
            payload["params"] = given
        line = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        try:
            handle.sendall(line)
        except OSError as problem:
            with self._lock:
                self._pending.pop(identifier, None)
            raise ApiError(api.UNAVAILABLE, f"could not send {verb}: {problem}") from problem

        return pending.wait(deadline, verb)

    # ------------------------------------------------------------------- streams

    def take(self, timeout: float | None = None) -> list[Delivery]:
        with self._lock:
            if not self._events and not self._closed:
                self._arrived.wait(timeout)
            drained = list(self._events)
            self._events.clear()
            return drained

    def events(self, timeout: float | None = None) -> Iterator[Delivery]:
        while not self._closed:
            drained = self.take(timeout)
            if not drained:
                if timeout is not None:
                    return
                continue
            yield from drained

    # -------------------------------------------------------------------- reading

    def _read(self) -> None:
        handle = self._socket
        if handle is None:  # pragma: no cover - open() always sets it first
            return
        try:
            with handle.makefile("rb") as stream:
                for line in stream:
                    if self._closed:
                        return
                    stripped = line.strip()
                    if stripped:
                        self._sort(stripped)
        except OSError:
            return
        finally:
            # The far end hung up. Fail anything still waiting rather than letting
            # it sit out its full timeout for an answer that is not coming.
            self.close()

    def _sort(self, line: bytes) -> None:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            self.error = "the server sent something that is not JSON"
            return
        if not isinstance(message, dict):
            return
        if "hello" in message:
            self.greeting = message["hello"]
            protocol = self.greeting.get("protocol")
            if protocol != api.PROTOCOL_VERSION:
                self.error = (
                    f"protocol {protocol} on the other side, {api.PROTOCOL_VERSION} here; "
                    "one of the two is out of date"
                )
            return
        if "stream" in message:
            self._buffer(message)
            return
        identifier = message.get("id")
        with self._lock:
            pending = self._pending.pop(identifier, None) if identifier is not None else None
        if pending is not None:
            pending.deliver(message)

    def _buffer(self, message: dict[str, Any]) -> None:
        stream = str(message.get("stream", ""))
        try:
            payload = api.decode_stream(stream, message.get("data") or {})
        except (ApiError, KeyError, TypeError, ValueError):
            # A frame this build does not understand, from a newer app. Skip it;
            # the protocol check in the greeting has already said why.
            return
        # Server-side drops are added to client-side ones, so summing what a
        # consumer receives accounts for everything that went missing anywhere on
        # the way to it -- including the tally of a frame evicted here.
        dropped = int(message.get("dropped", 0))
        with self._lock:
            if len(self._events) >= self._depth:
                lost = self._events.popleft()
                self._dropped += 1 + lost.dropped
            self._events.append(Delivery(stream, payload, dropped + self._dropped))
            self._dropped = 0
            self._arrived.notify()


class _Pending:
    """One outstanding request, waiting for its line to come back."""

    __slots__ = ("_done", "_error", "_reply")

    def __init__(self) -> None:
        self._done = threading.Event()
        self._reply: dict[str, Any] | None = None
        self._error: ApiError | None = None

    def deliver(self, reply: dict[str, Any]) -> None:
        self._reply = reply
        self._done.set()

    def fail(self, error: ApiError) -> None:
        self._error = error
        self._done.set()

    def wait(self, timeout: float, verb: str) -> Any:
        if not self._done.wait(timeout):
            raise ApiError(api.UNAVAILABLE, f"{verb} was not answered within {timeout:g}s")
        if self._error is not None:
            raise self._error
        reply = self._reply or {}
        if not reply.get("ok"):
            problem = reply.get("error") or {}
            raise ApiError(
                str(problem.get("code", api.INTERNAL)),
                str(problem.get("message", "the call failed without saying why")),
            )
        return reply.get("result")
