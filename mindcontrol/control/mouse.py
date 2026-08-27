"""Where an intent leaves Python.

Two ways out, same interface.

*Through the native helper*, when it is running. It owns the cursor, integrates
motion at display rate, snaps to whatever is nearest, and draws the highlight --
none of which can be done from here, because it needs a thread that is never
behind the GIL and a hundred accessibility hit tests a second. This is the path
that feels smooth, and it is the default.

*Straight to Quartz*, when the helper is not available -- not built yet, refused
permission, or crashed. Not a legacy path: it is the reason a missing Swift
toolchain degrades the feel rather than breaking the app. It posts one event per
camera frame, which is exactly as stepped as it sounds, and is why the helper
exists.

Two details are load-bearing on both paths.

*Tagging.* Every event carries a user-data marker so the physical-input watcher
can tell our own synthetic moves from the user's real ones. Without it the app
would see its own cursor motion, decide a human grabbed the mouse, and suspend
itself the instant it started working. The helper stamps the same marker, which
is why ``events.EVENT_MARKER`` and ``eventMarker`` in ``Cursor.swift`` have to
agree.

*Click chaining.* macOS decides what a double click is from the click-state field
on the event, not from two clicks arriving close together. Real trackpads set it;
so do we, which is what makes two quick pinches open a folder in Finder.
"""

from __future__ import annotations

import time

import Quartz

from .bridge import Bridge
from .events import create_source, post

_BUTTON_EVENTS = {
    "left": (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp, Quartz.kCGMouseButtonLeft),
    "right": (
        Quartz.kCGEventRightMouseDown,
        Quartz.kCGEventRightMouseUp,
        Quartz.kCGMouseButtonRight,
    ),
}
_DRAG_EVENTS = {
    "left": Quartz.kCGEventLeftMouseDragged,
    "right": Quartz.kCGEventRightMouseDragged,
}


def main_display_bounds() -> tuple[float, float, float, float]:
    """The main display's rect, as (min_x, min_y, max_x, max_y).

    Gaze is expressed as a fraction of this one screen rather than of the whole
    desktop. Calibration can only teach where you look on the screen the camera
    watched you look at, and a fraction of a three-monitor desktop would put the
    cursor on a display you were never calibrated for.
    """
    box = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
    return (
        box.origin.x,
        box.origin.y,
        box.origin.x + box.size.width,
        box.origin.y + box.size.height,
    )


def desktop_bounds() -> tuple[float, float, float, float]:
    """Union of every active display, as (min_x, min_y, max_x, max_y).

    Cursor coordinates are global across displays, so clamping to the main
    screen would trap the pointer on a multi-monitor desk. Hands can reach every
    screen; only gaze is confined to the calibrated one.
    """
    error, display_ids, _ = Quartz.CGGetActiveDisplayList(16, None, None)
    if error or not display_ids:
        main = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
        return (
            main.origin.x,
            main.origin.y,
            main.origin.x + main.size.width,
            main.origin.y + main.size.height,
        )
    boxes = [Quartz.CGDisplayBounds(display_id) for display_id in display_ids]
    return (
        min(b.origin.x for b in boxes),
        min(b.origin.y for b in boxes),
        max(b.origin.x + b.size.width for b in boxes),
        max(b.origin.y + b.size.height for b in boxes),
    )


class Mouse:
    """Posts cursor, button and scroll events, through the helper when it is up.

    Drag state is tracked here regardless of which path is in use, because the
    press and release that bracket it are still decided in Python -- the gaze arm
    reads :attr:`dragging` to know it must not warp mid-drag.
    """

    def __init__(self, double_click_ms: float = 400.0, bridge: Bridge | None = None) -> None:
        self._source = create_source()
        self._double_click_ms = double_click_ms
        self._bridge = bridge
        self._held: str | None = None
        self._last_click_at = 0.0
        self._last_click_point = (0.0, 0.0)
        self._click_run = 0
        self.refresh_bounds()

    @property
    def _native(self) -> Bridge | None:
        """The helper, if it is there to take the intent."""
        bridge = self._bridge
        return bridge if bridge is not None and bridge.connected else None

    def refresh_bounds(self) -> None:
        """Re-read display geometry, for when a monitor is plugged in or unplugged."""
        self._min_x, self._min_y, self._max_x, self._max_y = desktop_bounds()
        self._gaze_box = main_display_bounds()

    @property
    def size(self) -> tuple[float, float]:
        """Size of the whole desktop, which hand movement may roam across."""
        return self._max_x - self._min_x, self._max_y - self._min_y

    @property
    def gaze_size(self) -> tuple[float, float]:
        """Size of the display gaze is calibrated against."""
        left, top, right, bottom = self._gaze_box
        return right - left, bottom - top

    @property
    def dragging(self) -> bool:
        return self._held is not None

    def location(self) -> tuple[float, float]:
        """Where the cursor actually is right now.

        Read from the system rather than remembered, so that if you nudge the
        real mouse, gesture movement carries on from where you left it.
        """
        point = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        return point.x, point.y

    def _clamp(self, x: float, y: float) -> tuple[float, float]:
        return (
            min(max(x, self._min_x), self._max_x - 1.0),
            min(max(y, self._min_y), self._max_y - 1.0),
        )

    def _post(self, event) -> None:
        post(event)

    def _move_event(self, x: float, y: float) -> None:
        # While a button is down the motion must be posted as a drag, or the app
        # underneath sees the cursor teleport without ever being dragged.
        if self._held is not None:
            event_type = _DRAG_EVENTS[self._held]
            button = _BUTTON_EVENTS[self._held][2]
        else:
            event_type = Quartz.kCGEventMouseMoved
            button = Quartz.kCGMouseButtonLeft
        self._post(Quartz.CGEventCreateMouseEvent(self._source, event_type, (x, y), button))

    def move_by(self, dx: float, dy: float) -> None:
        native = self._native
        if native is not None:
            # The helper accumulates this into a goal and walks the cursor there at
            # display rate. Sending a delta rather than a destination is what lets
            # it do that without ever reading the cursor back.
            native.move_by(dx, dy)
            return
        x, y = self.location()
        self._move_event(*self._clamp(x + dx, y + dy))

    def move_to_fraction(self, fx: float, fy: float) -> None:
        """Jump to a point given as fractions of the calibrated display, for gaze warps."""
        native = self._native
        if native is not None:
            native.warp_to_fraction(fx, fy)
            return
        left, top, _, _ = self._gaze_box
        width, height = self.gaze_size
        self._move_event(*self._clamp(left + fx * width, top + fy * height))

    def click(self, button: str = "left") -> None:
        """Click, chaining into a double or triple click when repeated quickly."""
        if button not in _BUTTON_EVENTS:
            return
        native = self._native
        if native is not None:
            # Chaining is done on the far side, where the click's actual landing
            # point is known: it resolves to the snapped target, not to wherever
            # the cursor had drifted to.
            native.click(button)
            return
        down, up, index = _BUTTON_EVENTS[button]
        x, y = self.location()
        now = time.monotonic()
        near = abs(x - self._last_click_point[0]) < 6 and abs(y - self._last_click_point[1]) < 6
        in_time = (now - self._last_click_at) * 1000.0 < self._double_click_ms
        self._click_run = self._click_run + 1 if (near and in_time) else 1
        self._last_click_at = now
        self._last_click_point = (x, y)

        for event_type in (down, up):
            event = Quartz.CGEventCreateMouseEvent(self._source, event_type, (x, y), index)
            if event is not None:
                Quartz.CGEventSetIntegerValueField(
                    event, Quartz.kCGMouseEventClickState, min(self._click_run, 3)
                )
            self._post(event)

    def press(self, button: str = "left") -> None:
        if button not in _BUTTON_EVENTS or self._held is not None:
            return
        self._held = button
        native = self._native
        if native is not None:
            native.press(button)
            return
        down, _, index = _BUTTON_EVENTS[button]
        x, y = self.location()
        self._post(Quartz.CGEventCreateMouseEvent(self._source, down, (x, y), index))

    def release(self, button: str | None = None) -> None:
        """Let go of a held button. Safe to call when nothing is held."""
        held = self._held if button is None else button
        self._held = None
        if held is None or held not in _BUTTON_EVENTS:
            return
        native = self._native
        if native is not None:
            native.release(held)
            return
        _, up, index = _BUTTON_EVENTS[held]
        x, y = self.location()
        self._post(Quartz.CGEventCreateMouseEvent(self._source, up, (x, y), index))

    def scroll(self, dx: float, dy: float) -> None:
        """Scroll by a pixel delta, following the hand as if it held the page."""
        native = self._native
        if native is not None:
            native.scroll(dx, dy)
            return
        # Pulling your hand down should drag the content down, which in wheel
        # terms is a positive vertical value; the camera's y axis grows downward,
        # hence the negation.
        event = Quartz.CGEventCreateScrollWheelEvent2(
            self._source, Quartz.kCGScrollEventUnitPixel, 2, int(-dy), int(dx), 0
        )
        self._post(event)
