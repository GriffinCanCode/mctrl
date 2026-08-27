"""Shared fixtures and synthetic hand construction.

The synthetic hands here are built to plausible human proportions -- a hand about
18cm long with a 9cm palm -- so the thresholds under test are exercised against
numbers in the same range as the real tracker produces. A hand model with
arbitrary proportions would pass or fail for reasons that say nothing about
whether the code works on a person.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from mindcontrol.config import Config, load
from mindcontrol.geometry import HandFeatures, Pose, measure
from mindcontrol.session import Session

# Landmark chains per finger: (mcp, pip, dip, tip).
CHAIN = {
    "index": (5, 6, 7, 8),
    "middle": (9, 10, 11, 12),
    "ring": (13, 14, 15, 16),
    "pinky": (17, 18, 19, 20),
}
MCP_X = {"index": -0.02, "middle": 0.0, "ring": 0.02, "pinky": 0.04}


@pytest.fixture(scope="session")
def cfg() -> Config:
    """The project's own config, so tests check the shipped thresholds."""
    return load(Path(__file__).resolve().parent.parent / "config.toml")


def write_session(
    path,
    cameras=(0,),
    degrade: float = 0.0,
    dropout: tuple[int, int] | None = None,
    keep: set[str] | None = None,
) -> Session:
    """Write a scripted session shaped exactly like `mindcontrol record` does.

    Built as a *mirrored* feed, because that is the default the app actually runs
    in: landmarks x-flipped, and labelled as the opposite hand.

    ``cameras`` records the same performance from several viewpoints, and
    ``degrade`` is how much noise and confidence to subtract from every camera
    after the first -- standing in for an off-axis camera that sees the hand worse.

    ``dropout`` is ``(camera_id, frame)``: that camera stops contributing a view
    from that frame onwards, standing in for one that leaves partway through.

    ``keep`` restricts the script to those labels, standing in for a focused
    re-record of one gesture.
    """
    import numpy as np

    from mindcontrol.session import RecordedFrame, RecordedHand, RecordedView, SessionWriter

    rng = np.random.default_rng(7)
    all_fingers = ("index", "middle", "ring", "pinky")
    poses = {
        "ready": build_hand(curled=("middle", "ring", "pinky")),
        "pinch": build_hand(curled=("middle", "ring", "pinky"), pinch_to="index"),
        "pinch_mid": build_hand(curled=("ring", "pinky"), pinch_to="middle"),
        "fist": build_hand(curled=all_fingers, thumb="in"),
        "palm": build_hand(),
        "phone": build_hand(curled=("index", "middle", "ring")),
    }

    def sample(points, jitter, shift, extra):
        world = points.copy()
        world[:, 0] *= -1
        world += rng.normal(0, jitter + extra, world.shape).astype(np.float32)
        image = world.copy()
        image[:, 0] = 0.5 + shift[0] + world[:, 0] * 2
        image[:, 1] = 0.5 + shift[1] - world[:, 1] * 2
        return RecordedHand("Right", "Left", 0.95 - extra * 10, world, image)

    clock = {"t": 0.0, "n": 0}
    frame_time = 1 / 30.0

    with SessionWriter(path, note="synthetic") as writer:

        def emit(label, key, count, jitter=0.003, motion=None):
            if keep is not None and label not in keep:
                return
            for step in range(count):
                shift = motion(step) if motion else (0.0, 0.0)
                views: list = [
                    RecordedView(
                        camera_id=camera_id,
                        # Secondary cameras lag by a frame, as real ones do.
                        age_ms=0.0 if position == 0 else 12.0,
                        hands=[]
                        if key is None
                        else [
                            sample(poses[key], jitter, shift, 0.0 if position == 0 else degrade)
                        ],
                    )
                    for position, camera_id in enumerate(cameras)
                ]
                if dropout is not None and clock["n"] >= dropout[1]:
                    views = [v for v in views if v.camera_id != dropout[0]]
                writer.add(RecordedFrame(clock["t"], label, views))
                clock["t"] += frame_time
                clock["n"] += 1

        emit("none", None, 90)
        emit("ready", "ready", 150, motion=lambda i: (i * 0.001, 0.0))
        emit("pinch_closed", "pinch", 150)
        # Eight deliberate taps: open for most of a second, briefly shut.
        for _ in range(8):
            emit("pinch_cycle", "ready", 22)
            emit("pinch_cycle", "pinch", 6)
        emit("pinch_middle_closed", "pinch_mid", 150)
        emit("fist", "fist", 120, motion=lambda i: (0.0, i * 0.004))
        emit("open_palm", "palm", 150, jitter=0.0015)
        emit("telephone", "phone", 120)
        for _ in range(4):
            emit("swipe", "palm", 12, motion=lambda i: (-0.35 + i * 0.06, 0.0))
            emit("swipe", "palm", 12, motion=lambda i: (0.35 - i * 0.06, 0.0))

    return Session.load(path)


@pytest.fixture(scope="session")
def synthetic_session(tmp_path_factory) -> Session:
    """A one-camera scripted session.

    Real recordings test whether the thresholds suit a particular person; this
    tests the machinery that carries them -- session I/O, re-measurement, and the
    state machine over a realistic sequence -- deterministically, on any machine,
    with no camera and no hands.
    """
    return write_session(tmp_path_factory.mktemp("session") / "one-camera.jsonl")


@pytest.fixture(scope="session")
def fused_session(tmp_path_factory) -> Session:
    """The same performance seen by two cameras, the second noticeably worse.

    This is the only way the fusion path gets tested honestly: two viewpoints that
    genuinely disagree about one hand. Feeding the same image twice would exercise
    the code while proving nothing about the merge.
    """
    return write_session(
        tmp_path_factory.mktemp("session") / "two-camera.jsonl",
        cameras=(0, 1),
        degrade=0.004,
    )


@pytest.fixture(scope="session")
def flaky_session(tmp_path_factory) -> Session:
    """Three cameras, the third leaving partway through and never returning.

    A phone joined over Continuity does exactly this: it sleeps, or walks out of
    range, and simply stops delivering frames. It drops during `fist`, so the loss
    lands in the middle of a labelled pose rather than tidily between two.
    """
    return write_session(
        tmp_path_factory.mktemp("session") / "three-camera.jsonl",
        cameras=(0, 1, 2),
        degrade=0.004,
        dropout=(2, 700),
    )


def build_hand(curled=(), thumb="out", pinch_to=None) -> np.ndarray:
    """A 21-point hand in metres, with the named fingers folded in."""
    points = np.zeros((21, 3), dtype=np.float32)
    points[0] = (0.0, 0.0, 0.0)
    for name, (mcp, pip, dip, tip) in CHAIN.items():
        x = MCP_X[name]
        points[mcp] = (x, 0.09, 0.0)
        if name in curled:
            points[pip], points[dip], points[tip] = (
                (x, 0.115, 0.01),
                (x, 0.10, 0.03),
                (x, 0.075, 0.035),
            )
        else:
            points[pip], points[dip], points[tip] = (
                (x, 0.13, 0.0),
                (x, 0.16, 0.0),
                (x, 0.18, 0.0),
            )
    points[1] = (-0.03, 0.03, 0.0)
    if thumb == "out":
        points[2], points[3], points[4] = (
            (-0.05, 0.04, 0.0),
            (-0.07, 0.045, 0.0),
            (-0.09, 0.05, 0.0),
        )
    else:
        points[2], points[3], points[4] = (
            (-0.02, 0.045, 0.0),
            (-0.01, 0.055, 0.0),
            (0.0, 0.06, 0.0),
        )
    if pinch_to is not None:
        points[4] = points[CHAIN[pinch_to][3]] + np.array([0.0, 0.0, 0.004], dtype=np.float32)
    return points


def features(points: np.ndarray, gestures, handed: str = "Right") -> HandFeatures:
    return measure(points, points, handed, handed, 0.95, gestures)


def synthetic(
    gestures,
    pose: Pose = Pose.READY,
    anchor: tuple[float, float] = (0.5, 0.5),
    pinch: float = 0.9,
    pinch_middle: float = 0.9,
    handed: str = "Right",
) -> HandFeatures:
    """A features object with fields set directly, for driving the state machine.

    The state machine consumes measurements, not landmarks, so its tests are
    clearer and far more precise when the measurements are stated outright rather
    than reverse-engineered from a pose.
    """
    blank = np.zeros((21, 3), dtype=np.float32)
    return HandFeatures(
        handedness=handed,
        score=0.95,
        pose=pose,
        anchor=anchor,
        palm_size=0.09,
        pinch_index=pinch,
        pinch_middle=pinch_middle,
        extended=(True, True, False, False, False),
        spread=0.3,
        facing=0.5,
        landmarks=blank,
        world=blank,
        seen_handedness=handed,
    )
