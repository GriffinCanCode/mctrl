"""Eye tracking and the gaze-to-screen model.

Where you are looking is not directly observable from a webcam, so it is
*learned*: calibration shows you nine dots, records what your eyes and head look
like for each, and fits a small regression from those features to screen
coordinates. Inference then runs that regression per frame.

The feature vector is built in one place, `feature_vector`, and used by both
calibration and inference. If the two ever built it differently the model would
silently predict nonsense, so there is deliberately only one definition.
"""

from __future__ import annotations

import json
import math
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks import python as mp_tasks
from mediapipe.tasks.python import vision

from .. import models
from ..capture import Frame
from ..config import TrackingConfig

# Indices into MediaPipe's 478-point face mesh (the last ten are the irises).
RIGHT_EYE_OUTER, RIGHT_EYE_INNER = 33, 133
LEFT_EYE_INNER, LEFT_EYE_OUTER = 362, 263
RIGHT_IRIS, LEFT_IRIS = 468, 473
RIGHT_LID_TOP, RIGHT_LID_BOTTOM = 159, 145
LEFT_LID_TOP, LEFT_LID_BOTTOM = 386, 374
IRIS_MESH_POINTS = 478

FEATURE_NAMES = (
    "bias",
    "iris_x",
    "iris_y",
    "head_x",
    "head_y",
    "iris_x^2",
    "iris_y^2",
    "iris_x*iris_y",
    "iris_x*head_x",
    "iris_y*head_y",
)
FEATURE_COUNT = len(FEATURE_NAMES)


def feature_vector(iris: tuple[float, float], head: tuple[float, float]) -> np.ndarray:
    """Build the regression input from eye offset and head rotation.

    Quadratic and cross terms are included because the mapping is not linear:
    the same eye offset lands somewhere different depending on how your head is
    turned, and screen edges compress relative to the centre.
    """
    ix, iy = iris
    hx, hy = head
    return np.array(
        [1.0, ix, iy, hx, hy, ix * ix, iy * iy, ix * iy, ix * hx, iy * hy],
        dtype=np.float64,
    )


@dataclass(frozen=True)
class GazeObservation:
    """What one frame tells us about the eyes."""

    present: bool
    features: np.ndarray | None = None
    openness: float = 0.0
    iris_points: tuple[tuple[float, float], ...] = ()

    @property
    def usable(self) -> bool:
        return self.present and self.features is not None


class GazeTracker:
    """Extracts gaze features from the camera trusted for gaze."""

    def __init__(self, cfg: TrackingConfig) -> None:
        self._last_timestamp = -1
        self._warned_no_iris = False
        options = vision.FaceLandmarkerOptions(
            base_options=mp_tasks.BaseOptions(
                model_asset_path=str(models.ensure("face_landmarker.task"))
            ),
            running_mode=vision.RunningMode.VIDEO,
            num_faces=1,
            min_face_detection_confidence=cfg.face_detection_confidence,
            min_tracking_confidence=cfg.hand_tracking_confidence,
            # The head's own rotation is half of where you are looking.
            output_facial_transformation_matrixes=True,
        )
        self._landmarker = vision.FaceLandmarker.create_from_options(options)

    def process(self, frame: Frame) -> GazeObservation:
        image = mp.Image(
            image_format=mp.ImageFormat.SRGB,
            data=cv2.cvtColor(frame.image, cv2.COLOR_BGR2RGB),
        )
        timestamp = max(frame.timestamp_ms, self._last_timestamp + 1)
        self._last_timestamp = timestamp
        result = self._landmarker.detect_for_video(image, timestamp)
        if not result.face_landmarks:
            return GazeObservation(present=False)

        landmarks = result.face_landmarks[0]
        points = np.array([[p.x, p.y] for p in landmarks], dtype=np.float64)
        has_iris = len(landmarks) >= IRIS_MESH_POINTS
        if not has_iris and not self._warned_no_iris:
            print("[gaze] face model returned no iris points; falling back to head-only gaze")
            self._warned_no_iris = True

        right = _eye_offset(points, RIGHT_EYE_OUTER, RIGHT_EYE_INNER, RIGHT_IRIS, has_iris)
        left = _eye_offset(points, LEFT_EYE_OUTER, LEFT_EYE_INNER, LEFT_IRIS, has_iris)
        iris = ((right[0] + left[0]) / 2.0, (right[1] + left[1]) / 2.0)

        head = _head_rotation(result.facial_transformation_matrixes)
        openness = min(
            _aspect(points, RIGHT_LID_TOP, RIGHT_LID_BOTTOM, RIGHT_EYE_OUTER, RIGHT_EYE_INNER),
            _aspect(points, LEFT_LID_TOP, LEFT_LID_BOTTOM, LEFT_EYE_OUTER, LEFT_EYE_INNER),
        )
        iris_points = (tuple(points[RIGHT_IRIS]), tuple(points[LEFT_IRIS])) if has_iris else ()
        return GazeObservation(
            present=True,
            features=feature_vector(iris, head),
            openness=openness,
            iris_points=iris_points,  # type: ignore[arg-type]
        )

    def close(self) -> None:
        self._landmarker.close()


def _eye_offset(
    points: np.ndarray, outer: int, inner: int, iris: int, has_iris: bool
) -> tuple[float, float]:
    """Iris displacement from the eye's centre, in units of eye width.

    Dividing by eye width is what makes this survive leaning toward or away from
    the camera: the eye shrinks in the image but the ratio holds.
    """
    corner_a, corner_b = points[outer], points[inner]
    width = max(float(np.linalg.norm(corner_a - corner_b)), 1e-6)
    centre = (corner_a + corner_b) / 2.0
    if not has_iris:
        return 0.0, 0.0
    offset = (points[iris] - centre) / width
    return float(offset[0]), float(offset[1])


def _head_rotation(matrices: list) -> tuple[float, float]:
    """Head yaw and pitch in radians, from the face transformation matrix."""
    if not matrices:
        return 0.0, 0.0
    rotation = np.asarray(matrices[0])[:3, :3]
    magnitude = math.hypot(rotation[0, 0], rotation[1, 0])
    if magnitude < 1e-6:
        return 0.0, 0.0
    yaw = math.atan2(-rotation[2, 0], magnitude)
    pitch = math.atan2(rotation[2, 1], rotation[2, 2])
    return yaw, pitch


def _aspect(points: np.ndarray, top: int, bottom: int, outer: int, inner: int) -> float:
    """Eye aspect ratio: lid separation over eye width. Collapses toward 0 on a blink."""
    width = max(float(np.linalg.norm(points[outer] - points[inner])), 1e-6)
    return float(np.linalg.norm(points[top] - points[bottom])) / width


class GazeModel:
    """Ridge regression from gaze features to a point on screen."""

    def __init__(self, weights: np.ndarray | None = None, quality: float = 0.0) -> None:
        self.weights = weights
        self.quality = quality

    @property
    def ready(self) -> bool:
        return self.weights is not None

    @classmethod
    def fit(cls, features: np.ndarray, targets: np.ndarray, ridge: float = 1e-3) -> GazeModel:
        """Solve for the feature-to-screen mapping.

        Ridge rather than plain least squares: nine calibration points give
        nearly collinear features, and unregularised weights would blow up and
        fling the cursor off screen.
        """
        gram = features.T @ features + ridge * np.eye(features.shape[1])
        weights = np.linalg.solve(gram, features.T @ targets)
        residual = features @ weights - targets
        error = float(np.sqrt(np.mean(np.sum(residual**2, axis=1))))
        return cls(weights=weights, quality=error)

    def predict(self, features: np.ndarray) -> tuple[float, float]:
        """Screen position as fractions of width and height, clamped on screen."""
        if self.weights is None:
            raise RuntimeError("gaze model is not calibrated")
        point = features @ self.weights
        return float(np.clip(point[0], 0.0, 1.0)), float(np.clip(point[1], 0.0, 1.0))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        assert self.weights is not None
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "features": list(FEATURE_NAMES),
                    "weights": self.weights.tolist(),
                    "rms_error": self.quality,
                    "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                },
                indent=2,
            )
        )

    @classmethod
    def load(cls, path: Path) -> GazeModel:
        """Load a saved model, or an uncalibrated one if it is missing or stale."""
        if not path.is_file():
            return cls()
        try:
            data = json.loads(path.read_text())
            weights = np.array(data["weights"], dtype=np.float64)
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            print(f"[gaze] ignoring unreadable calibration at {path}: {exc}")
            return cls()
        if weights.shape != (FEATURE_COUNT, 2):
            print("[gaze] calibration was built for a different feature set; recalibrate")
            return cls()
        return cls(weights=weights, quality=float(data.get("rms_error", 0.0)))


class FixationDetector:
    """Reports where gaze has settled, ignoring the constant flicker of saccades.

    A cursor that chased raw gaze would be unusable, since the eye never truly
    holds still. Only once the recent samples all fall inside a small radius is
    the gaze treated as a deliberate target.
    """

    def __init__(self, window_ms: float, radius: float) -> None:
        self._window_s = window_ms / 1000.0
        self._radius = radius
        self._samples: deque[tuple[float, float, float]] = deque()

    def update(self, x: float, y: float, now: float | None = None) -> tuple[float, float] | None:
        now = time.monotonic() if now is None else now
        self._samples.append((now, x, y))
        cutoff = now - self._window_s
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        if len(self._samples) < 3 or self._samples[0][0] > cutoff + self._window_s:
            return None

        points = np.array([[sx, sy] for _, sx, sy in self._samples])
        centre = points.mean(axis=0)
        if float(np.max(np.linalg.norm(points - centre, axis=1))) > self._radius:
            return None
        return float(centre[0]), float(centre[1])

    def reset(self) -> None:
        self._samples.clear()
