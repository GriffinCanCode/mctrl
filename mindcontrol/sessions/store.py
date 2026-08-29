"""Recorded gesture sessions.

A session is a labelled capture of real hands: for every frame, the raw landmarks
plus the pose the user was *asked* to hold. That pairing is what makes the file
useful twice over.

Raw landmarks rather than finished measurements, because a recording of derived
numbers could only ever validate the thresholds it was captured with. Storing the
landmarks means any threshold can be re-evaluated against the same hands later --
which is exactly what `autotune` and the replay tests do.

The prompted label is the supervision signal. Knowing that these 150 frames were
meant to be a fist is what turns "here is a distribution of finger curl" into
"here is where the boundary between curled and extended belongs".

Format is JSON Lines: one self-describing header, then one object per frame, so a
session can be appended to as it is captured and read back in a streaming pass.

A frame holds one *view* per camera rather than a flat list of hands, mirroring
the `Observation` list the live pipeline fuses. Recording each camera separately
is what lets a replay exercise the fusion path; flattening them here would throw
away the disagreement between viewpoints that fusion exists to resolve.

Older single-camera recordings (format 1) are read as one view, because a
recording costs somebody a minute of holding poses and should not be invalidated
by a change to how the file is arranged.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..config import STATE_DIR
from ..gestures.geometry import HandFeatures, measure

SESSIONS_DIR = STATE_DIR / "sessions"
FORMAT_VERSION = 2


@dataclass(frozen=True)
class RecordedHand:
    """One hand in one frame, as landmarks."""

    handedness: str
    seen_handedness: str
    score: float
    world: np.ndarray
    image: np.ndarray

    def to_json(self) -> dict:
        return {
            "handedness": self.handedness,
            "seen": self.seen_handedness,
            "score": round(self.score, 4),
            # Rounded to keep sessions readable and small; far finer than the
            # tracker's own noise floor, so nothing measurable is lost.
            "world": np.round(self.world, 5).tolist(),
            "image": np.round(self.image, 5).tolist(),
        }

    @classmethod
    def from_json(cls, data: dict) -> RecordedHand:
        return cls(
            handedness=data["handedness"],
            seen_handedness=data.get("seen", data["handedness"]),
            score=float(data["score"]),
            world=np.array(data["world"], dtype=np.float32),
            image=np.array(data["image"], dtype=np.float32),
        )

    def remeasure(self, thresholds) -> HandFeatures:
        """Re-derive features under a given `GestureConfig`."""
        return measure(
            world=self.world,
            image_points=self.image,
            handedness=self.handedness,
            seen_handedness=self.seen_handedness,
            score=self.score,
            thresholds=thresholds,
        )


@dataclass(frozen=True)
class RecordedView:
    """What one camera saw at one instant.

    ``age_ms`` is how far behind the newest camera this view was, which is what
    fusion uses to decide how much to trust it.
    """

    camera_id: int
    hands: list[RecordedHand] = field(default_factory=list)
    age_ms: float = 0.0

    def to_json(self) -> dict:
        return {
            "camera": self.camera_id,
            "age": round(self.age_ms, 2),
            "hands": [hand.to_json() for hand in self.hands],
        }

    @classmethod
    def from_json(cls, data: dict) -> RecordedView:
        return cls(
            camera_id=int(data["camera"]),
            hands=[RecordedHand.from_json(h) for h in data.get("hands", [])],
            age_ms=float(data.get("age", 0.0)),
        )


@dataclass(frozen=True)
class RecordedFrame:
    """One instant, across every camera, with the pose that was being asked for."""

    time: float
    label: str
    views: list[RecordedView] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "t": round(self.time, 4),
            "label": self.label,
            "views": [view.to_json() for view in self.views],
        }

    @classmethod
    def from_json(cls, data: dict) -> RecordedFrame:
        if "views" in data:
            views = [RecordedView.from_json(v) for v in data["views"]]
        else:
            # Format 1 stored a single camera's hands directly on the frame.
            # Reading it as one view keeps existing recordings usable, which
            # matters because a recording is a minute of somebody's time.
            views = [
                RecordedView(
                    camera_id=int(data.get("camera", 0)),
                    hands=[RecordedHand.from_json(h) for h in data.get("hands", [])],
                )
            ]
        return cls(time=float(data["t"]), label=data["label"], views=views)

    @property
    def hands(self) -> list[RecordedHand]:
        """Every hand seen by every camera, for analysis that ignores viewpoint."""
        return [hand for view in self.views for hand in view.hands]

    @property
    def cameras(self) -> tuple[int, ...]:
        return tuple(view.camera_id for view in self.views)


class SessionWriter:
    """Appends frames to a session file as they are captured."""

    def __init__(self, path: Path, note: str = "") -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("w")
        self._count = 0
        self._write(
            {
                "format": FORMAT_VERSION,
                "kind": "mindcontrol-session",
                "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "note": note,
            }
        )

    def _write(self, payload: dict) -> None:
        self._handle.write(json.dumps(payload) + "\n")

    def add(self, frame: RecordedFrame) -> None:
        self._write(frame.to_json())
        self._count += 1

    @property
    def frames(self) -> int:
        return self._count

    def close(self) -> None:
        self._handle.flush()
        self._handle.close()

    def __enter__(self) -> SessionWriter:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


@dataclass
class Session:
    """A whole recording, in memory."""

    frames: list[RecordedFrame]
    header: dict = field(default_factory=dict)
    path: Path | None = None

    @classmethod
    def load(cls, path: Path) -> Session:
        header: dict = {}
        frames: list[RecordedFrame] = []
        with path.open() as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                if data.get("kind") == "mindcontrol-session":
                    header = data
                    continue
                frames.append(RecordedFrame.from_json(data))
        version = int(header.get("format", FORMAT_VERSION))
        if version > FORMAT_VERSION:
            raise ValueError(
                f"{path} is format {version}, newer than this build understands "
                f"({FORMAT_VERSION}); upgrade mindcontrol"
            )
        return cls(frames=frames, header=header, path=path)

    def labels(self) -> list[str]:
        """Distinct labels, in the order they were recorded."""
        seen: dict[str, None] = {}
        for frame in self.frames:
            seen.setdefault(frame.label, None)
        return list(seen)

    def segment(self, *labels: str) -> Iterator[RecordedFrame]:
        """Frames recorded under any of the given labels."""
        wanted = set(labels)
        for frame in self.frames:
            if frame.label in wanted:
                yield frame

    def hands(self, *labels: str) -> Iterator[RecordedHand]:
        """Every hand recorded under the given labels, ignoring frame grouping."""
        for frame in self.segment(*labels):
            yield from frame.hands

    @property
    def cameras(self) -> tuple[int, ...]:
        """Every camera that contributed to this recording."""
        seen: dict[int, None] = {}
        for frame in self.frames:
            for camera_id in frame.cameras:
                seen.setdefault(camera_id, None)
        return tuple(sorted(seen))

    def problems(self) -> list[str]:
        """Ways this recording cannot answer the questions asked of it.

        Worth reporting loudly. A threshold fitted from a prompt where the other
        hand was also in shot is not a measurement of anything, but it looks
        exactly like one -- so the recording has to be able to say when it is not
        fit to be fitted.
        """
        found: list[str] = []
        cameras = max(len(self.cameras), 1)

        for label in self.labels():
            frames = list(self.segment(label))
            if not frames:
                continue

            with_hands = [f for f in frames if f.hands]
            if label == "none":
                share = len(with_hands) / len(frames)
                if share > 0.1:
                    found.append(
                        f"'{label}': a hand was visible in {share:.0%} of frames; "
                        "the baseline needs both hands right out of shot"
                    )
                continue

            if len(with_hands) < len(frames) * 0.5:
                found.append(
                    f"'{label}': a hand was found in only "
                    f"{len(with_hands) / len(frames):.0%} of frames"
                )
                continue

            # More hands than cameras means a second hand was in shot, and its
            # shape is being recorded under this prompt's label.
            crowded = [f for f in with_hands if len(f.hands) > cameras]
            if len(crowded) > len(with_hands) * 0.25:
                found.append(
                    f"'{label}': two hands were in shot for "
                    f"{len(crowded) / len(with_hands):.0%} of frames; only the "
                    "performing hand should be visible"
                )

        return found

    def summary(self) -> str:
        counts: dict[str, int] = {}
        hands = 0
        for frame in self.frames:
            counts[frame.label] = counts.get(frame.label, 0) + 1
            hands += len(frame.hands)
        parts = ", ".join(f"{label} {count}" for label, count in counts.items())
        rig = f"{len(self.cameras)} camera(s) {list(self.cameras)}"
        return f"{len(self.frames)} frames ({hands} hand samples, {rig}): {parts}"


def latest_session() -> Path | None:
    """Most recently recorded session, if any."""
    if not SESSIONS_DIR.is_dir():
        return None
    files = sorted(SESSIONS_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def resolve(path: Path | None) -> Path:
    """Pick an explicit session, or fall back to the newest one."""
    if path is not None:
        return path
    found = latest_session()
    if found is None:
        raise FileNotFoundError(
            f"no recordings in {SESSIONS_DIR}; run 'mindcontrol record' first"
        )
    return found
