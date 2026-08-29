"""What a gesture means, and which application it means it to.

The pointing gestures need no table: a pinch is a click everywhere, and the
native helper already aims it at whatever the accessibility API says is under
the cursor, so those work in any application without being told about it. The
*discrete* gestures are the opposite -- a sweep left is "previous page" in
Preview, "back" in Safari and "previous desktop" on the desktop, and no single
answer is right in all three.

So they resolve through here, in one order:

1. a binding scoped to the application in front,
2. the binding that applies everywhere,
3. nothing, which is what an unbound gesture must do.

Two properties are worth the small amount of code they cost. A scope may be
written as the bundle identifier, the application's name, or the last component
of the identifier, so ``com.apple.Safari``, ``Safari`` and ``safari`` all name
the same application and nobody has to look an identifier up to get started. And
an app-scoped binding of ``none`` *mutes* a global one, which is the only way to
say "this gesture does nothing here" without deleting it everywhere.

Nothing in this module imports Quartz, AppKit or the pipeline: the table is data
plus a resolution rule, and both the API contract and the tests read it directly.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .keys import format_chord, parse_chord

# The gestures that reach the binding table. Everything else the engine emits is
# a pointer action the pipeline handles itself -- there is no useful sense in
# which a click could be rebound, since the click *is* the gesture.
#
# Kept as literal names rather than derived from ``Action`` so this module stays
# free of the engine, and held to the engine by a test instead.
BINDABLE: tuple[str, ...] = (
    "swipe_left",
    "swipe_right",
    "palm_push_up",
    "palm_push_down",
    "telephone",
)

# An action meaning "deliberately nothing". Only useful with an app scope, where
# it suppresses whatever the global table would otherwise have fired.
MUTED = "none"


@dataclass(frozen=True)
class App:
    """The application in front, as much of it as macOS will say."""

    bundle: str = ""
    name: str = ""

    def __bool__(self) -> bool:
        return bool(self.bundle or self.name)

    def matches(self, scope: str) -> bool:
        """Whether a scope name written by a human refers to this application.

        Three spellings, because requiring the bundle identifier would mean
        looking one up before the first binding could be written, and requiring
        the name would break as soon as an application was localised.
        """
        wanted = scope.strip().lower()
        if not wanted:
            return False
        bundle = self.bundle.lower()
        return wanted in (bundle, self.name.lower(), bundle.rpartition(".")[2])

    def to_json(self) -> dict[str, Any]:
        return {"bundle": self.bundle, "name": self.name}


class BindingTable:
    """Gesture bindings, global and per application.

    Read on the frame loop and written by the API, so writes are marshalled onto
    that loop by the runtime rather than locked here -- the same rule the cursor
    and the gesture engine already follow.
    """

    def __init__(
        self,
        default: Mapping[str, str] | None = None,
        apps: Mapping[str, Mapping[str, str]] | None = None,
        actions: Iterable[str] = (),
    ) -> None:
        self._actions = set(actions)
        self.default: dict[str, str] = {}
        self.apps: dict[str, dict[str, str]] = {}
        for gesture, action in (default or {}).items():
            self._adopt(self.default, gesture, action, where="[bindings]")
        for scope, table in (apps or {}).items():
            for gesture, action in table.items():
                self._adopt(
                    self.apps.setdefault(scope, {}),
                    gesture,
                    action,
                    where=f"[bindings.{scope}]",
                )

    def _adopt(self, into: dict[str, str], gesture: str, action: str, *, where: str) -> None:
        """Take a binding from config, complaining rather than raising.

        A typo in one line of ``config.toml`` must not stop the app from
        starting, but it must not pass silently either: a binding that was
        accepted and does nothing is indistinguishable from a broken gesture.
        """
        try:
            into[gesture] = self.check(gesture, action)
        except ValueError as problem:
            print(f"[bindings] ignoring {where} {gesture}: {problem}")

    # ------------------------------------------------------------------ reading

    def resolve(self, gesture: str, app: App | None = None) -> str | None:
        """The action for a gesture in front of an application, or ``None``."""
        if app:
            for scope, table in self.apps.items():
                if gesture in table and app.matches(scope):
                    action = table[gesture]
                    return None if action == MUTED else action
        action = self.default.get(gesture)
        return None if action in (None, MUTED) else action

    def scope_for(self, app: App | None) -> str | None:
        """Which scope name, if any, currently applies. For reporting."""
        if not app:
            return None
        return next((scope for scope in self.apps if app.matches(scope)), None)

    def to_json(self, app: App | None = None) -> dict[str, Any]:
        """The whole table, plus what it currently resolves to.

        ``resolved`` is the answer to the question a consumer actually has --
        "what will this gesture do right now" -- which neither half of the table
        answers on its own.
        """
        return {
            "gestures": list(BINDABLE),
            "actions": sorted(self._actions),
            "app": (app or App()).to_json(),
            "scope": self.scope_for(app),
            "default": dict(self.default),
            "apps": {scope: dict(table) for scope, table in self.apps.items()},
            "resolved": {gesture: self.resolve(gesture, app) for gesture in BINDABLE},
        }

    # ------------------------------------------------------------------ writing

    def set(self, gesture: str, action: str, app: str | None = None) -> None:
        """Bind a gesture, globally or for one application. Raises ``ValueError``."""
        checked = self.check(gesture, action)
        if app:
            self.apps.setdefault(app.strip(), {})[gesture] = checked
        else:
            self.default[gesture] = checked

    def clear(self, gesture: str, app: str | None = None) -> bool:
        """Drop a binding. False when there was nothing there to drop."""
        if gesture not in BINDABLE:
            raise ValueError(f"no gesture {gesture!r}; there is {', '.join(BINDABLE)}")
        if app is None:
            return self.default.pop(gesture, None) is not None
        table = self.apps.get(app.strip())
        if table is None:
            return False
        dropped = table.pop(gesture, None) is not None
        if not table:
            del self.apps[app.strip()]
        return dropped

    def check(self, gesture: str, action: str) -> str:
        """Validate a binding and return the action as it should be stored.

        Refused rather than stored, because a binding is only discovered to be
        wrong at the moment the gesture is performed -- by which time the user is
        looking at their hand, not at a log.
        """
        if gesture not in BINDABLE:
            raise ValueError(f"no gesture {gesture!r}; there is {', '.join(BINDABLE)}")
        if not isinstance(action, str):
            raise ValueError("an action is a key chord or the name of one")
        wanted = action.strip()
        if not wanted or wanted.lower() == MUTED:
            return MUTED
        if wanted in self._actions:
            return wanted
        chord = parse_chord(wanted)
        if chord is None:
            raise ValueError(f"{wanted!r} is neither a name in [keys] nor a chord like cmd+shift+p")
        return format_chord(chord)


class Focus:
    """Which application is in front, asked no more often than it can change.

    Asking once per frame would be thirty AppKit calls a second for an answer
    that only changes when a human switches window, so the answer is held for
    ``ttl`` seconds. The staleness that buys is bounded by the same number, and a
    binding resolved against the application you just left is no worse than one
    resolved a frame early.

    Degrades to an empty :class:`App` rather than failing: without AppKit, or
    without an answer, every binding simply resolves globally.
    """

    def __init__(self, ttl: float = 0.4, ask: Callable[[], App] | None = None) -> None:
        self.ttl = ttl
        self._ask = ask or _frontmost
        self._app = App()
        self._asked_at = 0.0

    def current(self, now: float | None = None) -> App:
        moment = time.monotonic() if now is None else now
        if self._asked_at and moment - self._asked_at < self.ttl:
            return self._app
        self._asked_at = moment
        self._app = self._ask()
        return self._app


def _frontmost() -> App:
    """Ask AppKit, and answer "no idea" for every way that can fail.

    Broad on purpose. This runs on the frame loop, and it is an enrichment: an
    unknown application costs a per-app binding and nothing else, while an
    exception escaping here would stop the tracker over a question it only asked
    in case the answer was interesting. Under SSH, or with no window server, or
    on a build without the Cocoa bindings, there is genuinely no answer.
    """
    try:
        from AppKit import NSWorkspace

        running = NSWorkspace.sharedWorkspace().frontmostApplication()
        if running is None:
            return App()
        return App(
            bundle=str(running.bundleIdentifier() or ""),
            name=str(running.localizedName() or ""),
        )
    except Exception:  # pragma: no cover - depends on the machine, not the code
        return App()
