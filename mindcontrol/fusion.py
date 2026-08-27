"""Meshing several cameras into one view of your hands.

The two halves of a hand observation are merged differently, because they mean
different things across viewpoints.

*Shape* -- pinch distances, which fingers are out -- is scale invariant and
comparable between cameras, so it is combined by confidence-weighted vote. This
is the real payoff of a second camera: a pinch hidden behind your palm from the
laptop is plainly visible from the side, and either camera can carry the gesture.

*Position* is not comparable. Each camera has its own viewpoint, so the same hand
sits at different normalised coordinates in each. Averaging them would invent a
location belonging to no camera and lurch whenever one dropped out. Instead one
camera *leads* for position, chosen by confidence and held onto until it is
clearly beaten, and a change of leader is reported so the pointer can rebase
instead of jumping.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .config import GestureConfig, TrackingConfig
from .geometry import HandFeatures, classify
from .tracking.gaze import GazeObservation

if TYPE_CHECKING:
    from .session import RecordedFrame, Session



@dataclass(frozen=True)
class Observation:
    """One camera's hands, with the age of the frame they came from."""

    camera_id: int
    hands: list[HandFeatures]
    age_ms: float


@dataclass(frozen=True)
class FusedHand:
    features: HandFeatures
    camera_id: int
    cameras: tuple[int, ...]
    rebased: bool

    @property
    def merged(self) -> bool:
        return len(self.cameras) > 1


class HandFusion:
    """Combines per-camera hand observations into one hand per side."""

    def __init__(self, tracking: TrackingConfig, gestures: GestureConfig) -> None:
        self._tracking = tracking
        self._gestures = gestures
        self._leader: dict[str, int] = {}
        # State for stitching position across a change of leading camera. Per side:
        # the offset currently mapping the leader's coordinates into the continuous
        # track, the last anchor emitted, and the last anchor each camera reported.
        self._offset: dict[str, tuple[float, float]] = {}
        self._emitted: dict[str, tuple[float, float]] = {}
        self._seen: dict[tuple[str, int], tuple[float, float]] = {}

    def fuse(self, observations: list[Observation]) -> list[FusedHand]:
        """Merge observations, newest-first, dropping stale frames."""
        by_side: dict[str, list[tuple[int, HandFeatures]]] = {}
        for observation in observations:
            if observation.age_ms > self._tracking.stale_after_ms:
                continue
            for hand in observation.hands:
                by_side.setdefault(hand.handedness, []).append((observation.camera_id, hand))

        return [self._fuse_side(side, entries) for side, entries in by_side.items()]

    def _fuse_side(self, side: str, entries: list[tuple[int, HandFeatures]]) -> FusedHand:
        cameras = tuple(sorted(camera_id for camera_id, _ in entries))
        leader_id, leader = self._pick_leader(side, entries)
        rebased = self._stitch(side, leader_id, leader.anchor)
        self._leader[side] = leader_id

        features = leader if len(entries) == 1 else self._blend(entries, leader)
        anchor, offset = features.anchor, self._offset[side]
        if offset != (0.0, 0.0):
            features = replace(features, anchor=(anchor[0] + offset[0], anchor[1] + offset[1]))

        self._emitted[side] = features.anchor
        for camera_id, hand in entries:
            self._seen[side, camera_id] = hand.anchor
        return FusedHand(features, leader_id, cameras, rebased)

    def _stitch(self, side: str, leader_id: int, anchor: tuple[float, float]) -> bool:
        """Absorb a change of leader into an offset, and say whether that failed.

        Two cameras looking at one hand disagree about where it is, so handing the
        lead over moves the anchor by the parallax between them. That used to be
        dealt with by telling the pointer to forget its baseline, which stops the
        cursor flinging but throws away the frame -- and a fast gesture crossing
        between views spends a third of its frames doing exactly that, which is how
        a real sweep changed leader 27 times and registered one swipe out of four.

        The new leader has usually been watching all along, so its *own* movement
        since the previous frame is known and is a faithful measure of the hand's.
        Choosing an offset that continues the track from there keeps position
        continuous while spending none of the motion:

            emitted = leader.anchor + offset,  offset = last_emitted - leader.previous

        The offset then stays put until the next handover, so between them the
        motion is exactly the leader's own. Only a leader that was not in the
        previous frame -- one that just appeared -- has nothing to continue from,
        and that alone still needs a rebase.
        """
        previous = self._leader.get(side)
        if previous == leader_id:
            return False

        here = self._seen.get((side, leader_id))
        emitted = self._emitted.get(side)
        if previous is None or here is None or emitted is None:
            self._offset[side] = (0.0, 0.0)
            # A first sighting is not a jump; there is no baseline to invalidate.
            return previous is not None

        self._offset[side] = (emitted[0] - here[0], emitted[1] - here[1])
        return False

    def _pick_leader(
        self, side: str, entries: list[tuple[int, HandFeatures]]
    ) -> tuple[int, HandFeatures]:
        """Best camera for position, with hysteresis so it does not flip-flop.

        The margin used to carry more weight than it should have, because every
        handover discarded a frame of motion; `_stitch` now absorbs them, so this
        only keeps the lead from flitting between views of near-equal confidence.
        """
        best_id, best = max(entries, key=lambda item: item[1].score)
        held = self._leader.get(side)
        if held is None:
            return best_id, best
        margin = self._tracking.leader_margin
        for camera_id, hand in entries:
            if camera_id == held and hand.score + margin >= best.score:
                return camera_id, hand
        return best_id, best

    def _blend(self, entries: list[tuple[int, HandFeatures]], leader: HandFeatures) -> HandFeatures:
        """Average shape across cameras, keeping the leader's position."""
        weights = [max(hand.score, 1e-3) for _, hand in entries]
        total = sum(weights)
        hands = [hand for _, hand in entries]

        def weighted(pick) -> float:
            pairs = zip(weights, hands, strict=True)
            return sum(weight * pick(hand) for weight, hand in pairs) / total

        # A finger counts as extended when the cameras that can see it agree by
        # weight; occlusion in one view is outvoted rather than trusted.
        flags = tuple(
            sum(weight for weight, hand in zip(weights, hands, strict=True) if hand.extended[index])
            > total / 2.0
            for index in range(5)
        )
        spread = weighted(lambda h: h.spread)
        facing = weighted(lambda h: h.facing)

        return replace(
            leader,
            pinch_index=weighted(lambda h: h.pinch_index),
            pinch_middle=weighted(lambda h: h.pinch_middle),
            extended=flags,  # type: ignore[arg-type]
            spread=spread,
            facing=facing,
            score=max(hand.score for hand in hands),
            pose=classify(flags, spread, facing, self._gestures),
        )

    def reset(self) -> None:
        """Forget everything, including the stitched track.

        Clearing the offset here is what bounds its drift: it only accumulates
        while one hand stays continuously in view, and each camera measures motion
        in its own field of view, so the scales are not identical.
        """
        self._leader.clear()
        self._offset.clear()
        self._emitted.clear()
        self._seen.clear()


def fuse_session(
    session: Session, gestures: GestureConfig, tracking: TrackingConfig | None = None
) -> Iterator[tuple[RecordedFrame, list[FusedHand]]]:
    """Push a recording through fusion, yielding each frame's merged hands.

    Tuning and reporting both want the hand the *engine* sees. Reading the raw
    per-camera views instead skews everything downstream, and not by a little:
    taking the lowest value across three viewpoints for a cluster meant to be low,
    and the highest for one meant to be high, manufactures a separation that no
    single camera ever saw. A threshold fitted to that invented gap lands in the
    space between the cameras, where no real measurement falls.

    Frames are walked in order because fusion carries state -- which camera
    currently leads position, and the hysteresis holding it there.
    """
    fusion = HandFusion(tracking or TrackingConfig(), gestures)
    for frame in session.frames:
        yield (
            frame,
            fusion.fuse(
                [
                    Observation(
                        camera_id=view.camera_id,
                        hands=[hand.remeasure(gestures) for hand in view.hands],
                        age_ms=view.age_ms,
                    )
                    for view in frame.views
                ]
            ),
        )


def fuse_gaze(observations: dict[int, GazeObservation], primary: int) -> GazeObservation:
    """Prefer the camera designated for gaze; fall back to any that sees a face.

    Gaze normally runs on one camera only -- it is the expensive model, and only
    a camera near the screen you look at can produce a useful answer -- but the
    fallback keeps gaze alive if that camera is unplugged.
    """
    preferred = observations.get(primary)
    if preferred is not None and preferred.usable:
        return preferred
    for observation in observations.values():
        if observation.usable:
            return observation
    return preferred or GazeObservation(present=False)
