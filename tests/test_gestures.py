"""The gesture state machine.

These are the tests that matter most, because the machine is where a small
mistake becomes a stuck mouse button or a cursor that toggles itself off three
times a second.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from conftest import synthetic

from mindcontrol.gestures.engine import Action, GestureEngine, State
from mindcontrol.gestures.geometry import Pose

FRAME = 1 / 30.0


def past_tap(cfg) -> float:
    """A frame interval that certainly outlasts a tap, so the pinch becomes a drag.

    Derived from the threshold rather than hardcoded: `tap_max_ms` is tuned per
    hand, and a fixed 0.3 s silently stopped being a drag the first time it was
    raised, failing four tests that were only ever asserting "held long enough".
    """
    return cfg.gestures.tap_max_ms / 1000.0 + 0.1


class Driver:
    """Feeds frames to an engine on a synthetic clock."""

    def __init__(self, cfg, engaged: bool = True) -> None:
        self.cfg = cfg
        self.engine = GestureEngine(cfg.pointer, cfg.gestures, cfg.tracking)
        self.engaged = engaged
        self.now = 0.0

    def step(self, hand=None, dt: float = FRAME):
        self.now += dt
        hands = [hand] if hand is not None else []
        return self.engine.update(hands, self.now, dt, self.engaged)

    def actions(self, hand=None, dt: float = FRAME):
        return [event.action for event in self.step(hand, dt)]

    def hold(self, hand, seconds: float, dt: float = FRAME):
        """Feed the same hand for a duration, returning every action seen."""
        seen = []
        for _ in range(max(int(seconds / dt), 1)):
            seen += self.actions(hand, dt)
        return seen


def test_ready_pose_moves_the_pointer(cfg):
    driver = Driver(cfg)
    moved = []
    for step in range(6):
        moved += [
            event
            for event in driver.step(synthetic(cfg.gestures, anchor=(0.5 + step * 0.02, 0.5)))
            if event.action is Action.POINTER_MOVE
        ]
    assert moved
    assert moved[-1].dx > 0


def test_first_frame_does_not_fling_the_cursor(cfg):
    """A newly seen hand must not produce a jump from nowhere."""
    driver = Driver(cfg)
    assert Action.POINTER_MOVE not in driver.actions(synthetic(cfg.gestures, anchor=(0.9, 0.1)))


def test_quick_pinch_clicks(cfg):
    driver = Driver(cfg)
    driver.step(synthetic(cfg.gestures))
    driver.step(synthetic(cfg.gestures, pinch=0.2))
    assert Action.CLICK in driver.actions(synthetic(cfg.gestures), dt=0.05)


def test_held_pinch_drags_then_releases(cfg):
    driver = Driver(cfg)
    driver.step(synthetic(cfg.gestures))
    driver.step(synthetic(cfg.gestures, pinch=0.2))

    assert Action.DRAG_START in driver.actions(synthetic(cfg.gestures, pinch=0.2), dt=past_tap(cfg))
    assert driver.engine.state is State.DRAGGING
    assert Action.DRAG_MOVE in driver.actions(
        synthetic(cfg.gestures, pinch=0.2, anchor=(0.56, 0.5))
    )
    released = driver.actions(synthetic(cfg.gestures, anchor=(0.56, 0.5)))
    assert Action.DRAG_END in released
    assert Action.CLICK not in released


def test_a_drag_is_not_also_a_click(cfg):
    """Releasing a drag must not additionally fire a click on whatever is under it."""
    driver = Driver(cfg)
    driver.step(synthetic(cfg.gestures))
    driver.step(synthetic(cfg.gestures, pinch=0.2))
    driver.step(synthetic(cfg.gestures, pinch=0.2), dt=past_tap(cfg))
    seen = driver.hold(synthetic(cfg.gestures), 0.5)
    assert seen.count(Action.DRAG_END) == 1
    assert Action.CLICK not in seen


def test_losing_the_hand_mid_drag_releases_the_button(cfg):
    """The most important test here: never leave the mouse held down."""
    driver = Driver(cfg)
    driver.step(synthetic(cfg.gestures))
    driver.step(synthetic(cfg.gestures, pinch=0.2))
    driver.step(synthetic(cfg.gestures, pinch=0.2), dt=past_tap(cfg))
    assert driver.engine.state is State.DRAGGING

    seen = driver.hold(None, 0.6)
    assert seen.count(Action.DRAG_END) == 1
    assert driver.engine.state is State.IDLE


def test_release_on_shutdown(cfg):
    """Suspending or quitting mid-drag must also let go."""
    driver = Driver(cfg)
    driver.step(synthetic(cfg.gestures))
    driver.step(synthetic(cfg.gestures, pinch=0.2))
    driver.step(synthetic(cfg.gestures, pinch=0.2), dt=past_tap(cfg))
    assert [event.action for event in driver.engine.release()] == [Action.DRAG_END]


def test_middle_pinch_right_clicks(cfg):
    driver = Driver(cfg)
    driver.step(synthetic(cfg.gestures))
    driver.step(synthetic(cfg.gestures, pinch_middle=0.2))
    clicks = [
        event for event in driver.step(synthetic(cfg.gestures), dt=0.05)
        if event.action is Action.CLICK
    ]
    assert clicks and clicks[0].button == "right"


def test_pinch_hysteresis_prevents_chatter(cfg):
    """A pinch hovering between the two thresholds must not repeat-click."""
    driver = Driver(cfg)
    driver.step(synthetic(cfg.gestures))
    between = (cfg.gestures.pinch_close + cfg.gestures.pinch_open) / 2
    driver.step(synthetic(cfg.gestures, pinch=0.2))
    seen = driver.hold(synthetic(cfg.gestures, pinch=between), 1.0)
    assert Action.CLICK not in seen


def test_fist_scrolls(cfg):
    driver = Driver(cfg)
    scrolls = []
    for step in range(6):
        scrolls += [
            event
            for event in driver.step(
                synthetic(cfg.gestures, pose=Pose.FIST, anchor=(0.5, 0.5 + step * 0.02))
            )
            if event.action is Action.SCROLL
        ]
    assert scrolls


def test_held_palm_engages_exactly_once(cfg):
    """Holding a palm for seconds must toggle once, not once per frame."""
    driver = Driver(cfg, engaged=False)
    palm = synthetic(cfg.gestures, pose=Pose.OPEN_PALM)
    seen = driver.hold(palm, 3.0)
    assert seen.count(Action.ENGAGE_TOGGLE) == 1


def test_nothing_acts_while_disengaged(cfg):
    """With control off, only the engage gesture may be heard."""
    driver = Driver(cfg, engaged=False)
    seen = []
    for step in range(12):
        seen += driver.actions(
            synthetic(cfg.gestures, anchor=(0.4 + step * 0.03, 0.5), pinch=0.2)
        )
    assert seen == []


def test_fast_palm_swipes(cfg):
    driver = Driver(cfg)
    seen = []
    for step in range(10):
        seen += driver.actions(
            synthetic(cfg.gestures, pose=Pose.OPEN_PALM, anchor=(0.2 + step * 0.06, 0.5))
        )
    assert Action.SWIPE_RIGHT in seen


def test_a_pinch_that_never_looked_ready_still_works(cfg):
    """A well-formed pinch is not a recognisable pose, and must still be heard.

    Pinching thumb to index with the other fingers out classifies as OTHER, not
    READY -- so a hand held already shut never passes the pose filter on its way
    in. Only pinches that happened to stay READY while closing used to register,
    which meant flicked taps clicked while a deliberately held pinch did nothing.
    """
    driver = Driver(cfg)
    shut = synthetic(cfg.gestures, pose=Pose.OTHER, pinch=0.2)
    driver.step(shut)
    assert Action.DRAG_START in driver.actions(shut, dt=past_tap(cfg))


def test_a_resting_hand_is_still_ignored(cfg):
    """The counterpart: admitting pinches must not admit every unposed hand.

    A hand merely in shot -- on the desk, holding a mug -- reads as OTHER too. It
    is only interesting once it is measurably shut, so an open one stays ignored.
    """
    driver = Driver(cfg)
    idle = synthetic(cfg.gestures, pose=Pose.OTHER, pinch=cfg.gestures.pinch_open + 0.2)
    seen = []
    for step in range(10):
        seen += driver.actions(
            replace_anchor(idle, cfg, 0.3 + step * 0.04), dt=past_tap(cfg)
        )
    assert seen == []


def replace_anchor(hand, cfg, x: float):
    """The same hand at a new position, so movement cannot be mistaken for stillness."""
    return replace(hand, anchor=(x, 0.5))


def _flickering_sweep(cfg, frames: int = 12) -> list[Action]:
    """Sweep a palm sideways while the classifier loses it every third frame.

    The dropped frames read as FIST, which is what makes this the hard case: FIST
    is an actionable pose, so it reaches the scroll branch and clears the travel
    accumulated so far. A pose the engine ignores outright would leave the sweep
    untouched and prove nothing.
    """
    driver = Driver(cfg)
    seen: list[Action] = []
    for step in range(frames):
        pose = Pose.FIST if step % 3 == 2 else Pose.OPEN_PALM
        seen += driver.actions(
            synthetic(cfg.gestures, pose=pose, anchor=(0.15 + step * 0.06, 0.5))
        )
    return seen


def test_a_sweep_survives_the_palm_flickering_out(cfg):
    """A blurred frame mid-sweep must not wipe the travel already accumulated.

    Real sweeps lose the pose for a good fraction of their length -- the hand is
    moving, rotating and blurred -- so a swipe that demands OPEN_PALM on every
    frame never finishes. The grace window is what makes the gesture reachable.
    """
    tuned = replace(cfg, gestures=replace(cfg.gestures, swipe_grace_ms=600.0))
    seen = _flickering_sweep(tuned)
    assert Action.SWIPE_RIGHT in seen
    # And it must not be mistaken for the scroll those flickers look like.
    assert Action.SCROLL not in seen


def test_without_the_grace_window_a_flickering_sweep_is_lost(cfg):
    """The counterpart: at the default of zero, the same sweep yields nothing.

    Pinned so the grace window cannot be quietly defaulted back to zero and leave
    the test above passing for some unrelated reason.
    """
    strict = replace(cfg, gestures=replace(cfg.gestures, swipe_grace_ms=0.0))
    assert Action.SWIPE_RIGHT not in _flickering_sweep(strict)


def test_a_latched_sweep_still_scrolls_once_the_palm_is_gone(cfg):
    """The latch must expire: a fist after a sweep has to scroll, not stay a palm."""
    tuned = replace(cfg, gestures=replace(cfg.gestures, swipe_grace_ms=600.0))
    driver = Driver(tuned)
    driver.step(synthetic(tuned.gestures, pose=Pose.OPEN_PALM, anchor=(0.3, 0.5)))
    seen = []
    for step in range(10):
        # Well past the 600 ms window at the driver's frame interval.
        seen += driver.actions(
            synthetic(tuned.gestures, pose=Pose.FIST, anchor=(0.5, 0.4 + step * 0.02)),
            dt=0.1,
        )
    assert Action.SCROLL in seen


def test_swiping_does_not_also_engage(cfg):
    """A moving palm is a swipe; only a still one is the toggle."""
    driver = Driver(cfg)
    seen = []
    for step in range(10):
        seen += driver.actions(
            synthetic(cfg.gestures, pose=Pose.OPEN_PALM, anchor=(0.2 + step * 0.06, 0.5))
        )
    assert Action.ENGAGE_TOGGLE not in seen


def test_the_driving_hand_keeps_control(cfg):
    """A second hand appearing must not steal the cursor mid-gesture."""
    driver = Driver(cfg)
    right = synthetic(cfg.gestures, handed="Right", anchor=(0.5, 0.5))
    driver.step(right)
    driver.step(synthetic(cfg.gestures, handed="Right", anchor=(0.52, 0.5)))

    intruder = synthetic(cfg.gestures, handed="Left", anchor=(0.1, 0.9))
    driver.now += FRAME
    driver.engine.update(
        [synthetic(cfg.gestures, handed="Right", anchor=(0.54, 0.5)), intruder],
        driver.now,
        FRAME,
        True,
    )
    assert driver.engine.status().endswith("Right")


@pytest.mark.parametrize("pose", [Pose.OTHER, Pose.NONE])
def test_unrecognised_poses_do_nothing(cfg, pose):
    """A hand that is merely visible must be ignored."""
    driver = Driver(cfg)
    assert driver.hold(synthetic(cfg.gestures, pose=pose), 1.0) == []
