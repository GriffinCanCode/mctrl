"""Synthetic mouse input via Quartz.

Two details are load-bearing.

*Tagging.* Every event carries a user-data marker so the physical-input watcher
can tell our own synthetic moves from the user's real ones. Without it the app
would see its own cursor motion, decide a human grabbed the mouse, and suspend
itself the instant it started working.

*Click chaining.* macOS decides what a double click is from the click-state field
on the event, not from two clicks arriving close together. Real trackpads set it;
so do we, which is what makes two quick pinches open a folder in Finder.
"""

from __future__ import annotations

import time

import Quartz

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
    """Posts cursor, button and scroll events."""

    def __init__(self, double_click_ms: float = 400.0) -> None:
        self._source = create_source()
        self._double_click_ms = double_click_ms
        self._held: str | None = None
        self._last_click_at = 0.0
        self._last_click_point = (0.0, 0.0)
        self._click_run = 0
        self.refresh_bounds()

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
        x, y = self.location()
        self._move_event(*self._clamp(x + dx, y + dy))

    def move_to_fraction(self, fx: float, fy: float) -> None:
        """Jump to a point given as fractions of the calibrated display, for gaze warps."""
        left, top, _, _ = self._gaze_box
        width, height = self.gaze_size
        self._move_event(*self._clamp(left + fx * width, top + fy * height))

    def click(self, button: str = "left") -> None:
        """Click, chaining into a double or triple click when repeated quickly."""
        if button not in _BUTTON_EVENTS:
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
        down, _, index = _BUTTON_EVENTS[button]
        x, y = self.location()
        self._post(Quartz.CGEventCreateMouseEvent(self._source, down, (x, y), index))
        self._held = button

    def release(self, button: str | None = None) -> None:
        """Let go of a held button. Safe to call when nothing is held."""
        held = self._held if button is None else button
        if held is None or held not in _BUTTON_EVENTS:
            self._held = None
            return
        _, up, index = _BUTTON_EVENTS[held]
        x, y = self.location()
        self._post(Quartz.CGEventCreateMouseEvent(self._source, up, (x, y), index))
        self._held = None

    def scroll(self, dx: float, dy: float) -> None:
        """Scroll by a pixel delta, following the hand as if it held the page."""
        # Pulling your hand down should drag the content down, which in wheel
        # terms is a positive vertical value; the camera's y axis grows downward,
        # hence the negation.
        event = Quartz.CGEventCreateScrollWheelEvent2(
            self._source, Quartz.kCGScrollEventUnitPixel, 2, int(-dy), int(dx), 0
        )
        self._post(event)
