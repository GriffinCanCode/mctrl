"""Finding out which cameras exist.

OpenCV offers no way to enumerate capture devices, only to try opening one by
index, so discovery means probing indices and seeing what answers. macOS reports
proper device names through AVFoundation, which is worth reading: index order is
not stable across reboots or replugs, and picking a camera by number alone is how
you end up gaze-tracking from the camera pointed at the wall.

Names are a strong hint rather than proof. AVFoundation's discovery order has been
observed to disagree with OpenCV's indices, and there is no identifier shared
between the two APIs to reconcile them. So `--preview` exists: it shows a frame
from each index, which is the only unambiguous way to learn which camera is which.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import cv2

MAX_PROBE = 8
# A camera that has just been opened can take a moment to deliver its first
# frame -- built-in webcams especially, since the indicator light has to come up.
# Giving up after a single failed read reports a working camera as absent.
READ_ATTEMPTS = 5
READ_DELAY_S = 0.12
# AVFoundation reports a placeholder frame rate until the stream settles, so a
# camera that woke slowly can advertise 1fps while really delivering 30. Reporting
# that is worse than saying nothing: it invites the user to exclude a good camera.
SETTLE_READS = 5
MEASURE_FRAMES = 15
PLAUSIBLE_FPS = 5.0


def _frame_rate(capture: cv2.VideoCapture) -> float:
    """The camera's real frame rate, timed directly if it will not say."""
    for _ in range(SETTLE_READS):
        capture.read()
    declared = float(capture.get(cv2.CAP_PROP_FPS))
    if declared >= PLAUSIBLE_FPS:
        return declared
    start = time.perf_counter()
    read = sum(bool(capture.read()[0]) for _ in range(MEASURE_FRAMES))
    elapsed = time.perf_counter() - start
    return read / elapsed if elapsed > 0 and read else declared


@dataclass(frozen=True)
class Device:
    index: int
    width: int
    height: int
    fps: float
    name: str = ""
    usable: bool = True
    problem: str = ""

    def describe(self) -> str:
        label = self.name or "unnamed device"
        if not self.usable:
            return f"  [{self.index}] {label}  UNAVAILABLE - {self.problem}"
        return f"  [{self.index}] {label}  {self.width}x{self.height} @ {self.fps:.0f}fps"


def names() -> list[str]:
    """Camera names in AVFoundation's order, which matches OpenCV's indices."""
    try:
        import AVFoundation
    except ImportError:
        return []
    try:
        # Modern discovery session; the older devicesWithMediaType_ is deprecated
        # and returns nothing on recent macOS.
        discover = (
            AVFoundation.AVCaptureDeviceDiscoverySession
            .discoverySessionWithDeviceTypes_mediaType_position_
        )
        session = discover(
            [
                AVFoundation.AVCaptureDeviceTypeBuiltInWideAngleCamera,
                AVFoundation.AVCaptureDeviceTypeExternal,
                AVFoundation.AVCaptureDeviceTypeContinuityCamera,
            ],
            AVFoundation.AVMediaTypeVideo,
            0,
        )
        return [str(device.localizedName()) for device in session.devices()]
    except (AttributeError, TypeError):
        return []


def probe(max_index: int = MAX_PROBE) -> list[Device]:
    """Open each index in turn and report what it can actually deliver.

    Bounded by the AVFoundation device count when that is available, because
    probing past the last device makes OpenCV shout about indices being out of
    bounds -- noise that looks like a failure but is just the end of the list.
    """
    labels = names()
    limit = min(max_index, len(labels)) if labels else max_index
    found: list[Device] = []

    for index in range(limit):
        name = labels[index] if index < len(labels) else ""
        capture = cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
        if not capture.isOpened():
            capture.release()
            found.append(
                Device(index, 0, 0, 0.0, name, usable=False, problem="could not be opened")
            )
            continue

        # isOpened() alone is optimistic; a device that cannot produce an image
        # is no use to us, so insist on a real frame before believing it.
        frame = None
        for _ in range(READ_ATTEMPTS):
            ok, frame = capture.read()
            if ok and frame is not None:
                break
            time.sleep(READ_DELAY_S)
            frame = None

        if frame is None:
            found.append(
                Device(
                    index, 0, 0, 0.0, name,
                    usable=False,
                    problem="opened but sent no frames (in use by another app?)",
                )
            )
        else:
            found.append(
                Device(
                    index=index,
                    width=int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
                    height=int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                    fps=_frame_rate(capture),
                    name=name,
                )
            )
        capture.release()
    return found


def preview(devices: list[Device], height: int = 260) -> None:
    """Show one frame from each usable camera, side by side and labelled.

    Grabbed one device at a time: opening several cameras at once can exceed the
    bus bandwidth and make an innocent device look broken.
    """
    import numpy as np

    tiles = []
    for device in devices:
        if not device.usable:
            continue
        capture = cv2.VideoCapture(device.index, cv2.CAP_AVFOUNDATION)
        frame = None
        for _ in range(READ_ATTEMPTS):
            ok, candidate = capture.read()
            if ok and candidate is not None:
                frame = candidate
                break
            time.sleep(READ_DELAY_S)
        capture.release()
        if frame is None:
            continue

        scaled = cv2.resize(frame, (int(frame.shape[1] * height / frame.shape[0]), height))
        cv2.rectangle(scaled, (0, 0), (scaled.shape[1], 30), (20, 20, 20), -1)
        cv2.putText(
            scaled,
            f"[{device.index}] {device.name}",
            (8, 21),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (240, 240, 240),
            1,
            cv2.LINE_AA,
        )
        tiles.append(scaled)

    if not tiles:
        print("[cameras] nothing to preview")
        return

    print("[cameras] preview open; press any key to close")
    cv2.imshow("mindcontrol cameras", np.hstack(tiles))
    cv2.waitKey(0)
    cv2.destroyAllWindows()
    for _ in range(4):
        cv2.waitKey(1)


def run(max_index: int = MAX_PROBE, show: bool = False) -> int:
    devices = probe(max_index)
    usable = [device for device in devices if device.usable]

    if not devices:
        print("[cameras] none found; check System Settings > Privacy & Security > Camera")
        return 2

    print(f"[cameras] {len(usable)} of {len(devices)} device(s) usable:")
    for device in devices:
        print(device.describe())

    if not usable:
        return 2

    indices = ", ".join(str(d.index) for d in usable)
    print("\nto use them together, in config.toml:")
    print(f"  [cameras]\n  devices = [{indices}]\n  primary_gaze = {usable[0].index}")
    print("\nprimary_gaze should be whichever camera sits nearest the screen you look at;")
    print("it is the one gaze is estimated from, so put it under your main display.")
    print("\nNames come from AVFoundation and its order can disagree with these indices.")
    print("Run 'mindcontrol cameras --preview' to see which index is really which.")

    if show:
        print()
        preview(devices)
    return 0
