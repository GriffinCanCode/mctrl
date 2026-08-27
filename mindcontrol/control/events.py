"""Shared plumbing for posting synthetic events.

Every event this app injects is stamped with the same marker so the physical-input
watcher in `modes.py` can recognise and ignore it. If the app could not tell its
own output apart from the user's input, it would suspend itself the moment it
moved the cursor.
"""

from __future__ import annotations

import Quartz

EVENT_MARKER = 0x4D494E44  # "MIND"


def create_source():
    """An event source tagged as ours, or None if the system refuses one."""
    source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    if source is not None:
        Quartz.CGEventSourceSetUserData(source, EVENT_MARKER)
    return source


def post(event) -> None:
    """Tag and dispatch one event to the HID tap."""
    if event is None:
        return
    Quartz.CGEventSetIntegerValueField(event, Quartz.kCGEventSourceUserData, EVENT_MARKER)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def is_ours(event) -> bool:
    """True when an observed event was injected by this app."""
    return Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceUserData) == EVENT_MARKER
