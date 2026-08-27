"""Synthetic key input via Quartz.

Used for the system-level gestures -- desktop switching, Mission Control,
dictation -- where the right thing to send is the shortcut a user would type.
Sending real shortcuts means these gestures work with whatever the user has
already configured, instead of this app reimplementing window management.
"""

from __future__ import annotations

import Quartz

from ..config import KeyBinding
from .events import create_source, post

# macOS virtual key codes. Only the keys worth binding to a gesture are listed.
KEY_CODES: dict[str, int] = {
    "left": 0x7B,
    "right": 0x7C,
    "down": 0x7D,
    "up": 0x7E,
    "space": 0x31,
    "tab": 0x30,
    "return": 0x24,
    "escape": 0x35,
    "delete": 0x33,
    "f1": 0x7A,
    "f2": 0x78,
    "f3": 0x63,
    "f4": 0x76,
    "f5": 0x60,
    "f6": 0x61,
    "f7": 0x62,
    "f8": 0x64,
    "f9": 0x65,
    "f10": 0x6D,
    "f11": 0x67,
    "f12": 0x6F,
    "a": 0x00,
    "c": 0x08,
    "d": 0x02,
    "h": 0x04,
    "m": 0x2E,
    "n": 0x2D,
    "s": 0x01,
    "t": 0x11,
    "v": 0x09,
    "w": 0x0D,
    "z": 0x06,
}

MODIFIER_FLAGS: dict[str, int] = {
    "cmd": Quartz.kCGEventFlagMaskCommand,
    "command": Quartz.kCGEventFlagMaskCommand,
    "ctrl": Quartz.kCGEventFlagMaskControl,
    "control": Quartz.kCGEventFlagMaskControl,
    "alt": Quartz.kCGEventFlagMaskAlternate,
    "option": Quartz.kCGEventFlagMaskAlternate,
    "shift": Quartz.kCGEventFlagMaskShift,
    "fn": Quartz.kCGEventFlagMaskSecondaryFn,
}


class Keyboard:
    """Sends keystrokes for named actions defined in config."""

    def __init__(self, keys: dict[str, KeyBinding]) -> None:
        self._source = create_source()
        self._keys = keys

    def update_bindings(self, keys: dict[str, KeyBinding]) -> None:
        self._keys = keys

    def tap(self, binding: KeyBinding) -> bool:
        """Press and release one key with modifiers. False if the key is unknown."""
        code = KEY_CODES.get(binding.key.lower())
        if code is None:
            print(f"[keyboard] no key code for {binding.key!r}; add it to KEY_CODES")
            return False
        flags = 0
        for name in binding.mods:
            flag = MODIFIER_FLAGS.get(name.lower())
            if flag is None:
                print(f"[keyboard] unknown modifier {name!r}")
                continue
            flags |= flag

        for pressed in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(self._source, code, pressed)
            if event is not None and flags:
                Quartz.CGEventSetFlags(event, flags)
            post(event)
        return True

    def run_action(self, action: str) -> bool:
        """Fire a named action such as ``desktop_left``, if it is bound."""
        binding = self._keys.get(action)
        if binding is None:
            print(f"[keyboard] action {action!r} has no key binding in [keys]")
            return False
        return self.tap(binding)
