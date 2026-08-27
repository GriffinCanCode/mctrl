"""End-to-end replay over a scripted session.

These are the same assertions made against a real recording, run against
deterministic synthetic input so the machinery is covered on any machine. When
one of these fails the pipeline is broken; when the equivalent real-recording
test fails but this passes, the thresholds simply do not suit those hands.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from mindcontrol import replay as replay_module
from mindcontrol.gestures.engine import Action


def test_every_prompt_classifies_as_intended(synthetic_session, cfg):
    scores = replay_module.accuracy(synthetic_session, cfg)
    assert scores
    for label, share in scores.items():
        assert share > 0.95, f"{label} only classified {share:.0%} of the time"


def test_taps_become_clicks(synthetic_session, cfg):
    """Eight scripted taps should yield roughly eight clicks."""
    result = replay_module.replay(synthetic_session, cfg)
    assert 6 <= result.total(Action.CLICK, "pinch_cycle") <= 8


def test_sustained_pinch_drags_without_clicking(synthetic_session, cfg):
    result = replay_module.replay(synthetic_session, cfg)
    assert result.total(Action.DRAG_START, "pinch_closed") == 1
    assert result.total(Action.CLICK, "pinch_closed") == 0


def test_drags_always_balance(synthetic_session, cfg):
    counts = replay_module.replay(synthetic_session, cfg).counts()
    assert counts[Action.DRAG_START] == counts[Action.DRAG_END] > 0


def test_fist_scrolls(synthetic_session, cfg):
    result = replay_module.replay(synthetic_session, cfg)
    assert result.total(Action.SCROLL, "fist") > 50


def test_four_sweeps_give_swipes_both_ways(synthetic_session, cfg):
    result = replay_module.replay(synthetic_session, cfg)
    assert result.total(Action.SWIPE_LEFT, "swipe") >= 1
    assert result.total(Action.SWIPE_RIGHT, "swipe") >= 1


def test_baseline_produces_nothing(synthetic_session, cfg):
    """No hand in frame must mean no events at all."""
    assert not replay_module.replay(synthetic_session, cfg).counts("none")


def test_disengaged_only_hears_the_engage_gesture(synthetic_session, cfg):
    result = replay_module.replay(synthetic_session, cfg, engaged=False)
    assert result.total(Action.ENGAGE_TOGGLE) == 1
    leaked = {a: c for a, c in result.counts().items() if a is not Action.ENGAGE_TOGGLE}
    assert not leaked, f"leaked while disengaged: {leaked}"


def test_replay_is_deterministic(synthetic_session, cfg):
    """Two replays of one session must agree, or regression testing is worthless."""
    first = replay_module.replay(synthetic_session, cfg)
    second = replay_module.replay(synthetic_session, cfg)
    assert first.counts() == second.counts()


def test_autotune_separates_every_threshold(synthetic_session, cfg):
    """On clean data the fitter should decline nothing and stay in range."""
    from mindcontrol.autotune import analyse

    suggestions = analyse(synthetic_session, cfg.gestures)
    declined = [s.key for s in suggestions if s.proposed is None]
    assert not declined, f"could not fit: {declined}"
    for suggestion in suggestions:
        assert 0.0 <= suggestion.proposed < 10.0, f"{suggestion.key} out of range"


def test_two_cameras_actually_merge(fused_session, cfg):
    """A second viewpoint must be merged in, not silently dropped."""
    result = replay_module.replay(fused_session, cfg)
    assert fused_session.cameras == (0, 1)
    assert result.merged_frames > 100, f"only {result.merged_frames} frames merged"


def test_a_worse_second_camera_does_not_break_gestures(fused_session, cfg):
    """Adding a noisier camera must not cost you clicks, drags, or swipes.

    This is the real risk of fusion: averaging in a bad viewpoint could blunt
    every gesture. Confidence weighting exists to prevent that, and this is what
    demonstrates it working.
    """
    result = replay_module.replay(fused_session, cfg)
    assert 6 <= result.total(Action.CLICK, "pinch_cycle") <= 9
    assert result.total(Action.DRAG_START, "pinch_closed") >= 1
    assert result.total(Action.SCROLL, "fist") > 50
    assert result.total(Action.SWIPE_LEFT, "swipe") >= 1
    assert result.total(Action.SWIPE_RIGHT, "swipe") >= 1


PARALLAX = 0.10
STRIDE = 0.02


def _two_camera_track(cfg, margin: float, hand_off: int | None = None):
    """Drive fusion with two offset views of one hand crossing the frame.

    The cameras disagree about where the hand is by `PARALLAX`, as real ones do,
    while it moves a steady `STRIDE` each frame. Returns the anchors fusion chose
    to emit, and how many of those it asked the pointer to rebase across.
    """
    from mindcontrol.fusion import HandFusion, Observation

    from conftest import synthetic

    fusion = HandFusion(replace(cfg.tracking, leader_margin=margin), cfg.gestures)
    anchors: list[tuple[float, float]] = []
    rebases = 0
    for step in range(10):
        near = 0.30 + step * STRIDE
        # Camera 1 takes the lead at `hand_off`, by out-scoring the incumbent.
        leading_second = hand_off is not None and step >= hand_off
        views = [
            Observation(
                0,
                [
                    replace(
                        synthetic(cfg.gestures, anchor=(near, 0.5)),
                        score=0.5 if leading_second else 0.99,
                    )
                ],
                0.0,
            ),
            Observation(
                1,
                [
                    replace(
                        synthetic(cfg.gestures, anchor=(near + PARALLAX, 0.5)),
                        score=0.99 if leading_second else 0.5,
                    )
                ],
                0.0,
            ),
        ]
        for fused in fusion.fuse(views):
            anchors.append(fused.features.anchor)
            rebases += fused.rebased
    return anchors, rebases


def test_a_handover_costs_neither_position_nor_motion(cfg):
    """Changing leading camera must not jump the pointer or eat a frame of motion.

    These used to be a trade-off. Two views of one hand disagree by their parallax,
    so the old code told the pointer to forget its baseline at every handover --
    no jump, but that frame's movement was gone. A fast gesture crossing between
    views spends a third of its frames handing over, which is how a real sweep
    changed leader 27 times in eight seconds and registered one swipe out of four.
    Stitching the new leader's own movement onto the track pays for neither.
    """
    anchors, rebases = _two_camera_track(cfg, margin=0.0, hand_off=5)
    steps = [b[0] - a[0] for a, b in zip(anchors, anchors[1:], strict=False)]

    assert rebases == 0, "a handover should be absorbed, not charged to the pointer"
    for step in steps:
        assert step == pytest.approx(STRIDE, abs=1e-6), f"motion broke at handover: {steps}"


def test_a_camera_seen_for_the_first_time_still_rebases(cfg):
    """The one case stitching cannot serve: nothing to continue the track from.

    A leader absent in the previous frame has no movement of its own to measure,
    so its parallax is genuinely unknown and the baseline has to go.
    """
    from mindcontrol.fusion import HandFusion, Observation

    from conftest import synthetic

    fusion = HandFusion(cfg.tracking, cfg.gestures)
    first = replace(synthetic(cfg.gestures, anchor=(0.3, 0.5)), score=0.9)
    fusion.fuse([Observation(0, [first], 0.0)])
    # Camera 1 arrives already leading, having never been seen before.
    arriving = replace(synthetic(cfg.gestures, anchor=(0.6, 0.5)), score=0.99)
    fused = fusion.fuse([Observation(1, [arriving], 0.0)])
    assert [hand.rebased for hand in fused] == [True]


def test_poses_survive_fusion(fused_session, cfg):
    for label, share in replay_module.accuracy(fused_session, cfg).items():
        assert share > 0.9, f"{label} only classified {share:.0%} once fused"


def test_fused_drags_still_balance(fused_session, cfg):
    counts = replay_module.replay(fused_session, cfg).counts()
    assert counts[Action.DRAG_START] == counts[Action.DRAG_END] > 0


def test_a_second_camera_does_not_double_count_an_instant(synthetic_session, fused_session, cfg):
    """Two views of one moment are one sample, not two.

    Fitting takes the best view per frame rather than every view, so adding a
    camera sharpens the measurement without inflating the apparent evidence --
    which would otherwise make a shaky fit look well supported.
    """
    from mindcontrol.autotune import analyse

    def samples(session):
        return {s.key: s.samples for s in analyse(session, cfg.gestures) if s.samples}

    single, doubled = samples(synthetic_session), samples(fused_session)
    assert set(single) == set(doubled)
    for key, count in single.items():
        assert doubled[key] == count, key


def test_a_camera_leaving_mid_session_is_survivable(flaky_session, cfg):
    """A camera that stops delivering must cost coverage, not the recording.

    Worth pinning down because a Continuity Camera genuinely does this: it sleeps
    or walks out of range partway through, and every frame after that carries one
    fewer view than the frames before it.
    """
    assert flaky_session.cameras == (0, 1, 2), "the departed camera left no trace"

    views = [len(frame.views) for frame in flaky_session.frames]
    assert views[0] == 3 and views[-1] == 2, "the dropout never happened"

    result = replay_module.replay(flaky_session, cfg)
    assert result.total(Action.CLICK, "pinch_cycle") >= 6
    assert result.total(Action.SCROLL, "fist") > 50
    assert result.total(Action.SWIPE_LEFT, "swipe") >= 1
    assert result.total(Action.SWIPE_RIGHT, "swipe") >= 1


def test_losing_a_camera_does_not_strand_a_drag(flaky_session, cfg):
    """Position follows one leading camera, so losing one could hang a button."""
    counts = replay_module.replay(flaky_session, cfg).counts()
    assert counts[Action.DRAG_START] == counts[Action.DRAG_END] > 0


def test_poses_survive_losing_a_camera(flaky_session, cfg):
    for label, share in replay_module.accuracy(flaky_session, cfg).items():
        assert share > 0.9, f"{label} only classified {share:.0%} across the dropout"


def test_thresholds_still_fit_across_a_dropout(flaky_session, fused_session, cfg):
    """Fitting must not skew because later frames had fewer viewpoints."""
    from mindcontrol.autotune import analyse

    def fitted(session):
        return {s.key: s.proposed for s in analyse(session, cfg.gestures) if s.proposed}

    steady, flaky = fitted(fused_session), fitted(flaky_session)
    assert set(flaky) == set(steady), "a dropout changed which thresholds are fittable"
    for key, value in steady.items():
        assert flaky[key] == pytest.approx(value, rel=0.25), key


def test_extra_cameras_do_not_move_the_thresholds(synthetic_session, fused_session, cfg):
    """Adding a viewpoint must sharpen a fit, not shift it.

    The regression this guards against is subtle and was live: sampling picked the
    frame's extreme across *cameras* as well as across hands, taking the lowest of
    the views for a cluster meant to be low and the highest for one meant to be
    high. That invents a gap wider than any camera measured, and the threshold
    lands in the space between viewpoints where no real sample falls. On a
    three-camera recording it put `pinch_close` below every pinch it was meant to
    catch, and claimed 0.277 of hold drift where the hand had moved 0.072.
    """
    from mindcontrol.autotune import analyse

    def fitted(session):
        return {
            s.key: s.proposed
            for s in analyse(session, cfg.gestures, cfg.tracking)
            if s.proposed is not None
        }

    single, doubled = fitted(synthetic_session), fitted(fused_session)
    assert set(single) == set(doubled)
    for key, value in single.items():
        assert doubled[key] == pytest.approx(value, rel=0.2, abs=0.05), (
            f"{key} moved from {value} to {doubled[key]} purely by adding a camera"
        )


def test_drift_is_not_measured_across_a_viewpoint_change(flaky_session, synthetic_session, cfg):
    """Parallax between cameras is not the hand wandering.

    Fused position comes from whichever camera leads, so a change of leader moves
    the anchor without the hand moving. Measured straight through, that reads as
    drift and inflates `hold_max_travel` -- which then accepts a hand that is
    plainly being waved about as one held still.
    """
    from mindcontrol.autotune import analyse

    def drift(session):
        for s in analyse(session, cfg.gestures, cfg.tracking):
            if s.key == "hold_max_travel":
                return s.proposed
        return None

    one, three = drift(synthetic_session), drift(flaky_session)
    assert one is not None and three is not None
    assert three == pytest.approx(one, rel=0.35, abs=0.02), (
        f"three cameras claimed {three} of allowable drift where one claimed {one}"
    )


def test_autotuned_thresholds_still_classify(synthetic_session, cfg):
    """Applying the fitted values must not break what already worked.

    A tuner that improves one threshold while destroying pose recognition would
    look successful on paper, so the fit is checked by replaying under it.
    """
    from dataclasses import replace

    from mindcontrol.autotune import analyse

    fitted = {s.key: s.proposed for s in analyse(synthetic_session, cfg.gestures) if s.proposed}
    tuned = replace(cfg, gestures=replace(cfg.gestures, **fitted))

    for label, share in replay_module.accuracy(synthetic_session, tuned).items():
        assert share > 0.95, f"{label} broke after autotune ({share:.0%})"
