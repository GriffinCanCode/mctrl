"""The gesture state machine.

Turns a stream of measured hands into discrete intents. Three ideas do most of
the work:

*Hysteresis* -- a pinch closes at one distance and opens at a wider one, so a
hand hovering near the threshold does not machine-gun clicks.

*Latching* -- a held pose fires once and then refuses to fire again until you
change pose. Without it, holding an open palm for three seconds would toggle
control three times.

*Fail-safe release* -- if the hand vanishes mid-drag the button is released.
Losing tracking should never leave the mouse stuck down.

The machine emits pixel deltas rather than raw landmark deltas: pointer feel
(sensitivity, acceleration, smoothing) is a property of the gesture layer, and
the control layer below should stay a dumb, testable event emitter.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from ..config import GestureConfig, PointerConfig, TrackingConfig
from ..filters import OneEuroFilter2D
from ..geometry import HandFeatures, Pose


class Action(Enum):
    POINTER_MOVE = "pointer_move"
    CLICK = "click"
    DRAG_START = "drag_start"
    DRAG_MOVE = "drag_move"
    DRAG_END = "drag_end"
    SCROLL = "scroll"
    ENGAGE_TOGGLE = "engage_toggle"
    TELEPHONE = "telephone"
    SWIPE_LEFT = "swipe_left"
    SWIPE_RIGHT = "swipe_right"
    PALM_PUSH_UP = "palm_push_up"


class State(Enum):
    IDLE = "idle"
    POINTING = "pointing"
    PINCHED = "pinched"
    DRAGGING = "dragging"
    SCROLLING = "scrolling"
    HOLDING_PALM = "holding_palm"
    HOLDING_PHONE = "holding_phone"


@dataclass(frozen=True)
class GestureEvent:
    action: Action
    dx: float = 0.0
    dy: float = 0.0
    button: str = "left"


# Poses that mean "this hand is talking to the computer". A hand that is merely
# visible -- resting on the desk, holding a mug -- is deliberately ignored.
ACTIONABLE = (Pose.READY, Pose.FIST, Pose.OPEN_PALM, Pose.TELEPHONE)


class GestureEngine:
    """Interprets hands frame by frame."""

    def __init__(
        self, pointer: PointerConfig, gestures: GestureConfig, tracking: TrackingConfig
    ) -> None:
        self._pointer = pointer
        self._cfg = gestures
        self._tracking = tracking

        self.state = State.IDLE
        self._hand_label: str | None = None
        self._pose = Pose.NONE

        self._anchor = OneEuroFilter2D(pointer.filter_fc_min, pointer.filter_beta)
        self._last_anchor: tuple[float, float] | None = None
        self._lost_since: float | None = None

        self._pinch_closed = False
        self._pinch_started_at = 0.0
        self._pinch_origin = (0.0, 0.0)
        self._pinch_button = "left"

        self._pose_since = 0.0
        self._pose_origin = (0.0, 0.0)
        self._pose_consumed = False
        self._cooldown_until = 0.0
        self._clear_sweep()
        # Negative infinity so nothing is latched until a palm is actually seen.
        self._palm_seen_at = float("-inf")

    # ---------------------------------------------------------------- properties

    @property
    def hand_speed(self) -> float:
        """Smoothed hand speed in normalised units per second."""
        return self._anchor.speed

    @property
    def pointer_active(self) -> bool:
        return self.state in (State.POINTING, State.PINCHED, State.DRAGGING)

    def status(self) -> str:
        hand = self._hand_label or "-"
        return f"{self.state.value} [{self._pose.value}] {hand}"

    # -------------------------------------------------------------------- update

    def update(
        self, hands: list[HandFeatures], now: float, dt: float, engaged: bool
    ) -> list[GestureEvent]:
        """Advance the machine one frame and return everything it wants done."""
        hand = self._select(hands)

        if hand is None:
            return self._handle_missing_hand(now)
        self._lost_since = None

        if not engaged:
            # Disengaged, exactly one gesture is listened for: the palm hold that
            # turns control back on. Anything else must be inert, or you could
            # never safely put your hands down.
            return self._watch_for_engage(hand, now)

        return self._handle_engaged(hand, now, dt)

    # ------------------------------------------------------------------ internals

    def _select(self, hands: list[HandFeatures]) -> HandFeatures | None:
        """Pick the hand in charge, preferring the one already driving.

        Sticking with the current hand matters: mid-drag, a second hand entering
        frame must not steal the cursor.
        """
        usable = [h for h in hands if h.pose in ACTIONABLE or self._is_pinching(h)]
        if not usable:
            return None
        if self._hand_label is not None:
            for hand in usable:
                if hand.handedness == self._hand_label:
                    return hand
        return max(usable, key=lambda h: h.score)

    def _is_pinching(self, hand: HandFeatures) -> bool:
        """True while a hand is pinching, whether it just started or is mid-pinch.

        A pinching hand reads as no recognisable pose, since the index curls in to
        meet the thumb. Both halves matter, and only the second was here at first:
        a hand already pinching keeps control of the pointer down to `pinch_open`,
        and a hand measurably shut past `pinch_close` may *take* control.

        Without that first half a well-formed pinch was unreachable. Every hand is
        filtered on pose before the pinch machinery runs, so a pinch that never
        passed through READY on its way shut -- exactly what holding one looks like
        -- was discarded before anything could measure it. Only pinches that
        happened to stay READY while closing worked, which is why quick taps
        clicked and a deliberate held pinch did nothing at all.
        """
        if self._pinch_closed:
            return hand.pinch < self._cfg.pinch_open
        return hand.pinch < self._cfg.pinch_close

    def _handle_missing_hand(self, now: float) -> list[GestureEvent]:
        if self._lost_since is None:
            self._lost_since = now
        if (now - self._lost_since) * 1000.0 < self._tracking.stale_after_ms:
            return []
        events: list[GestureEvent] = []
        if self.state is State.DRAGGING:
            events.append(GestureEvent(Action.DRAG_END, button=self._pinch_button))
        self._reset()
        return events

    def _watch_for_engage(self, hand: HandFeatures, now: float) -> list[GestureEvent]:
        self._track_pose(hand, now)
        if hand.pose is not Pose.OPEN_PALM:
            return []
        if self._hold_satisfied(hand, now, self._cfg.engage_hold_ms):
            self._consume(now)
            return [GestureEvent(Action.ENGAGE_TOGGLE)]
        return []

    def _handle_engaged(self, hand: HandFeatures, now: float, dt: float) -> list[GestureEvent]:
        events: list[GestureEvent] = []
        self._hand_label = hand.handedness

        # A sweeping palm is a moving, rotating, motion-blurred palm, and the pose
        # drops out partway through: one recording held OPEN_PALM for 100% of a
        # still palm but only 25% of the sweep. Demanding the pose every frame means
        # the travel is wiped mid-gesture and the swipe never completes, so an
        # established palm keeps its status through a brief flicker. Some of those
        # flickers read as FIST, which is why the latch outranks the scroll branch --
        # otherwise a sweep turns into a scroll halfway across.
        if hand.pose is Pose.OPEN_PALM:
            self._palm_seen_at = now
        latched = (now - self._palm_seen_at) * 1000.0 <= self._cfg.swipe_grace_ms

        self._track_pose(hand, now, keep_travel=latched)

        smoothed = self._anchor(hand.anchor[0], hand.anchor[1], dt)
        delta = self._delta(smoothed)

        events.extend(self._update_pinch(hand, now))

        if self.state is State.DRAGGING:
            events.extend(self._emit_motion(delta, Action.DRAG_MOVE))
            return events

        if self.state is State.PINCHED:
            events.extend(self._emit_motion(delta, Action.POINTER_MOVE))
            return events

        if hand.pose is Pose.FIST and not latched:
            self.state = State.SCROLLING
            events.extend(self._emit_scroll(delta))
            return events

        if hand.pose is Pose.OPEN_PALM or latched:
            self.state = State.HOLDING_PALM
            events.extend(self._update_palm(hand, now, delta, dt))
            return events

        if hand.pose is Pose.TELEPHONE:
            self.state = State.HOLDING_PHONE
            if self._hold_satisfied(hand, now, self._cfg.dictation_hold_ms):
                self._consume(now)
                events.append(GestureEvent(Action.TELEPHONE))
            return events

        if hand.pose is Pose.READY:
            self.state = State.POINTING
            events.extend(self._emit_motion(delta, Action.POINTER_MOVE))
            return events

        self.state = State.IDLE
        return events

    def _clear_sweep(self) -> None:
        """Discard the accumulated sweep, peak included.

        Both together, always: a peak surviving its travel would arm the next
        sweep with speed the hand never reached during it.
        """
        self._swipe_travel = (0.0, 0.0)
        self._swipe_peak = 0.0

    def _track_pose(self, hand: HandFeatures, now: float, keep_travel: bool = False) -> None:
        """Restart the hold timer whenever the pose changes or the hand drifts.

        ``keep_travel`` spares the accumulated sweep. Hold timers still restart --
        a flicker genuinely is not a held pose -- but zeroing the travel would
        discard the first half of a swipe every time the palm blurred.
        """
        if hand.pose is not self._pose:
            self._pose = hand.pose
            self._pose_since = now
            self._pose_origin = hand.anchor
            self._pose_consumed = False
            if not keep_travel:
                self._clear_sweep()
        elif _distance(hand.anchor, self._pose_origin) > self._cfg.hold_max_travel:
            self._pose_since = now
            self._pose_origin = hand.anchor

    def _hold_satisfied(self, hand: HandFeatures, now: float, duration_ms: float) -> bool:
        if self._pose_consumed or now < self._cooldown_until:
            return False
        if _distance(hand.anchor, self._pose_origin) > self._cfg.hold_max_travel:
            return False
        return (now - self._pose_since) * 1000.0 >= duration_ms

    def _consume(self, now: float) -> None:
        """Latch the current pose and start the refractory window."""
        self._pose_consumed = True
        self._cooldown_until = now + self._cfg.gesture_cooldown_ms / 1000.0

    def _delta(self, smoothed: tuple[float, float]) -> tuple[float, float]:
        """Movement since the last frame, suppressing the jump on first sight."""
        if self._last_anchor is None:
            self._last_anchor = smoothed
            return 0.0, 0.0
        delta = (smoothed[0] - self._last_anchor[0], smoothed[1] - self._last_anchor[1])
        self._last_anchor = smoothed
        return delta

    def _update_pinch(self, hand: HandFeatures, now: float) -> list[GestureEvent]:
        """Drive the pinch sub-machine: closing, promotion to drag, and release."""
        events: list[GestureEvent] = []
        distance = hand.pinch

        # A fist folds the thumb alongside the curled fingers, so its thumb-to-index
        # distance can read exactly like a pinch -- on some hands the two ranges
        # overlap almost entirely. A fist means scroll, so it must never open a
        # pinch: otherwise it silently starts a drag and then never scrolls, because
        # dragging outranks every pose. An in-flight pinch is left alone, so curling
        # the rest of the hand mid-drag does not drop what you are holding.
        #
        # Exempting a tightly-closed fist was tried and reverted. Both candidate
        # discriminators fail on real recordings: thumb-to-index distance overlaps a
        # genuine pinch almost entirely, and index reach -- which does separate the
        # two by median -- still leaves 5% of fist frames in pinch territory. A
        # single such frame latches PINCHED, and PINCHED outranks the scroll branch
        # below, so one misread costs the remainder of the scroll. Blunt, but it
        # fails in the direction that keeps scrolling working.
        if not self._pinch_closed and hand.pose is Pose.FIST:
            return events

        if not self._pinch_closed and distance < self._cfg.pinch_close:
            self._pinch_closed = True
            self._pinch_started_at = now
            self._pinch_origin = hand.anchor
            self._pinch_button = "right" if hand.pinch_is_middle else "left"
            if self.state is not State.DRAGGING:
                self.state = State.PINCHED
            return events

        if not self._pinch_closed:
            return events

        held_ms = (now - self._pinch_started_at) * 1000.0
        travel = _distance(hand.anchor, self._pinch_origin)

        if distance > self._cfg.pinch_open:
            self._pinch_closed = False
            if self.state is State.DRAGGING:
                self.state = State.POINTING
                events.append(GestureEvent(Action.DRAG_END, button=self._pinch_button))
            elif self._is_tap(held_ms, travel):
                self.state = State.POINTING
                events.append(GestureEvent(Action.CLICK, button=self._pinch_button))
            else:
                self.state = State.POINTING
            return events

        # Still pinched. A left pinch held past the tap window becomes a drag;
        # a right pinch has no drag equivalent, so it just waits for release.
        if (
            self.state is State.PINCHED
            and self._pinch_button == "left"
            and held_ms >= self._cfg.tap_max_ms
        ):
            self.state = State.DRAGGING
            events.append(GestureEvent(Action.DRAG_START, button="left"))
        return events

    def _is_tap(self, held_ms: float, travel: float) -> bool:
        if travel > self._cfg.tap_max_travel:
            return False
        # A right pinch is always a deliberate act, so it is not time limited;
        # a left pinch has to be quick or it would have become a drag.
        return self._pinch_button == "right" or held_ms < self._cfg.tap_max_ms

    def _emit_motion(self, delta: tuple[float, float], action: Action) -> list[GestureEvent]:
        dx, dy = delta
        if math.hypot(dx, dy) < self._pointer.deadzone:
            return []
        gain = self._gain()
        scale = self._pointer.sensitivity * gain
        return [GestureEvent(action, dx=dx * scale, dy=dy * scale)]

    def _gain(self) -> float:
        """Pointer acceleration: precise when slow, sweeping when fast."""
        cfg = self._pointer
        ratio = self._anchor.speed / max(cfg.gain_speed_reference, 1e-6)
        return min(cfg.gain_min + (cfg.gain_max - cfg.gain_min) * ratio, cfg.gain_max)

    def _emit_scroll(self, delta: tuple[float, float]) -> list[GestureEvent]:
        dx, dy = delta
        if math.hypot(dx, dy) < self._cfg.scroll_deadzone:
            return []
        scale = self._cfg.scroll_sensitivity
        return [GestureEvent(Action.SCROLL, dx=dx * scale, dy=dy * scale)]

    def _update_palm(
        self, hand: HandFeatures, now: float, delta: tuple[float, float], dt: float
    ) -> list[GestureEvent]:
        """An open palm either sweeps (swipe) or sits still (engage toggle).

        The two never collide, because a swipe is defined by speed and the toggle
        by the absence of it.
        """
        self._swipe_travel = (
            self._swipe_travel[0] + delta[0],
            self._swipe_travel[1] + delta[1],
        )
        # Peak speed over the sweep, not the speed on the frame that happens to
        # complete the travel. The two conditions are anti-correlated in real
        # motion -- speed peaks early, while travel only accumulates later, by
        # which time the hand is decelerating into the end of its arc -- and a
        # multi-camera rig makes it worse: every change of leading camera drops
        # that frame's delta to zero to stop the cursor flinging, so a fast sweep
        # crossing between views reports standstill for a third of its frames. On
        # one recording 39 frames were fast enough and 21 had travelled far
        # enough, but only 2 managed both at once, and the sweep read as nothing.
        speed = math.hypot(*delta) / max(dt, 1e-6)
        self._swipe_peak = max(self._swipe_peak, speed)

        if self._swipe_peak >= self._cfg.swipe_min_speed and now >= self._cooldown_until:
            travel_x, travel_y = self._swipe_travel
            if abs(travel_x) >= self._cfg.swipe_min_travel and abs(travel_x) >= abs(travel_y):
                self._consume(now)
                self._clear_sweep()
                action = Action.SWIPE_RIGHT if travel_x > 0 else Action.SWIPE_LEFT
                return [GestureEvent(action)]
            if -travel_y >= self._cfg.swipe_min_travel and abs(travel_y) > abs(travel_x):
                self._consume(now)
                self._clear_sweep()
                return [GestureEvent(Action.PALM_PUSH_UP)]
            return []

        if self._hold_satisfied(hand, now, self._cfg.engage_hold_ms):
            self._consume(now)
            return [GestureEvent(Action.ENGAGE_TOGGLE)]
        return []

    def _reset(self) -> None:
        self.state = State.IDLE
        self._pose = Pose.NONE
        self._hand_label = None
        self._pinch_closed = False
        self._last_anchor = None
        self._pose_consumed = False
        self._clear_sweep()
        # Losing the hand ends any sweep; the latch must not span that gap.
        self._palm_seen_at = float("-inf")
        self._anchor.reset()

    def rebase(self) -> None:
        """Forget the motion baseline, without disturbing gesture state.

        Called when the camera leading for position changes: the anchor jumps to
        a new viewpoint, and differencing across that jump would fling the cursor.
        """
        self._last_anchor = None
        self._anchor.reset()

    def release(self) -> list[GestureEvent]:
        """Drop everything held, for suspend or shutdown. Never leaves a button down."""
        events: list[GestureEvent] = []
        if self.state is State.DRAGGING:
            events.append(GestureEvent(Action.DRAG_END, button=self._pinch_button))
        self._reset()
        return events


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])
