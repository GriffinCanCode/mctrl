"""The public API: what other applications talk to.

Everything MindControl does, behind one small vocabulary of modules and verbs.
Five modules -- ``status``, ``modes``, ``tracking``, ``input``, ``system`` -- and
one way in from anywhere:

    from mindcontrol.api import MindControl

    with MindControl.connect() as mc:
        mc.modes.engage()
        for delivery in mc.tracking.events(["gestures"], timeout=5.0):
            print(delivery.stream, delivery.payload)

From another language, the same surface is a Unix socket speaking one JSON
object per line; ``mindcontrol api describe`` prints the whole catalogue, which
is the same table this package dispatches from.

Importing this package is cheap: neither MediaPipe nor Quartz is loaded until
something asks to run a pipeline in this process.
"""

from __future__ import annotations

from .contract import (
    GAZE,
    GESTURES,
    HANDS,
    PROTOCOL_VERSION,
    STATUS,
    STREAMS,
    VERBS,
    ApiError,
    GazeSnapshot,
    GestureEventMsg,
    HandsFrame,
    HandSnapshot,
    StatusSnapshot,
    catalogue,
)
from .facade import MindControl
from .hub import Delivery

__all__ = [
    "GAZE",
    "GESTURES",
    "HANDS",
    "PROTOCOL_VERSION",
    "STATUS",
    "STREAMS",
    "VERBS",
    "ApiError",
    "Delivery",
    "GazeSnapshot",
    "GestureEventMsg",
    "HandSnapshot",
    "HandsFrame",
    "MindControl",
    "StatusSnapshot",
    "catalogue",
]
