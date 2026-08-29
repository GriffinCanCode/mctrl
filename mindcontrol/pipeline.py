"""The processing loop.

Runs on a worker thread so the menu bar keeps its own main thread, and does the
same five things every frame: read cameras, find hands and eyes, merge cameras,
ask the gesture engine what that means, and post the resulting events.

Gaze and hands are combined here rather than in either tracker, because the
useful rule is a relationship between them: gaze may only move the cursor while
the hand is holding still. Eyes are good at crossing a screen and bad at holding
a target; hands are the reverse. So gaze throws the cursor into the right region
and the hand does the last inch, and gaze stops interfering the moment the hand
starts working.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .camera.capture import CameraBank, Frame
from .config import GAZE_MODEL_PATH, Config
from .control.bridge import Bridge
from .control.keyboard import Keyboard
from .control.modes import Mode, ModeManager
from .control.mouse import Mouse
from .filters import OneEuroFilter2D
from .gestures.engine import Action, GestureEngine, GestureEvent
from .gestures.fusion import FusedHand, HandFusion, Observation, fuse_gaze
from .logs import muffled
from .tracking.gaze import FixationDetector, GazeModel, GazeObservation, GazeTracker
from .tracking.hands import HandTracker


@dataclass
class PipelineStatus:
    """Snapshot for the menu bar and the debug overlay."""

    fps: float = 0.0
    mode: str = "off"
    gesture: str = "idle"
    hands: int = 0
    cameras: tuple[int, ...] = ()
    merged: bool = False
    gaze_ready: bool = False
    gaze_point: tuple[float, float] | None = None
    warps: int = 0
    # Whether the native helper is driving. False means the fallback path is,
    # which works but is neither smoothed nor snapped.
    native: bool = False
    problems: list[str] = field(default_factory=list)


class _Command:
    """Work handed to the frame loop from another thread.

    Cursor state, the gesture engine and the socket to the native helper are all
    single-threaded by construction -- the loop is the only writer, and that is
    what makes a warp and a hand delta unable to race each other. So an outside
    caller does not touch them; it queues a closure and the loop runs it between
    frames, which keeps that guarantee while still letting anything drive.
    """

    __slots__ = ("done", "error", "result", "run")

    def __init__(self, run: Callable[[], object], *, wait: bool) -> None:
        self.run = run
        self.done = threading.Event() if wait else None
        self.result: object = None
        self.error: BaseException | None = None

    def __call__(self) -> None:
        try:
            self.result = self.run()
        except BaseException as problem:
            # Carried back to whoever queued it rather than raised here, where it
            # would end the frame loop over somebody else's mistake.
            self.error = problem
        finally:
            if self.done is not None:
                self.done.set()


class Pipeline:
    """Owns the cameras, the models, and the frame loop."""

    def __init__(
        self,
        cfg: Config,
        on_status: Callable[[PipelineStatus], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self._on_status = on_status

        self.modes = ModeManager(cfg.modes)
        self.bridge = Bridge(cfg.native, cfg.gestures.double_click_ms)
        self.mouse = Mouse(double_click_ms=cfg.gestures.double_click_ms, bridge=self.bridge)
        self.keyboard = Keyboard(cfg.keys)
        self.engine = GestureEngine(cfg.pointer, cfg.gestures, cfg.tracking)
        self.fusion = HandFusion(cfg.tracking, cfg.gestures)

        self._bank: CameraBank | None = None
        self._hand_trackers: dict[int, HandTracker] = {}
        self._gaze_tracker: GazeTracker | None = None
        self.gaze_model = GazeModel.load(GAZE_MODEL_PATH)
        self._gaze_filter = OneEuroFilter2D(cfg.gaze.filter_fc_min, cfg.gaze.filter_beta)
        self._fixation = FixationDetector(cfg.gaze.fixation_ms, cfg.gaze.fixation_radius)
        self._last_warp: tuple[float, float] | None = None

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._commands: queue.SimpleQueue[_Command] = queue.SimpleQueue()
        self._seen: dict[int, int] = {}
        self.status = PipelineStatus(mode=self.modes.describe())
        self.frame_hook: Callable[[Frame, list[FusedHand], PipelineStatus], None] | None = None
        # Everything the engine decided this frame, before it is acted on. The
        # overlay wants the frame; anything watching the program from outside
        # wants the intents, which used to be visible only in their effects.
        self.gesture_hook: Callable[[list[GestureEvent]], None] | None = None

    # ----------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self.modes.start()
        if self.modes.watcher_error:
            self.status.problems.append(self.modes.watcher_error)
        # A missing helper is a downgrade, not a failure: the fallback path in
        # `Mouse` still drives the cursor, just without smoothing or snapping.
        if not self.bridge.start() and self.bridge.error:
            self.status.problems.append(self.bridge.error)
            print(f"[bridge] {self.bridge.error}")
        self.status.native = self.bridge.connected
        self._open_cameras()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="pipeline", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._release_control()
        self._close_cameras()
        self.modes.stop()
        self.bridge.stop()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def submit(self, work: Callable[[], object], *, timeout: float | None = None) -> object:
        """Run ``work`` on the frame loop, from any thread.

        With a timeout the call waits for the result and re-raises whatever the
        work raised, which is what a caller who needs to know it landed wants.
        Without one it returns immediately: for cursor motion that is the right
        trade, since the cost of a late delta is worse than the cost of a lost one.
        """
        if not self.running:
            raise RuntimeError("the pipeline is not running")
        command = _Command(work, wait=timeout is not None)
        self._commands.put(command)
        if command.done is None:
            return None
        if not command.done.wait(timeout):
            raise TimeoutError("the frame loop did not get to it in time")
        if command.error is not None:
            raise command.error
        return command.result

    def _drain_commands(self) -> None:
        """Run queued work. First thing each pass, so it happens even when paused."""
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            command()

    def pause(self) -> None:
        """Release the cameras so another process can use them, e.g. calibration."""
        self._paused.set()
        self._release_control()
        self._close_cameras()

    def resume(self) -> None:
        """Reopen cameras and pick up a calibration written while paused."""
        self.gaze_model = GazeModel.load(GAZE_MODEL_PATH)
        self._open_cameras()
        self.engine.rebase()
        self.fusion.reset()
        self._paused.clear()

    def _open_cameras(self) -> None:
        self._bank = CameraBank(self.cfg.cameras)
        problems = self._bank.start()
        self.status.problems = list(problems)
        if not len(self._bank):
            self.status.problems.append("no cameras available")
            return
        mirrored = self.cfg.cameras.mirror
        # Model construction is where MediaPipe does its logging, and it happens
        # again on every resume after a calibration, so it is worth muffling.
        with muffled():
            self._hand_trackers = {
                camera_id: HandTracker(self.cfg.tracking, self.cfg.gestures, mirrored)
                for camera_id in self._bank.workers
            }
            if self.cfg.tracking.face_enabled:
                self._gaze_tracker = GazeTracker(self.cfg.tracking)
        self._seen.clear()

    def _close_cameras(self) -> None:
        for tracker in self._hand_trackers.values():
            tracker.close()
        self._hand_trackers.clear()
        if self._gaze_tracker is not None:
            self._gaze_tracker.close()
            self._gaze_tracker = None
        if self._bank is not None:
            self._bank.stop()
            self._bank = None

    # ---------------------------------------------------------------------- loop

    def _run(self) -> None:
        previous = time.monotonic()
        smoothed_fps = 0.0
        while not self._stop.is_set():
            self._drain_commands()
            if self._paused.is_set() or self._bank is None:
                time.sleep(0.05)
                continue

            frames = self._fresh_frames()
            if not frames:
                time.sleep(0.005)
                continue

            now = time.monotonic()
            dt = max(now - previous, 1e-4)
            previous = now
            smoothed_fps = 0.9 * smoothed_fps + 0.1 * (1.0 / dt) if smoothed_fps else 1.0 / dt

            self._process(frames, now, dt)
            self.status.fps = smoothed_fps
            self.status.mode = self.modes.describe()
            self.status.gesture = self.engine.status()
            if self._on_status is not None:
                self._on_status(self.status)

    def _fresh_frames(self) -> dict[int, Frame]:
        """Newest unprocessed frame per camera.

        A camera that has not produced a new image is skipped rather than
        reprocessed, which keeps a slow camera from throttling a fast one.
        """
        assert self._bank is not None
        fresh: dict[int, Frame] = {}
        for camera_id, frame in self._bank.latest().items():
            if self._seen.get(camera_id) == frame.sequence:
                continue
            self._seen[camera_id] = frame.sequence
            fresh[camera_id] = frame
        return fresh

    def _process(self, frames: dict[int, Frame], now: float, dt: float) -> None:
        assert self._bank is not None
        newest_ms = max(frame.timestamp_ms for frame in frames.values())

        observations = [
            Observation(
                camera_id=camera_id,
                hands=self._hand_trackers[camera_id].process(frame),
                age_ms=float(newest_ms - frame.timestamp_ms),
            )
            for camera_id, frame in frames.items()
            if camera_id in self._hand_trackers
        ]
        fused = self.fusion.fuse(observations)
        if any(hand.rebased for hand in fused):
            self.engine.rebase()

        gaze = self._read_gaze(frames)
        engaged = self.modes.engaged

        events = self.engine.update([hand.features for hand in fused], now, dt, engaged)
        # Before dispatching, not after: the helper decides whether to look for a
        # target from this, and a click arriving first would resolve against a
        # stale mode.
        self._signal_mode(engaged)
        if engaged:
            self._apply_gaze(gaze, dt)
        self._dispatch(events)

        self.status.hands = len(fused)
        self.status.cameras = tuple(sorted(frames))
        self.status.merged = any(hand.merged for hand in fused)
        self.status.gaze_ready = self.gaze_model.ready

        if self.frame_hook is not None:
            primary = frames.get(self._bank.primary_id) or next(iter(frames.values()))
            self.frame_hook(primary, fused, self.status)

    def _read_gaze(self, frames: dict[int, Frame]) -> GazeObservation:
        """Run the face model on the gaze camera only; it is the expensive one."""
        assert self._bank is not None
        if self._gaze_tracker is None:
            return GazeObservation(present=False)
        primary_id = self._bank.primary_id
        frame = frames.get(primary_id)
        if frame is None:
            return GazeObservation(present=False)
        return fuse_gaze({primary_id: self._gaze_tracker.process(frame)}, primary_id)

    # ------------------------------------------------------------------- gaze arm

    def _apply_gaze(self, gaze: GazeObservation, dt: float) -> None:
        """Warp the cursor to a settled gaze target, when the hand is not busy."""
        cfg = self.cfg.gaze
        if self.cfg.pointer.mode == "hands" or not self.gaze_model.ready or not gaze.usable:
            return
        if gaze.openness < cfg.blink_ear:
            self._fixation.reset()
            return
        # The hand always outranks the eyes. Mid-drag, mid-scroll, or simply while
        # the hand is moving, a warp would fight the user.
        if self.mouse.dragging or self.engine.hand_speed > cfg.hand_quiet_speed:
            self._fixation.reset()
            return

        assert gaze.features is not None
        raw_x, raw_y = self.gaze_model.predict(gaze.features)
        point = self._gaze_filter(raw_x, raw_y, dt)
        self.status.gaze_point = point

        settled = self._fixation.update(*point)
        if settled is None:
            return
        # Only make the big jumps. Small corrections belong to the hand, and
        # warping for them would feel like the cursor twitching under you.
        if self._last_warp is not None:
            moved = max(abs(settled[0] - self._last_warp[0]), abs(settled[1] - self._last_warp[1]))
            if moved < cfg.warp_min_distance:
                return
        self._last_warp = settled
        self.mouse.move_to_fraction(*settled)
        self.status.warps += 1
        self._fixation.reset()

    # --------------------------------------------------------------- dispatching

    def _dispatch(self, events: list[GestureEvent]) -> None:
        self._announce(events)
        for event in events:
            action = event.action
            if action is Action.ENGAGE_TOGGLE:
                self._toggle_engage()
            elif action in (Action.POINTER_MOVE, Action.DRAG_MOVE):
                self.mouse.move_by(event.dx, event.dy)
            elif action is Action.CLICK:
                self.mouse.click(event.button)
            elif action is Action.DRAG_START:
                self.mouse.press(event.button)
            elif action is Action.DRAG_END:
                self.mouse.release(event.button)
            elif action is Action.SCROLL:
                self.mouse.scroll(event.dx, event.dy)
            else:
                self._run_binding(action.value)

    def _announce(self, events: list[GestureEvent]) -> None:
        """Report events to whoever is listening, before they are acted on."""
        if events and self.gesture_hook is not None:
            self.gesture_hook(list(events))

    def _signal_mode(self, engaged: bool) -> None:
        """Keep the helper's idea of the current gesture current.

        Sent every frame but transmitted only on change. Also where a helper that
        died is picked back up, since this runs unconditionally and cheaply.
        """
        if not self.bridge.connected:
            if self.bridge.reconnect():
                self.status.native = True
                print("[bridge] native helper reconnected")
            else:
                self.status.native = False
                return
        self.bridge.set_mode(
            engaged=engaged,
            pointing=self.engine.pointer_active,
            sweeping=self.engine.sweeping,
        )

    def _run_binding(self, gesture: str) -> None:
        action = self.cfg.bindings.get(gesture)
        if action is None:
            return
        self.keyboard.run_action(action)

    def apply_mode(self, mode: Mode | None = None) -> Mode:
        """Adopt a mode, or flip between off and active when none is given.

        The engage gesture and any outside caller come through here together, so
        neither can change mode without control being released on the way out. A
        button left held is the one failure that makes the machine unusable, and
        it must not depend on which route asked.

        Runs on the frame loop, since it touches the cursor.
        """
        if mode is None:
            resolved = self.modes.toggle()
        else:
            self.modes.set_mode(mode)
            resolved = self.modes.mode
        if resolved is Mode.ACTIVE:
            self.engine.rebase()
            self.mouse.refresh_bounds()
        else:
            self._release_control()
        return resolved

    def _toggle_engage(self) -> None:
        self.apply_mode()

    def _release_control(self) -> None:
        """Drop any held button and clear motion state."""
        released = self.engine.release()
        self._announce(released)
        for event in released:
            if event.action is Action.DRAG_END:
                self.mouse.release(event.button)
        self.mouse.release()
        # Belt and braces: the helper drops anything held on its own side too, so a
        # button cannot survive a suspend even if Python's idea of what is held has
        # drifted. A stuck mouse button is the one failure that makes the machine
        # unusable, so it is worth saying twice.
        if self.bridge.connected:
            self.bridge.release_all()
            self.bridge.set_mode(engaged=False, pointing=False, sweeping=False)
        self._fixation.reset()
        self._last_warp = None

    # -------------------------------------------------------------------- config

    def apply_config(self, cfg: Config) -> None:
        """Adopt an edited config without dropping the current session.

        Camera changes need a restart, since a device list change means opening
        different hardware; everything else is re-read in place.
        """
        cameras_changed = (
            cfg.cameras.devices != self.cfg.cameras.devices
            or cfg.cameras.mirror != self.cfg.cameras.mirror
            or cfg.cameras.primary_gaze != self.cfg.cameras.primary_gaze
        )
        self.cfg = cfg
        self.engine = GestureEngine(cfg.pointer, cfg.gestures, cfg.tracking)
        self.fusion = HandFusion(cfg.tracking, cfg.gestures)
        self.keyboard.update_bindings(cfg.keys)
        self.bridge.apply_config(cfg.native, cfg.gestures.double_click_ms)
        self._gaze_filter = OneEuroFilter2D(cfg.gaze.filter_fc_min, cfg.gaze.filter_beta)
        self._fixation = FixationDetector(cfg.gaze.fixation_ms, cfg.gaze.fixation_radius)
        if cameras_changed and not self._paused.is_set():
            self._close_cameras()
            self._open_cameras()
