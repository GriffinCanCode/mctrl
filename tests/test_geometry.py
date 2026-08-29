"""Pose classification and the scale invariance it depends on."""

from __future__ import annotations

import numpy as np
import pytest
from conftest import build_hand, features

from mindcontrol.gestures.geometry import Pose

ALL_FINGERS = ("index", "middle", "ring", "pinky")


@pytest.mark.parametrize(
    ("curled", "thumb", "expected"),
    [
        ((), "out", Pose.OPEN_PALM),
        (ALL_FINGERS, "in", Pose.FIST),
        (("middle", "ring", "pinky"), "out", Pose.READY),
        (("index", "middle", "ring"), "out", Pose.TELEPHONE),
    ],
)
def test_poses_classify(cfg, curled, thumb, expected):
    assert features(build_hand(curled=curled, thumb=thumb), cfg.gestures).pose is expected


def test_measurements_are_scale_invariant(cfg):
    """Leaning back must not change any measurement.

    Every threshold is stated in palm units precisely so that distance to the
    camera drops out. If this fails, gestures work at one seating position and
    not another.
    """
    near = features(build_hand(curled=("middle", "ring", "pinky")), cfg.gestures)
    far = features(build_hand(curled=("middle", "ring", "pinky")) * 0.5, cfg.gestures)

    assert far.pose is near.pose
    assert far.pinch_index == pytest.approx(near.pinch_index, abs=1e-3)
    assert far.spread == pytest.approx(near.spread, abs=1e-3)
    assert far.extended == near.extended


def test_pinch_straddles_configured_thresholds(cfg):
    """A relaxed hand must read above pinch_open, a pinched one below pinch_close.

    Without this margin the hysteresis pair is meaningless: clicks would either
    never fire or never release.
    """
    relaxed = features(build_hand(curled=("middle", "ring", "pinky")), cfg.gestures)
    pinched = features(
        build_hand(curled=("middle", "ring", "pinky"), pinch_to="index"), cfg.gestures
    )

    assert relaxed.pinch_index > cfg.gestures.pinch_open
    assert pinched.pinch_index < cfg.gestures.pinch_close


def test_index_and_middle_pinches_are_distinguished(cfg):
    index = features(build_hand(curled=("middle", "ring", "pinky"), pinch_to="index"), cfg.gestures)
    middle = features(build_hand(curled=("ring", "pinky"), pinch_to="middle"), cfg.gestures)

    assert not index.pinch_is_middle
    assert middle.pinch_is_middle


def test_palm_normal_faces_camera(cfg):
    """Facing must be positive for a palm shown to the camera, either hand.

    The cross product's sign flips between hands, so a chirality mistake here
    would make the engage gesture work with one hand and not the other.
    """
    points = build_hand()
    assert features(points, cfg.gestures, handed="Right").facing > 0
    assert features(points, cfg.gestures, handed="Left").facing < 0


def test_palm_facing_survives_a_mirrored_feed(cfg):
    """A palm shown to the camera must read as facing it, mirrored or not.

    Mirroring flips the landmarks' x axis *and* flips the label MediaPipe
    reports, and those two sign changes have to cancel. If they ever stop
    cancelling, `engage` and every swipe silently stop working -- but only when
    `cameras.mirror` is on, which is the default, so this is worth pinning down.
    """
    from mindcontrol.gestures.geometry import Pose, measure

    upright = build_hand()
    mirrored = upright.copy()
    mirrored[:, 0] *= -1

    direct = measure(upright, upright, "Right", "Right", 0.95, cfg.gestures)
    flipped = measure(mirrored, mirrored, "Right", "Left", 0.95, cfg.gestures)

    assert direct.facing > 0
    assert flipped.facing > 0
    assert direct.pose is flipped.pose is Pose.OPEN_PALM
    # The physical hand is what the rest of the app reasons about, so it must
    # survive the flip rather than following what the camera happened to see.
    assert direct.handedness == flipped.handedness == "Right"


def test_palm_span_is_the_measurement_unit(cfg):
    """The declared palm size should match the modelled 9cm palm."""
    assert features(build_hand(), cfg.gestures).palm_size == pytest.approx(0.09, abs=1e-3)


def test_degenerate_landmarks_do_not_raise(cfg):
    """All-zero landmarks happen when tracking collapses; they must not crash."""
    blank = np.zeros((21, 3), dtype=np.float32)
    result = features(blank, cfg.gestures)
    assert result.pose in tuple(Pose)
    assert np.isfinite(result.pinch_index)
