"""The API over a Unix socket, one JSON object per line.

Newline-delimited JSON on ``AF_UNIX``, because the consumers are other programs
on the same machine and every language can already do both halves of that. No
dependency to install, no port to collide, no HTTP framing to get wrong, and
nothing listening on the network: the socket lives in the user's state directory
with 0600 on it, which is the same trust boundary the native helper's socket
already uses.

Three kinds of line travel back:

===============  ============================================================
``hello``        sent once on connect, carrying the protocol version
``ok``           the answer to a request, echoing its ``id``
``stream``       a pushed frame, sent only after ``tracking.subscribe``
===============  ============================================================

Each connection gets a reader thread and, once it subscribes, a pump thread.
Two rather than one because a connection has to be able to wait on the socket
and on the frame loop at the same time, and a shared send lock is a great deal
easier to reason about than a selector loop holding half-written frames.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import STATE_DIR
from . import contract as api
from .contract import HANDS, ApiError

if TYPE_CHECKING:
    from ..config import ApiConfig
    from .runtime import Runtime, Session

SOCKET_PATH = STATE_DIR / "api.sock"

# A request is a verb and a handful of numbers. Anything past this is either a
# consumer with a bug or somebody probing, and both are better off refused than
# buffered.
MAX_LINE = 64 * 1024
# How long a pump waits for a frame before checking whether it should still be
# running, which is what makes a closed connection notice in bounded time.
PUMP_TICK = 0.5


def socket_path(cfg: ApiConfig | None = None) -> Path:
    """Where the socket lives: the config's path, or the default one."""
    if cfg is not None and cfg.socket:
        return Path(cfg.socket).expanduser()
    return SOCKET_PATH


def already_serving(path: Path) -> bool:
    """True when something is answering on that socket right now.

    Asked before binding, because binding means unlinking whatever is there, and
    unlinking a live socket would take the API away from the app that owns it.
    """
    if not path.exists():
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(str(path))
    except OSError:
        return False
    finally:
        probe.close()
    return True


class ApiServer:
    """Accepts connections and gives each one a session on the runtime."""

    def __init__(self, runtime: Runtime, cfg: ApiConfig | None = None) -> None:
        self._runtime = runtime
        self._cfg = cfg
        self.path = socket_path(cfg)
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._connections: set[Connection] = set()
        self.error: str | None = None

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> bool:
        """Bind and start accepting. False, with :attr:`error` set, if it cannot.

        A failure here is a downgrade rather than a fault: the app keeps tracking
        and controlling the cursor, it just has nobody to tell about it.
        """
        if self._cfg is not None and not self._cfg.enabled:
            self.error = "api disabled in config"
            return False
        if already_serving(self.path):
            self.error = f"something is already serving {self.path}"
            return False

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()

        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # Narrow the mode before the socket exists rather than after. A chmod
        # afterwards leaves a window in which the socket is connectable by
        # anyone, and this one carries the ability to move the cursor.
        previous = os.umask(0o077)
        try:
            listener.bind(str(self.path))
        except OSError as problem:
            listener.close()
            self.error = f"could not open {self.path}: {problem}"
            return False
        finally:
            os.umask(previous)

        listener.listen(8)
        listener.settimeout(PUMP_TICK)
        self._listener = listener
        self._stop.clear()
        self._thread = threading.Thread(target=self._accept, name="api-accept", daemon=True)
        self._thread.start()
        self.error = None
        print(f"[api] listening on {self.path}", flush=True)
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        with self._lock:
            current, self._connections = set(self._connections), set()
        for connection in current:
            connection.close()
        with contextlib.suppress(OSError):
            self.path.unlink()

    @property
    def clients(self) -> int:
        with self._lock:
            return len(self._connections)

    # -------------------------------------------------------------------- accept

    def _accept(self) -> None:
        listener = self._listener
        while not self._stop.is_set() and listener is not None:
            try:
                handle, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                # The listener was closed under us, which is how stop() works.
                return
            connection = Connection(handle, self._runtime.session(), self._forget)
            with self._lock:
                self._connections.add(connection)
            connection.start()

    def _forget(self, connection: Connection) -> None:
        with self._lock:
            self._connections.discard(connection)


class Connection:
    """One client: its session, its socket, and the threads serving both."""

    def __init__(
        self,
        handle: socket.socket,
        session: Session,
        done: Callable[[Connection], None] | None = None,
    ) -> None:
        self._handle = handle
        self._session = session
        self._done = done
        self._send_lock = threading.Lock()
        self._closed = threading.Event()
        self._reader: threading.Thread | None = None
        self._pump: threading.Thread | None = None

    def start(self) -> None:
        self._reader = threading.Thread(target=self._read, name="api-client", daemon=True)
        self._reader.start()

    # --------------------------------------------------------------------- write

    def _write(self, payload: dict[str, Any]) -> bool:
        if self._closed.is_set():
            return False
        line = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        with self._send_lock:
            try:
                self._handle.sendall(line)
            except OSError:
                # The peer went away. Nothing to report to and nowhere to report
                # it, so drop the connection quietly.
                self._closed.set()
                return False
        return True

    # ---------------------------------------------------------------------- read

    def _read(self) -> None:
        self._write({"hello": {"protocol": api.PROTOCOL_VERSION, "app": "mindcontrol"}})
        try:
            with self._handle.makefile("rb") as stream:
                while not self._closed.is_set():
                    line = stream.readline(MAX_LINE)
                    if not line:
                        return
                    if not line.endswith(b"\n"):
                        self._write(
                            api.failure(
                                None, ApiError(api.BAD_REQUEST, f"a line may not exceed {MAX_LINE}")
                            )
                        )
                        return
                    stripped = line.strip()
                    if stripped:
                        self._handle_line(stripped)
        except OSError:
            return
        finally:
            self.close()

    def _handle_line(self, line: bytes) -> None:
        identifier: int | None = None
        try:
            request = api.Request.from_json(json.loads(line))
            identifier = request.id
            result = self._session.call(request.verb, request.params)
        except json.JSONDecodeError as problem:
            self._write(api.failure(None, ApiError(api.BAD_REQUEST, f"bad JSON: {problem.msg}")))
            return
        except ApiError as problem:
            self._write(api.failure(identifier, problem))
            return
        except Exception as problem:
            # One client's bad call must not take the app down with it.
            self._write(api.failure(identifier, ApiError(api.INTERNAL, repr(problem))))
            return
        self._write(api.reply(identifier, _jsonable(result)))
        # Subscribing is what makes a connection worth pumping, and it may happen
        # at any point in its life, so the pump is started on demand.
        if self._session.subscriber is not None:
            self._ensure_pump()

    # ---------------------------------------------------------------------- pump

    def _ensure_pump(self) -> None:
        if self._pump is not None and self._pump.is_alive():
            return
        self._pump = threading.Thread(target=self._push, name="api-stream", daemon=True)
        self._pump.start()

    def _push(self) -> None:
        while not self._closed.is_set():
            subscriber = self._session.subscriber
            if subscriber is None or subscriber.closed:
                return
            for delivery in subscriber.take(PUMP_TICK):
                payload = delivery.payload
                if delivery.stream == HANDS:
                    data = payload.to_json(landmarks=subscriber.landmarks)
                else:
                    data = payload.to_json()
                if not self._write(api.event(delivery.stream, data, dropped=delivery.dropped)):
                    return

    # --------------------------------------------------------------------- close

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._session.close()
        with contextlib.suppress(OSError):
            self._handle.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self._handle.close()
        if self._done is not None:
            self._done(self)


def _jsonable(result: Any) -> Any:
    """Snapshots know how to encode themselves; everything else already is."""
    encode = getattr(result, "to_json", None)
    return encode() if callable(encode) else result
