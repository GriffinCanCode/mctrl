"""Hand-shape features and pose classification.

Everything here is scale invariant: raw landmark distances shrink as you lean
back from the camera, so each measurement is divided by the palm span
(wrist to middle knuckle). A pinch is then "0.3 palms" whether you are at the
keyboard or across the room.

Landmark ordering is MediaPipe's 21-point hand model.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from itertools import pairwise

import numpy as np

WRIST = 0
THUMB_MCP, THUMB_IP, THUMB_TIP = 2, 3, 4
INDEX_MCP, INDEX_PIP, INDEX_TIP = 5, 6, 8
MIDDLE_MCP, MIDDLE_PIP, MIDDLE_TIP = 9, 10, 12
RING_MCP, RING_PIP, RING_TIP = 13, 14, 16
PINKY_MCP, PINKY_PIP, PINKY_TIP = 17, 18, 20

PALM_POINTS = (WRIST, INDEX_MCP, MIDDLE_MCP, RING_MCP, PINKY_MCP)
# (tip, pip) pairs for the four non-thumb fingers, in finger order.
FINGERS = (
    (INDEX_TIP, INDEX_PIP),
    (MIDDLE_TIP, MIDDLE_PIP),
    (RING_TIP, RING_PIP),
    (PINKY_TIP, PINKY_PIP),
)
TIPS = (INDEX_TIP, MIDDLE_TIP, RING_TIP, PINKY_TIP)

# Drawing skeleton: pairs of landmark indices connected by a bone, one row per
# finger, then the strap across the base of the palm.
# fmt: off
SKELETON = (
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12),
    (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (17, 18), (18, 19), (19, 20),
    (0, 17),
)
# fmt: on


class Pose(Enum):
    """Coarse hand shape, evaluated fresh on every frame."""

    NONE = "none"
    OTHER = "other"
    READY = "ready"
    FIST = "fist"
    OPEN_PALM = "open_palm"
    TELEPHONE = "telephone"


@dataclass(frozen=True)
class HandFeatures:
    """Scale-invariant measurements of one hand, plus its classified pose."""

    handedness: str
    score: float
    pose: Pose
    anchor: tuple[float, float]
    palm_size: float
    pinch_index: float
    pinch_middle: float
    extended: tuple[bool, bool, bool, bool, bool]
    spread: float
    facing: float
    landmarks: np.ndarray
    # The metric landmarks every measurement above was derived from. Kept so a
    # recorded session can be re-measured under different thresholds; without it
    # a recording would only ever prove the thresholds it was captured with.
    world: np.ndarray
    seen_handedness: str = ""

    @property
    def pinch(self) -> float:
        """Distance of whichever pinch is closest to closing."""
        return min(self.pinch_index, self.pinch_middle)

    @property
    def pinch_is_middle(self) -> bool:
        """True when the thumb is meeting the middle finger, not the index."""
        return self.pinch_middle < self.pinch_index


def _norm(vector: np.ndarray) -> float:
    return float(np.linalg.norm(vector))


def palm_span(points: np.ndarray) -> float:
    """Wrist-to-middle-knuckle distance: the unit every other measure divides by."""
    return max(_norm(points[MIDDLE_MCP] - points[WRIST]), 1e-6)


def palm_normal(points: np.ndarray, handedness: str) -> float:
    """Signed z of the palm normal; positive means the palm faces the camera.

    MediaPipe's z axis grows away from the camera, and the cross product's sign
    flips between hands, so both are corrected here.
    """
    edge_a = points[INDEX_MCP] - points[WRIST]
    edge_b = points[PINKY_MCP] - points[WRIST]
    normal = np.cross(edge_a, edge_b)
    magnitude = _norm(normal)
    if magnitude < 1e-9:
        return 0.0
    chirality = 1.0 if handedness.lower().startswith("r") else -1.0
    return float(-normal[2] / magnitude) * chirality


def measure(
    world: np.ndarray,
    image_points: np.ndarray,
    handedness: str,
    seen_handedness: str,
    score: float,
    thresholds: object,
) -> HandFeatures:
    """Turn 21 landmarks into features and a pose label.

    Shape is measured from ``world`` (MediaPipe's metric landmarks) because those
    axes share one scale; normalised image coordinates do not, and a pinch judged
    in them would drift with the frame's aspect ratio. Position comes from
    ``image_points``, which is what the pointer actually needs.

    ``thresholds`` is a ``GestureConfig``, passed in rather than imported so the
    classifier stays tunable from ``config.toml`` at runtime.
    """
    points = world
    span = palm_span(points)
    wrist = points[WRIST]

    # A finger is extended when its tip sits farther from the wrist than its
    # middle joint. Comparing against the joint instead of a fixed length keeps
    # the test honest for the short pinky and the long middle finger alike.
    extended: list[bool] = []
    for tip, pip in FINGERS:
        pip_reach = max(_norm(points[pip] - wrist), 1e-6)
        extended.append(_norm(points[tip] - wrist) / pip_reach > thresholds.finger_extended)

    # The thumb rotates rather than curls, so it is measured by how far its tip
    # has swung away from the far edge of the palm.
    thumb_out = _norm(points[THUMB_TIP] - points[PINKY_MCP]) / span > thresholds.thumb_extended
    flags = (thumb_out, *extended)

    spread = float(np.mean([_norm(points[a] - points[b]) for a, b in pairwise(TIPS)]) / span)
    # Chirality follows the hand as the camera sees it, which is what the cross
    # product is computed from; a mirrored frame flips that but not the physical
    # label carried in `handedness`.
    facing = palm_normal(points, seen_handedness)
    # The palm centre is the anchor rather than a fingertip: it barely moves when
    # you pinch, so clicking does not shove the cursor off target.
    anchor_point = image_points[list(PALM_POINTS)].mean(axis=0)

    return HandFeatures(
        handedness=handedness,
        score=score,
        pose=classify(flags, spread, facing, thresholds),
        anchor=(float(anchor_point[0]), float(anchor_point[1])),
        palm_size=span,
        pinch_index=_norm(points[THUMB_TIP] - points[INDEX_TIP]) / span,
        pinch_middle=_norm(points[THUMB_TIP] - points[MIDDLE_TIP]) / span,
        extended=flags,
        spread=spread,
        facing=facing,
        landmarks=image_points,
        world=world,
        seen_handedness=seen_handedness,
    )


def classify(flags: tuple[bool, ...], spread: float, facing: float, thresholds: object) -> Pose:
    """Label a hand shape. Order matters: the specific poses are tested first."""
    thumb, index, middle, ring, pinky = flags

    if index and middle and ring and pinky:
        # An open hand only means "open palm" when it is shown to the camera;
        # otherwise it is just a hand that happens to be relaxed.
        if spread >= thresholds.palm_spread and facing >= thresholds.palm_facing:
            return Pose.OPEN_PALM
        return Pose.OTHER

    if thumb and pinky and not index and not middle and not ring:
        return Pose.TELEPHONE

    if not any((thumb, index, middle, ring, pinky)):
        return Pose.FIST

    # Thumb and index available to pinch, with the hand otherwise relaxed.
    if index and thumb and not (ring and pinky):
        return Pose.READY

    return Pose.OTHER
