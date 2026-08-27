"""Hand landmark tracking.

One tracker instance per camera. MediaPipe's VIDEO running mode is used rather
than LIVE_STREAM because it is synchronous -- the pipeline stays a plain loop
instead of a callback maze -- while still carrying tracking state between frames,
which is what makes landmarks stable enough to point with.
"""

from __future__ import annotations

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks import python as mp_tasks
from mediapipe.tasks.python import vision

from .. import geometry, models
from ..capture import Frame
from ..config import GestureConfig, TrackingConfig
from ..geometry import HandFeatures


def _to_array(landmarks: list) -> np.ndarray:
    return np.array([[p.x, p.y, p.z] for p in landmarks], dtype=np.float32)


class HandTracker:
    """Detects hands in a camera's frames and reports their measured features."""

    def __init__(self, cfg: TrackingConfig, gestures: GestureConfig, mirrored: bool) -> None:
        self._gestures = gestures
        self._mirrored = mirrored
        self._last_timestamp = -1
        options = vision.HandLandmarkerOptions(
            base_options=mp_tasks.BaseOptions(
                model_asset_path=str(models.ensure("hand_landmarker.task"))
            ),
            running_mode=vision.RunningMode.VIDEO,
            num_hands=cfg.max_hands,
            min_hand_detection_confidence=cfg.hand_detection_confidence,
            min_hand_presence_confidence=cfg.hand_presence_confidence,
            min_tracking_confidence=cfg.hand_tracking_confidence,
        )
        self._landmarker = vision.HandLandmarker.create_from_options(options)

    def process(self, frame: Frame) -> list[HandFeatures]:
        image = mp.Image(
            image_format=mp.ImageFormat.SRGB,
            data=cv2.cvtColor(frame.image, cv2.COLOR_BGR2RGB),
        )
        # VIDEO mode rejects a timestamp that does not advance, which two frames
        # landing in the same millisecond will do.
        timestamp = max(frame.timestamp_ms, self._last_timestamp + 1)
        self._last_timestamp = timestamp
        result = self._landmarker.detect_for_video(image, timestamp)

        hands: list[HandFeatures] = []
        for index, image_landmarks in enumerate(result.hand_landmarks):
            image_points = _to_array(image_landmarks)
            world = (
                _to_array(result.hand_world_landmarks[index])
                if index < len(result.hand_world_landmarks)
                else _aspect_corrected(image_points, frame.image.shape)
            )
            category = result.handedness[index][0]
            seen = category.category_name
            # A mirrored frame inverts what the model calls left and right, so the
            # label is flipped back to the hand you are actually holding up.
            physical = _flip(seen) if self._mirrored else seen
            hands.append(
                geometry.measure(
                    world=world,
                    image_points=image_points,
                    handedness=physical,
                    seen_handedness=seen,
                    score=float(category.score),
                    thresholds=self._gestures,
                )
            )
        return hands

    def close(self) -> None:
        self._landmarker.close()


def _flip(label: str) -> str:
    return {"Left": "Right", "Right": "Left"}.get(label, label)


def _aspect_corrected(points: np.ndarray, shape: tuple[int, ...]) -> np.ndarray:
    """Make normalised image landmarks isotropic, for when world landmarks are absent.

    Normalised x and y each span 0..1 over different pixel counts, so on a 16:9
    frame a horizontal centimetre reads smaller than a vertical one. Scaling x
    (and z, which shares x's scale) by the aspect ratio restores one common unit.
    """
    height, width = shape[0], shape[1]
    aspect = width / max(height, 1)
    scaled = points.copy()
    scaled[:, 0] *= aspect
    scaled[:, 2] *= aspect
    return scaled
