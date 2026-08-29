"""Key names, and the chords a binding may be written as.

Split out of :mod:`keyboard` because this half imports nothing. The binding table
has to be able to say whether ``cmd+shift+p`` is a real chord, and it is asked
that by the API contract and by the tests, neither of which may load Quartz --
one runs before the app starts, the other runs without a display.

A chord is written the way a menu writes it: modifiers and then a key, joined by
``+``. ``cmd+shift+p``, ``ctrl+left``, ``f5``. Case is ignored, so is whitespace,
and the modifier names macOS uses interchangeably are accepted interchangeably.
"""

from __future__ import annotations

from ..config import KeyBinding

# macOS virtual key codes -- ``kVK_ANSI_*`` from Carbon's Events.h. They describe
# a *position* on the keyboard rather than a character, so these are the ANSI
# names for those positions and not what a non-US layout prints on the key.
KEY_CODES: dict[str, int] = {
    "a": 0x00,
    "s": 0x01,
    "d": 0x02,
    "f": 0x03,
    "h": 0x04,
    "g": 0x05,
    "z": 0x06,
    "x": 0x07,
    "c": 0x08,
    "v": 0x09,
    "b": 0x0B,
    "q": 0x0C,
    "w": 0x0D,
    "e": 0x0E,
    "r": 0x0F,
    "y": 0x10,
    "t": 0x11,
    "1": 0x12,
    "2": 0x13,
    "3": 0x14,
    "4": 0x15,
    "6": 0x16,
    "5": 0x17,
    "=": 0x18,
    "9": 0x19,
    "7": 0x1A,
    "-": 0x1B,
    "8": 0x1C,
    "0": 0x1D,
    "]": 0x1E,
    "o": 0x1F,
    "u": 0x20,
    "[": 0x21,
    "i": 0x22,
    "p": 0x23,
    "return": 0x24,
    "l": 0x25,
    "j": 0x26,
    "'": 0x27,
    "k": 0x28,
    ";": 0x29,
    "\\": 0x2A,
    ",": 0x2B,
    "/": 0x2C,
    "n": 0x2D,
    "m": 0x2E,
    ".": 0x2F,
    "tab": 0x30,
    "space": 0x31,
    "`": 0x32,
    "delete": 0x33,
    "escape": 0x35,
    "f17": 0x40,
    "f18": 0x4F,
    "f19": 0x50,
    "f20": 0x5A,
    "f5": 0x60,
    "f6": 0x61,
    "f7": 0x62,
    "f3": 0x63,
    "f8": 0x64,
    "f9": 0x65,
    "f11": 0x67,
    "f13": 0x69,
    "f16": 0x6A,
    "f14": 0x6B,
    "f10": 0x6D,
    "f12": 0x6F,
    "f15": 0x71,
    "help": 0x72,
    "home": 0x73,
    "pageup": 0x74,
    "forwarddelete": 0x75,
    "f4": 0x76,
    "end": 0x77,
    "f2": 0x78,
    "pagedown": 0x79,
    "f1": 0x7A,
    "left": 0x7B,
    "right": 0x7C,
    "down": 0x7D,
    "up": 0x7E,
}

# Spellings for the same key, so a binding can be written the way it is spoken.
KEY_ALIASES: dict[str, str] = {
    "enter": "return",
    "esc": "escape",
    "backspace": "delete",
    "del": "forwarddelete",
    "spacebar": "space",
    "minus": "-",
    "equal": "=",
    "equals": "=",
    "plus": "=",
    "comma": ",",
    "period": ".",
    "dot": ".",
    "slash": "/",
    "backslash": "\\",
    "semicolon": ";",
    "quote": "'",
    "grave": "`",
    "backtick": "`",
    "leftbracket": "[",
    "rightbracket": "]",
    "pgup": "pageup",
    "pgdn": "pagedown",
    "pagedn": "pagedown",
    "arrowleft": "left",
    "arrowright": "right",
    "arrowup": "up",
    "arrowdown": "down",
}

# Canonical modifier per accepted spelling. The values are what `keyboard` maps
# onto Quartz flags, so this table is the one place a synonym is resolved.
MODIFIER_ALIASES: dict[str, str] = {
    "cmd": "cmd",
    "command": "cmd",
    "super": "cmd",
    "meta": "cmd",
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "option": "alt",
    "opt": "alt",
    "shift": "shift",
    "fn": "fn",
    "function": "fn",
}

MODIFIERS: tuple[str, ...] = ("cmd", "ctrl", "alt", "shift", "fn")


def resolve_key(name: str) -> str | None:
    """Canonical key name for a spelling of one, or ``None`` if it is not a key."""
    key = name.strip().lower()
    key = KEY_ALIASES.get(key, key)
    return key if key in KEY_CODES else None


def parse_chord(spec: str) -> KeyBinding | None:
    """``"cmd+shift+p"`` into a :class:`KeyBinding`, or ``None`` if it is not one.

    Deliberately total: an unparseable chord is a question the caller asked, not
    an exception it has to catch. Both callers -- validating a binding somebody
    typed, and firing one -- want a plain no.

    ``+`` is the separator and never a key name, which costs nothing: macOS has
    no key code for it either, since that position is shift and ``=``. Write it
    as ``plus`` or ``shift+=``. The alternative was reading a trailing separator
    as the key, and that turns ``cmd+`` -- a plain typo -- into a valid chord.
    """
    if not spec or not spec.strip():
        return None
    parts = [part.strip() for part in spec.strip().split("+")]
    key = resolve_key(parts[-1])
    if key is None:
        return None

    mods: list[str] = []
    for part in parts[:-1]:
        modifier = MODIFIER_ALIASES.get(part.lower())
        if modifier is None:
            return None
        if modifier not in mods:
            mods.append(modifier)
    return KeyBinding(key, mods)


def format_chord(binding: KeyBinding) -> str:
    """A chord back into the string that would parse to it, for reporting."""
    ordered = [name for name in MODIFIERS if name in {m.lower() for m in binding.mods}]
    return "+".join([*ordered, binding.key])
