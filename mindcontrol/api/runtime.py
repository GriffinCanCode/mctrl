"""Where the verbs meet the running program.

One implementation of the contract against a live :class:`Pipeline`, and the only
place that knows how to do so safely. Both transports sit on top of this, which
is what stops the socket and the Python facade from becoming two APIs.

The safety is mostly about threads. The cursor, the gesture engine and the socket
to the native helper have exactly one writer -- the frame loop -- and that is
what makes a gaze warp and a hand delta unable to race against each other. A verb
arriving on a socket thread therefore does not touch them; it is queued with
:meth:`Pipeline.submit` and runs between frames. Reads go the other way: the live
status object is copied into a frozen snapshot before it leaves, so a consumer
cannot be handed a half-rewritten frame.

Calibration is the exception on purpose. It wants the camera, a fullscreen window
and a person, in that order, for the best part of a minute, so it runs on its own
thread and asks the loop to stand down while it does.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import Config, load
from ..control.modes import Mode
from . import contract as api
from .contract import (
    GAZE,
    GESTURES,
    HANDS,
    STATUS,
    ApiError,
    GazeSnapshot,
    GestureEventMsg,
    HandsFrame,
    HandSnapshot,
    StatusSnapshot,
)
from .hub import DEFAULT_DEPTH, EventHub, Subscriber

if TYPE_CHECKING:
    from ..capture import Frame
    from ..fusion import FusedHand
    from ..gestures.engine import GestureEvent
    from ..pipeline import Pipeline, PipelineStatus

# How long a verb that has to happen on the frame loop will wait for it. A frame
# takes tens of milliseconds, so anything near this means the loop is wedged and
# the caller deserves to be told rather than left hanging.
LOOP_TIMEOUT = 10.0
# Reopening cameras rebuilds the MediaPipe graphs, which is seconds, not
# milliseconds. Measured at about three on a laptop with two cameras.
SETTLE_TIMEOUT = 30.0


class Runtime:
    """The verbs, bound to one pipeline."""

    def __init__(
        self,
        pipeline: Pipeline,
        *,
        config_path: Path | None = None,
        on_config: Callable[[Config], None] | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.hub = EventHub()
        self._config_path = config_path
        # Whoever else holds the config -- the menu bar keeps its own reference --
        # gets told when a reload replaces it, so one client's reload does not
        # leave the rest of the process reading a superseded file.
        self._on_config = on_config
        self._previous_frame_hook: Callable[..., None] | None = None
        self._previous_gesture_hook: Callable[..., None] | None = None
        self._attached = False
        self._lock = threading.Lock()
        self._calibrating = False
        self._complained: set[str] = set()

    # ---------------------------------------------------------------- lifecycle

    def attach(self) -> None:
        """Start publishing. Chains whatever hooks are already installed.

        Chained rather than replaced because the debug overlay is a frame hook
        too, and an API client attaching must not blank the window someone is
        using to tune thresholds.
        """
        if self._attached:
            return
        self._previous_frame_hook = self.pipeline.frame_hook
        self._previous_gesture_hook = self.pipeline.gesture_hook
        self.pipeline.frame_hook = self._on_frame
        self.pipeline.gesture_hook = self._on_gestures
        self._attached = True

    def detach(self) -> None:
        if not self._attached:
            return
        self.pipeline.frame_hook = self._previous_frame_hook
        self.pipeline.gesture_hook = self._previous_gesture_hook
        self._attached = False

    def close(self) -> None:
        self.detach()
        self.hub.close()

    def session(self) -> Session:
        """A per-consumer view, holding that consumer's subscriptions."""
        return Session(self)

    # ------------------------------------------------------------- publishing

    def _on_frame(self, frame: Frame, hands: list[FusedHand], status: PipelineStatus) -> None:
        """On the frame loop, once per processed frame."""
        if self.hub.wanted:
            try:
                self._publish_frame(frame, hands, status)
            except Exception as problem:
                # A consumer's snapshot must never be able to stop the loop.
                self._complain("frame", problem)
        if self._previous_frame_hook is not None:
            self._previous_frame_hook(frame, hands, status)

    def _publish_frame(self, frame: Frame, hands: list[FusedHand], status: PipelineStatus) -> None:
        hub = self.hub
        if hub.wants(STATUS):
            hub.publish(STATUS, StatusSnapshot.of(status))
        if hub.wants(HANDS):
            landmarks = hub.wants_landmarks
            hub.publish(
                HANDS,
                HandsFrame(
                    t=time.monotonic(),
                    camera_id=frame.camera_id,
                    sequence=frame.sequence,
                    timestamp_ms=frame.timestamp_ms,
                    hands=tuple(HandSnapshot.of(hand, landmarks=landmarks) for hand in hands),
                ),
            )
        if hub.wants(GAZE):
            hub.publish(
                GAZE,
                GazeSnapshot(ready=status.gaze_ready, point=status.gaze_point, warps=status.warps),
            )

    def _on_gestures(self, events: list[GestureEvent]) -> None:
        if self.hub.wants(GESTURES):
            try:
                for event in events:
                    self.hub.publish(GESTURES, GestureEventMsg.of(event))
            except Exception as problem:
                self._complain("gestures", problem)
        if self._previous_gesture_hook is not None:
            self._previous_gesture_hook(events)

    def _complain(self, where: str, problem: Exception) -> None:
        """Report a publishing failure once, then stay quiet about it."""
        if where in self._complained:
            return
        self._complained.add(where)
        print(f"[api] could not publish {where}: {problem!r}")

    # ------------------------------------------------------------------ status

    def status(self) -> StatusSnapshot:
        return StatusSnapshot.of(self.pipeline.status)

    # ------------------------------------------------------------------- modes

    def modes(self) -> dict[str, Any]:
        manager = self.pipeline.modes
        mode = manager.mode
        return {"mode": mode.value, "engaged": mode is Mode.ACTIVE, "detail": manager.describe()}

    def set_mode(self, mode: str) -> dict[str, Any]:
        try:
            wanted = Mode(mode)
        except ValueError as problem:  # pragma: no cover - the contract checks choices first
            raise ApiError(api.BAD_PARAMS, f"no mode {mode!r}") from problem
        self._on_loop(lambda: self.pipeline.apply_mode(wanted), LOOP_TIMEOUT)
        return self.modes()

    def toggle_mode(self) -> dict[str, Any]:
        self._on_loop(lambda: self.pipeline.apply_mode(), LOOP_TIMEOUT)
        return self.modes()

    # ------------------------------------------------------------------- input

    def move_by(self, dx: float, dy: float) -> dict[str, Any]:
        return self._defer(lambda: self.pipeline.mouse.move_by(dx, dy))

    def move_to(self, x: float, y: float) -> dict[str, Any]:
        return self._defer(lambda: self.pipeline.mouse.move_to_fraction(x, y))

    def click(self, button: str = "left") -> dict[str, Any]:
        return self._defer(lambda: self.pipeline.mouse.click(button))

    def press(self, button: str = "left") -> dict[str, Any]:
        return self._defer(lambda: self.pipeline.mouse.press(button))

    def release(self, button: str | None = None) -> dict[str, Any]:
        return self._defer(lambda: self.pipeline.mouse.release(button))

    def scroll(self, dx: float, dy: float) -> dict[str, Any]:
        return self._defer(lambda: self.pipeline.mouse.scroll(dx, dy))

    # ------------------------------------------------------------------ system

    def reload_config(self) -> dict[str, Any]:
        # Read the file here rather than on the loop: parsing TOML is the slowest
        # part and there is no reason to spend a frame on it.
        cfg = load(self._config_path)
        self._on_loop(lambda: self.pipeline.apply_config(cfg), SETTLE_TIMEOUT)
        if self._on_config is not None:
            self._on_config(cfg)
        source = cfg.source_path
        return {"source": None if source is None else str(source)}

    def pause(self) -> dict[str, Any]:
        self._on_loop(self.pipeline.pause, LOOP_TIMEOUT)
        return {"paused": True}

    def resume(self) -> dict[str, Any]:
        self._on_loop(self.pipeline.resume, SETTLE_TIMEOUT)
        return {"paused": False}

    def calibrate(self) -> dict[str, Any]:
        """Start the nine-point calibration on a thread of its own.

        A subprocess, because calibration needs a fullscreen window and therefore
        a main thread, which this process has already given to the menu bar. Two
        at once would fight over the camera, so the second is refused.
        """
        with self._lock:
            if self._calibrating:
                raise ApiError(api.BUSY, "a calibration is already running")
            self._calibrating = True
        threading.Thread(target=self._calibrate, name="api-calibrate", daemon=True).start()
        return {"started": True}

    @property
    def calibrating(self) -> bool:
        """True while one is running, whoever asked for it."""
        return self._calibrating

    def _calibrate(self) -> None:
        previous = self.pipeline.modes.mode
        try:
            self._quietly(lambda: self.pipeline.apply_mode(Mode.OFF))
            self._quietly(self.pipeline.pause)
            result = subprocess.run(
                [sys.executable, "-m", "mindcontrol.calibrate"],
                cwd=Path.cwd(),
                check=False,
            )
            if result.returncode != 0:
                print(f"[api] calibration exited with {result.returncode}")
        except OSError as problem:
            print(f"[api] could not start calibration: {problem}")
        finally:
            self._quietly(self.pipeline.resume)
            self._quietly(lambda: self.pipeline.apply_mode(previous))
            with self._lock:
                self._calibrating = False

    def describe(self) -> dict[str, Any]:
        return api.catalogue()

    # --------------------------------------------------------------- marshalling

    def _on_loop(self, work: Callable[[], Any], timeout: float) -> Any:
        """Run work on the frame loop and wait for it, as an API failure if it cannot."""
        try:
            return self.pipeline.submit(work, timeout=timeout)
        except RuntimeError as problem:
            raise ApiError(api.UNAVAILABLE, str(problem)) from problem
        except TimeoutError as problem:
            raise ApiError(api.UNAVAILABLE, str(problem)) from problem

    def _defer(self, work: Callable[[], Any]) -> dict[str, Any]:
        """Queue work for the frame loop without waiting for it."""
        try:
            self.pipeline.submit(work)
        except RuntimeError as problem:
            raise ApiError(api.UNAVAILABLE, str(problem)) from problem
        return {"queued": True}

    def _quietly(self, work: Callable[[], Any]) -> None:
        """Best effort, for the calibration teardown that must always finish."""
        try:
            self.pipeline.submit(work, timeout=SETTLE_TIMEOUT)
        except (RuntimeError, TimeoutError) as problem:
            print(f"[api] {problem}")


class Session:
    """One consumer's connection: its subscriptions, and its way in.

    Streams are per consumer, so subscribing has to be answered by something that
    knows who asked. Everything else is stateless and goes straight through to
    the runtime.
    """

    def __init__(self, runtime: Runtime, *, depth: int = DEFAULT_DEPTH) -> None:
        self.runtime = runtime
        self._depth = depth
        self.subscriber: Subscriber | None = None
        self._handlers = self._bind()

    # ------------------------------------------------------------------ streams

    def subscribe(
        self,
        streams: Iterable[str] | None = None,
        *,
        landmarks: bool = False,
        interval_ms: float = 0.0,
    ) -> dict[str, Any]:
        wanted = api.resolve_streams(tuple(streams) if streams else None)
        current = self.subscriber
        if current is None or current.closed:
            self.subscriber = self.runtime.hub.subscribe(
                wanted, depth=self._depth, landmarks=landmarks, interval_ms=interval_ms
            )
        else:
            # Additive, so subscribing to one more stream does not silently drop
            # the ones already flowing.
            current.landmarks = landmarks or current.landmarks
            current.interval_ms = max(interval_ms, 0.0)
            current.set_streams(current.streams | set(wanted))
        return self.subscriptions()

    def unsubscribe(self, streams: Iterable[str] | None = None) -> dict[str, Any]:
        current = self.subscriber
        if current is None:
            return self.subscriptions()
        if not streams:
            current.close()
            self.subscriber = None
            return self.subscriptions()
        remaining = current.streams - set(api.resolve_streams(tuple(streams)))
        if not remaining:
            current.close()
            self.subscriber = None
        else:
            current.set_streams(remaining)
        return self.subscriptions()

    def subscriptions(self) -> dict[str, Any]:
        current = self.subscriber
        if current is None or current.closed:
            return {"streams": [], "landmarks": False, "interval_ms": 0.0}
        return {
            "streams": [name for name in api.STREAMS if name in current.streams],
            "landmarks": current.landmarks,
            "interval_ms": current.interval_ms,
        }

    def close(self) -> None:
        if self.subscriber is not None:
            self.subscriber.close()
            self.subscriber = None

    # ------------------------------------------------------------------ dispatch

    def call(self, verb_id: str, params: dict[str, Any] | None = None) -> Any:
        """Run one verb. The single door every transport comes through."""
        verb = api.lookup(verb_id)
        arguments = api.coerce(verb, params)
        handler = self._handlers.get(verb.id)
        if handler is None:  # pragma: no cover - the tables are checked by a test
            raise ApiError(api.INTERNAL, f"{verb.id} is declared but not implemented")
        return handler(**arguments)

    def _bind(self) -> dict[str, Callable[..., Any]]:
        runtime = self.runtime
        return {
            "status.get": runtime.status,
            "modes.get": runtime.modes,
            "modes.set": runtime.set_mode,
            "modes.toggle": runtime.toggle_mode,
            "tracking.subscribe": self.subscribe,
            "tracking.unsubscribe": self.unsubscribe,
            "input.move_by": runtime.move_by,
            "input.move_to": runtime.move_to,
            "input.click": runtime.click,
            "input.press": runtime.press,
            "input.release": runtime.release,
            "input.scroll": runtime.scroll,
            "system.calibrate": runtime.calibrate,
            "system.reload_config": runtime.reload_config,
            "system.pause": runtime.pause,
            "system.resume": runtime.resume,
            "system.describe": runtime.describe,
        }
