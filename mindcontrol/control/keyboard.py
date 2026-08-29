"""Synthetic key input via Quartz.

What a bound gesture actually does. Sending the shortcut a user would type means
these gestures work with whatever that application already offers, instead of
this app reimplementing window management, or page navigation, or undo.

The key names and the chord syntax live in :mod:`keys`, which imports nothing, so
a binding can be checked before there is anything to send it to. This half is
only the sending.
"""

from __future__ import annotations

import Quartz

from ..config import KeyBinding
from .events import create_source, post
from .keys import KEY_CODES, MODIFIER_ALIASES, parse_chord, resolve_key

# Canonical modifier name to the flag Quartz wants. Synonyms are resolved by
# `keys.MODIFIER_ALIASES` before they arrive here, so there is one row per
# modifier rather than one per spelling of one.
MODIFIER_FLAGS: dict[str, int] = {
    "cmd": Quartz.kCGEventFlagMaskCommand,
    "ctrl": Quartz.kCGEventFlagMaskControl,
    "alt": Quartz.kCGEventFlagMaskAlternate,
    "shift": Quartz.kCGEventFlagMaskShift,
    "fn": Quartz.kCGEventFlagMaskSecondaryFn,
}


class Keyboard:
    """Sends keystrokes, named through ``[keys]`` or spelled out as a chord."""

    def __init__(self, keys: dict[str, KeyBinding]) -> None:
        self._source = create_source()
        self._keys = keys

    def update_bindings(self, keys: dict[str, KeyBinding]) -> None:
        self._keys = keys

    def tap(self, binding: KeyBinding) -> bool:
        """Press and release one key with modifiers. False if the key is unknown."""
        key = resolve_key(binding.key)
        if key is None:
            print(f"[keyboard] no key code for {binding.key!r}; add it to keys.KEY_CODES")
            return False
        flags = 0
        for name in binding.mods:
            modifier = MODIFIER_ALIASES.get(name.strip().lower())
            if modifier is None:
                print(f"[keyboard] unknown modifier {name!r}")
                continue
            flags |= MODIFIER_FLAGS[modifier]

        code = KEY_CODES[key]
        for pressed in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(self._source, code, pressed)
            if event is not None and flags:
                Quartz.CGEventSetFlags(event, flags)
            post(event)
        return True

    def run_action(self, action: str) -> bool:
        """Fire an action, whether it names a ``[keys]`` entry or spells out a chord.

        Both, rather than one or the other: the named indirection is what lets
        ``dictation`` stay one edit away from the key a user assigned in System
        Settings, and the chord is what lets a binding be written -- or sent over
        the API -- without inventing a name for it first.
        """
        binding = self._keys.get(action) or parse_chord(action)
        if binding is None:
            print(f"[keyboard] {action!r} is neither a name in [keys] nor a key chord")
            return False
        return self.tap(binding)
