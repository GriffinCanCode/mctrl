"""Mode arbitration between your hands and your hardware.

The rule that makes gesture control livable: the physical mouse and keyboard
always win, immediately, without asking. Touch either one and gesture output
stops mid-motion; stop touching them and it comes back on its own. You never
"exit" hand mode, you just reach for the trackpad.

Three modes:

``OFF``       nothing is driven; only a held open palm is watched for.
``ACTIVE``    hands are driving the cursor.
``SUSPENDED`` hands are tracked but muted, because hardware was just used.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from enum import Enum

import Quartz

from ..config import ModesConfig
from .events import is_ours


class Mode(Enum):
    OFF = "off"
    ACTIVE = "active"
    SUSPENDED = "suspended"


class PhysicalInputWatcher:
    """Watches for real mouse and keyboard use on a private run loop.

    A listen-only event tap needs a CFRunLoop to deliver callbacks, and the main
    thread already belongs to the menu bar, so the tap gets its own thread.
    Our own synthetic events arrive here too and are filtered out by their tag.
    """

    EVENTS = (
        Quartz.kCGEventMouseMoved,
        Quartz.kCGEventLeftMouseDown,
        Quartz.kCGEventRightMouseDown,
        Quartz.kCGEventScrollWheel,
        Quartz.kCGEventKeyDown,
        Quartz.kCGEventFlagsChanged,
    )

    def __init__(self, on_input: Callable[[], None]) -> None:
        self._on_input = on_input
        self._thread: threading.Thread | None = None
        self._runloop = None
        self._tap = None
        self.error: str | None = None

    def start(self) -> bool:
        started = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(started,), name="input-watch")
        self._thread.daemon = True
        self._thread.start()
        started.wait(timeout=3.0)
        return self._tap is not None

    def _run(self, started: threading.Event) -> None:
        mask = 0
        for event_type in self.EVENTS:
            mask |= Quartz.CGEventMaskBit(event_type)
        self._tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly,
            mask,
            self._callback,
            None,
        )
        if self._tap is None:
            self.error = (
                "could not observe input; grant Accessibility permission to enable "
                "automatic hand-off to the mouse and keyboard"
            )
            started.set()
            return

        source = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        self._runloop = Quartz.CFRunLoopGetCurrent()
        Quartz.CFRunLoopAddSource(self._runloop, source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(self._tap, True)
        started.set()
        Quartz.CFRunLoopRun()

    def _callback(self, proxy, event_type, event, refcon):
        # A tap can be disabled by the system if it ever runs too slowly; the
        # documented recovery is simply to switch it back on.
        if event_type in (
            Quartz.kCGEventTapDisabledByTimeout,
            Quartz.kCGEventTapDisabledByUserInput,
        ):
            Quartz.CGEventTapEnable(self._tap, True)
            return event
        if not is_ours(event):
            self._on_input()
        return event

    def stop(self) -> None:
        if self._tap is not None:
            Quartz.CGEventTapEnable(self._tap, False)
        if self._runloop is not None:
            Quartz.CFRunLoopStop(self._runloop)
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None


class ModeManager:
    """Owns the current mode and the rules for changing it."""

    def __init__(self, cfg: ModesConfig, on_change: Callable[[Mode], None] | None = None) -> None:
        self._cfg = cfg
        self._on_change = on_change
        self._lock = threading.Lock()
        self._mode = Mode.ACTIVE if cfg.start_engaged else Mode.OFF
        self._last_physical_input = 0.0
        self._watcher: PhysicalInputWatcher | None = None
        self.watcher_error: str | None = None

    def start(self) -> None:
        if not self._cfg.suspend_on_physical_input:
            return
        self._watcher = PhysicalInputWatcher(self._note_physical_input)
        if not self._watcher.start():
            self.watcher_error = self._watcher.error
            print(f"[modes] {self.watcher_error}")
            self._watcher = None

    def stop(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

    @property
    def mode(self) -> Mode:
        """Current mode, resolving an expired suspension on read."""
        with self._lock:
            if self._mode is Mode.SUSPENDED:
                idle = time.monotonic() - self._last_physical_input
                if idle >= self._cfg.resume_after_s:
                    self._set(Mode.ACTIVE)
            return self._mode

    @property
    def engaged(self) -> bool:
        """True when gesture output should actually reach the system."""
        return self.mode is Mode.ACTIVE

    @property
    def watching_for_engage(self) -> bool:
        """True when the engage gesture is the only thing being listened for."""
        return self.mode is Mode.OFF

    def toggle(self) -> Mode:
        """Flip between off and active, clearing any suspension."""
        with self._lock:
            self._set(Mode.OFF if self._mode in (Mode.ACTIVE, Mode.SUSPENDED) else Mode.ACTIVE)
            return self._mode

    def set_mode(self, mode: Mode) -> None:
        with self._lock:
            self._set(mode)

    def _note_physical_input(self) -> None:
        """Called from the tap thread on any real input."""
        with self._lock:
            self._last_physical_input = time.monotonic()
            # Only an actively driving session gets suspended. If control is off,
            # using the mouse should not silently arm it.
            if self._mode is Mode.ACTIVE:
                self._set(Mode.SUSPENDED)

    def _set(self, mode: Mode) -> None:
        """Change mode. Caller holds the lock; the callback runs outside it."""
        if mode is self._mode:
            return
        self._mode = mode
        if self._on_change is not None:
            self._on_change(mode)

    def describe(self) -> str:
        mode = self.mode
        if mode is Mode.SUSPENDED:
            remaining = self._cfg.resume_after_s - (time.monotonic() - self._last_physical_input)
            return f"suspended ({max(remaining, 0.0):.1f}s)"
        return mode.value
