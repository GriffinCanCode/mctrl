"""Fan-out from the frame loop to whoever is watching.

The loop must never wait for a consumer. A camera delivers thirty frames a
second whatever else is happening, and a subscriber that stops reading -- a
window being dragged, a script paused in a debugger, a socket whose peer has
wandered off -- would otherwise apply back pressure straight into the gesture
engine and make the cursor stutter.

So every subscriber gets its own bounded buffer and drops its own oldest frame
when it overflows, counting what it lost so the loss is reported rather than
silent. Publishing is a bounded amount of work per subscriber with no blocking
call in it, and no subscriber can slow another one down.

The counterpart to that is the ``wants`` gate. With nobody watching, publishing
costs one set membership test, so the API is free when it is not in use -- which
is most of the time, and is why building snapshots is the caller's job rather
than the hub's.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from .contract import GESTURES, HANDS, STREAMS

# Two seconds of camera frames. Long enough to ride out a consumer that stalls
# briefly, short enough that a consumer which fell behind gets current data when
# it comes back rather than a minute of history it no longer cares about.
DEFAULT_DEPTH = 64


@dataclass(frozen=True)
class Delivery:
    """One stream frame, and how many were lost immediately before it.

    Summing ``dropped`` over what a consumer receives gives everything that went
    missing on the way to it, whether it was dropped here or upstream.
    """

    stream: str
    payload: Any
    dropped: int = 0


class Subscriber:
    """One watcher's buffer. Written by the frame loop, read by its owner."""

    def __init__(
        self,
        hub: EventHub,
        streams: Iterable[str],
        *,
        depth: int = DEFAULT_DEPTH,
        landmarks: bool = False,
        interval_ms: float = 0.0,
    ) -> None:
        self._hub = hub
        self._lock = threading.Lock()
        self._ready = threading.Condition(self._lock)
        self._queue: deque[Delivery] = deque()
        self._depth = max(int(depth), 1)
        self._dropped = 0
        self._next_at: dict[str, float] = {}
        self.streams: frozenset[str] = frozenset(streams)
        self.landmarks = landmarks
        self.interval_ms = max(interval_ms, 0.0)
        self.closed = False

    # ---------------------------------------------------------------- writing

    def offer(self, stream: str, payload: Any, now: float) -> None:
        """Take a frame if this subscriber wants one. Called on the frame loop."""
        if stream not in self.streams:
            return
        # Sampled streams may be thinned; discrete ones may not. Throttling
        # `gestures` would throw away a click, and a click that did not arrive is
        # not a slower feed, it is a lost intent.
        if self.interval_ms and stream != GESTURES:
            due = self._next_at.get(stream, 0.0)
            if now < due:
                return
            self._next_at[stream] = now + self.interval_ms / 1000.0
        with self._lock:
            if self.closed:
                return
            if len(self._queue) >= self._depth:
                # Whatever the evicted frame was itself reporting comes forward
                # with it. Otherwise a long stall loses its own tally: each
                # eviction would claim one frame lost, and the count of the nine
                # before it would be discarded along with the frame carrying it.
                lost = self._queue.popleft()
                self._dropped += 1 + lost.dropped
            self._queue.append(Delivery(stream, payload, self._dropped))
            self._dropped = 0
            self._ready.notify()

    # ---------------------------------------------------------------- reading

    def take(self, timeout: float | None = None) -> list[Delivery]:
        """Everything buffered, waiting up to ``timeout`` for the first frame."""
        with self._lock:
            if not self._queue and not self.closed:
                self._ready.wait(timeout)
            drained = list(self._queue)
            self._queue.clear()
            return drained

    def frames(self, timeout: float | None = None) -> Iterator[Delivery]:
        """Deliveries as they arrive, ending when the subscriber is closed.

        Anything already buffered is still handed over on the way out, so closing
        from another thread ends the loop without swallowing frames the consumer
        had been sent but not yet read.

        With a timeout the iterator ends on a quiet period too, which is what
        makes it usable for "watch for a while" rather than only "watch forever".
        """
        while True:
            drained = self.take(timeout)
            if drained:
                yield from drained
                continue
            if self.closed or timeout is not None:
                return

    def set_streams(self, streams: Iterable[str]) -> None:
        self.streams = frozenset(streams)
        self._hub.refresh()

    def close(self) -> None:
        self.detach()
        self._hub.remove(self)

    def detach(self) -> None:
        """Stop accepting frames and wake any reader. Leaves the hub alone.

        What is already buffered stays readable: a consumer closing from one
        thread while another drains should not lose frames it was already sent.
        """
        with self._lock:
            self.closed = True
            self._ready.notify_all()


class EventHub:
    """Holds the subscribers and hands each of them every frame it asked for."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: tuple[Subscriber, ...] = ()
        # Read on every frame and written only when somebody subscribes, so both
        # are plain attribute assignments rather than a lock the loop must take.
        self.wanted: frozenset[str] = frozenset()
        self.wants_landmarks = False

    def subscribe(
        self,
        streams: Iterable[str] = STREAMS,
        *,
        depth: int = DEFAULT_DEPTH,
        landmarks: bool = False,
        interval_ms: float = 0.0,
    ) -> Subscriber:
        subscriber = Subscriber(
            self, streams, depth=depth, landmarks=landmarks, interval_ms=interval_ms
        )
        with self._lock:
            self._subscribers += (subscriber,)
        self.refresh()
        return subscriber

    def remove(self, subscriber: Subscriber) -> None:
        with self._lock:
            self._subscribers = tuple(s for s in self._subscribers if s is not subscriber)
        self.refresh()

    def refresh(self) -> None:
        """Recompute the gate after any change to who is watching what."""
        with self._lock:
            current = self._subscribers
        wanted: set[str] = set()
        landmarks = False
        for subscriber in current:
            if subscriber.closed:
                continue
            wanted |= subscriber.streams
            landmarks = landmarks or (subscriber.landmarks and HANDS in subscriber.streams)
        self.wanted = frozenset(wanted)
        self.wants_landmarks = landmarks

    def wants(self, stream: str) -> bool:
        return stream in self.wanted

    @property
    def watching(self) -> int:
        return len(self._subscribers)

    def publish(self, stream: str, payload: Any) -> None:
        """Offer one frame to every subscriber. Never blocks, never raises."""
        if stream not in self.wanted:
            return
        now = time.monotonic()
        for subscriber in self._subscribers:
            subscriber.offer(stream, payload, now)

    def close(self) -> None:
        with self._lock:
            current, self._subscribers = self._subscribers, ()
        self.wanted = frozenset()
        self.wants_landmarks = False
        for subscriber in current:
            subscriber.detach()
