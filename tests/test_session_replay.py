"""Round-tripping sessions, and replaying real recordings.

The synthetic tests prove the logic is self-consistent. These prove it survives
contact with actual hands -- but only once a recording exists, so they skip
cleanly on a machine that has never run `mindcontrol record`.

That skip is deliberate rather than lazy: a test that silently invented its own
input would report success while checking nothing about the person using this.
"""

from __future__ import annotations

import numpy as np
import pytest
from conftest import build_hand

from mindcontrol import replay as replay_module
from mindcontrol import session as session_module
from mindcontrol.gestures.engine import Action
from mindcontrol.session import (
    RecordedFrame,
    RecordedHand,
    RecordedView,
    Session,
    SessionWriter,
)

# A prompt's frames should mostly classify as the pose that was asked for.
POSE_ACCURACY_FLOOR = 0.7


def _hand(points: np.ndarray) -> RecordedHand:
    return RecordedHand("Right", "Left", 0.9, points, points)


def test_session_round_trips(tmp_path):
    """Landmarks must survive the write/read cycle intact."""
    path = tmp_path / "s.jsonl"
    points = build_hand()
    with SessionWriter(path, note="unit") as writer:
        writer.add(RecordedFrame(0.0, "fist", [RecordedView(0, [_hand(points)])]))
        writer.add(RecordedFrame(0.033, "fist", [RecordedView(0, [])]))

    loaded = Session.load(path)
    assert loaded.header["note"] == "unit"
    assert len(loaded.frames) == 2
    assert loaded.labels() == ["fist"]
    assert loaded.cameras == (0,)
    assert loaded.frames[0].hands[0].world == pytest.approx(points, abs=1e-4)
    assert list(loaded.hands("fist"))


def test_multi_camera_views_round_trip(tmp_path):
    """Per-camera grouping and staleness must survive, or fusion cannot be replayed."""
    path = tmp_path / "two.jsonl"
    points = build_hand()
    with SessionWriter(path) as writer:
        writer.add(
            RecordedFrame(
                0.0,
                "ready",
                [
                    RecordedView(0, [_hand(points)], age_ms=0.0),
                    RecordedView(3, [_hand(points)], age_ms=17.5),
                ],
            )
        )

    frame = Session.load(path).frames[0]
    assert frame.cameras == (0, 3)
    assert [view.age_ms for view in frame.views] == [0.0, 17.5]
    assert len(frame.hands) == 2


def test_remeasure_respects_supplied_thresholds(cfg):
    """The same landmarks must be re-classifiable under different thresholds.

    This is the property the whole tuning workflow rests on: if remeasuring
    ignored the config passed to it, autotune would be fitting to nothing.
    """
    from dataclasses import replace

    hand = _hand(build_hand(curled=("middle", "ring", "pinky")))
    strict = replace(cfg.gestures, finger_extended=99.0)

    # Slot 0 is the thumb, so the four fingers occupy slots 1-4.
    shipped = hand.remeasure(cfg.gestures).extended
    assert shipped[1] is True, "index should read extended"
    assert shipped[2] is False, "middle was curled"
    # An unreachable finger threshold curls all four fingers; the thumb is
    # governed by thumb_extended instead, so it is untouched.
    assert hand.remeasure(strict).extended[1:] == (False, False, False, False)


def test_rejects_unknown_format(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"kind": "mindcontrol-session", "format": 999}\n')
    with pytest.raises(ValueError, match="format"):
        Session.load(path)


# ---------------------------------------------------------------------------
# Everything below needs a real recording.


def newest_with(*labels: str) -> Session:
    """The most recent recording containing every named prompt.

    Not simply the newest file, and not one single "full" recording either.
    `record --focus` exists precisely so one gesture can be re-taken on its own,
    which means the best pinch data and the best swipe data routinely live in
    different files. Each test therefore asks for the newest recording that can
    answer *its* question: judging a re-tuned pinch against a months-old full
    session would report a code fault long after the recording was fixed.
    """
    if not (paths := sorted(session_module.SESSIONS_DIR.glob("*.jsonl"), reverse=True)):
        pytest.skip("no recording yet; run 'mindcontrol record'")

    wanted = set(labels)
    for path in paths:
        try:
            session = Session.load(path)
        except (ValueError, KeyError):
            # An unreadable recording is a reason to look at the next one, not to
            # fail every test in the file with a stack trace about JSON.
            continue
        if wanted <= set(session.labels()):
            return session

    pytest.skip(f"no recording covers {sorted(wanted)}; run 'mindcontrol record'")


@pytest.fixture(scope="module")
def recorded() -> Session:
    """The newest recording that covers the whole script."""
    from mindcontrol.record import SCRIPT

    return newest_with(*(prompt.label for prompt in SCRIPT))


def _require_clean(session: Session, *labels: str) -> None:
    """Skip when the named prompts cannot answer the question asked of them.

    A prompt recorded with the other hand also in shot, or with the hand barely
    tracked, tells us nothing about whether the code works. Failing on it would
    report a code fault where there is only a recording fault -- and that is the
    distinction that decides whether to change the thresholds or re-record.
    """
    for problem in session.problems():
        for label in labels:
            if problem.startswith(f"'{label}'"):
                pytest.skip(f"{problem}; re-record to test this")


def test_recording_covers_every_prompt(recorded):
    from mindcontrol.record import SCRIPT

    missing = {prompt.label for prompt in SCRIPT} - set(recorded.labels())
    assert not missing, f"recording is missing prompts: {sorted(missing)}"


def test_held_poses_classify_as_intended(recorded, cfg):
    """Each held pose must be recognised in most of its frames.

    This is the test that catches thresholds tuned for the wrong hands: if it
    fails on a clean recording, `mindcontrol autotune` has something to fix.
    """
    dirty = {
        label
        for label in replay_module.EXPECTED_POSE
        for problem in recorded.problems()
        if problem.startswith(f"'{label}'")
    }
    scores = {
        label: share
        for label, share in replay_module.accuracy(recorded, cfg).items()
        if label not in dirty
    }
    if not scores:
        pytest.skip(f"every held prompt has recording problems: {sorted(dirty)}")

    poor = {
        label: round(share, 2)
        for label, share in scores.items()
        if share < POSE_ACCURACY_FLOOR
    }
    assert not poor, f"poses under-recognised: {poor} (run 'mindcontrol autotune --apply')"


def test_real_pinches_produce_clicks(cfg):
    """The deliberate taps in the recording must come out as clicks."""
    session = newest_with("pinch_cycle", "fist")
    _require_clean(session, "pinch_cycle")
    result = replay_module.replay(session, cfg)
    clicks = result.total(Action.CLICK, "pinch_cycle")
    assert clicks >= 3, f"only {clicks} clicks from the pinch_cycle segment"


def test_a_pinch_never_costs_a_scroll(cfg):
    """Whatever makes the pinch reachable must not make a fist stop scrolling.

    These two pull against each other: a fist measures as pinched, so loosening
    `pinch_close` far enough to catch a real pinch eventually catches the fist on
    the way in -- and a latched pinch outranks the scroll branch, so scrolling
    dies silently. Asserted on one recording holding both gestures, because that
    is the only place the trade-off is visible.
    """
    session = newest_with("pinch_cycle", "fist")
    result = replay_module.replay(session, cfg)
    assert result.total(Action.SCROLL, "fist") >= 1, "pinch_close is starving the scroll"
    assert result.total(Action.DRAG_START, "fist") == 0, "a fist is opening a pinch"


def test_sustained_pinch_becomes_a_drag_not_clicks(cfg):
    """Holding a pinch shut must drag once, not emit a stream of clicks."""
    session = newest_with("pinch_closed")
    _require_clean(session, "pinch_closed")
    result = replay_module.replay(session, cfg)
    assert result.total(Action.CLICK, "pinch_closed") == 0
    assert result.total(Action.DRAG_START, "pinch_closed") >= 1


def test_every_real_drag_is_released(recorded, cfg):
    """Across an entire real session, drags and releases must balance."""
    counts = replay_module.replay(recorded, cfg).counts()
    assert counts[Action.DRAG_START] == counts[Action.DRAG_END]


def test_real_fist_scrolls(cfg):
    result = replay_module.replay(newest_with("fist"), cfg)
    assert result.total(Action.SCROLL, "fist") >= 1


def test_real_swipes_are_detected(cfg):
    result = replay_module.replay(newest_with("swipe"), cfg)
    swipes = result.total(Action.SWIPE_LEFT, "swipe") + result.total(Action.SWIPE_RIGHT, "swipe")
    assert swipes >= 1


def test_nothing_leaks_through_while_disengaged(recorded, cfg):
    """Replayed with control off, only engage toggles may fire.

    A leak here would mean the app acts on your hands after you told it to stop.
    """
    result = replay_module.replay(recorded, cfg, engaged=False)
    leaked = {
        action: count
        for action, count in result.counts().items()
        if action is not Action.ENGAGE_TOGGLE
    }
    assert not leaked, f"gestures fired while disengaged: {leaked}"


def test_resting_hand_does_not_act(recorded, cfg):
    """With no hand in frame, nothing may happen.

    Checks its own premise first: if a hand was actually visible during the
    baseline, the recording cannot answer this, and pretending otherwise would
    report a code fault where there is only a recording fault.
    """
    frames = list(recorded.segment("none"))
    if not frames:
        pytest.skip("recording has no baseline segment")
    visible = sum(1 for frame in frames if frame.hands) / len(frames)
    if visible > 0.1:
        pytest.skip(
            f"a hand was visible in {visible:.0%} of the baseline; re-record with "
            "both hands out of frame"
        )

    result = replay_module.replay(recorded, cfg)
    assert not result.counts("none"), f"events during the 'none' baseline: {result.counts('none')}"
