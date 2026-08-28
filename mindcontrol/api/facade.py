"""The SDK: one object, five modules, the same verbs either way.

    from mindcontrol.api import MindControl

    with MindControl.connect() as mc:        # talk to the running menu-bar app
        mc.modes.engage()
        for delivery in mc.tracking.events(["gestures"]):
            print(delivery.payload.action)

    with MindControl.launch() as mc:         # or run the pipeline in this process
        print(mc.status().mode)

:meth:`connect` and :meth:`launch` differ in one line and nothing else. That is
the point of routing both through :mod:`mindcontrol.api.contract`: a consumer
written against an embedded pipeline works unchanged against a remote one, and a
verb cannot exist on one and not the other.

The heavy imports -- the pipeline, MediaPipe, Quartz -- happen inside
:meth:`launch`, so a program that only wants to talk to an app that is already
running does not pay for a copy of the tracker it will never start.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Protocol

from .contract import GESTURES, StatusSnapshot
from .hub import Delivery

if TYPE_CHECKING:
    from ..config import Config
    from .runtime import Runtime


class Backend(Protocol):
    """What a facade needs, whichever side of a socket the program is on."""

    def call(self, verb: str, params: dict[str, Any] | None = None) -> Any: ...

    def events(self, timeout: float | None = None) -> Iterator[Delivery]: ...

    def close(self) -> None: ...


class _Module:
    def __init__(self, backend: Backend) -> None:
        self._backend = backend


class Status(_Module):
    """One verb, so the module is callable: ``mc.status()``."""

    def get(self) -> StatusSnapshot:
        result = self._backend.call("status.get")
        return result if isinstance(result, StatusSnapshot) else StatusSnapshot.from_json(result)

    __call__ = get


class Modes(_Module):
    """Whether hands are driving, and turning that on and off."""

    def get(self) -> dict[str, Any]:
        return self._backend.call("modes.get")

    def set(self, mode: str) -> dict[str, Any]:
        return self._backend.call("modes.set", {"mode": mode})

    def toggle(self) -> dict[str, Any]:
        return self._backend.call("modes.toggle")

    def engage(self) -> dict[str, Any]:
        """Hand the cursor to the hands. Idempotent, unlike :meth:`toggle`."""
        return self.set("active")

    def disengage(self) -> dict[str, Any]:
        return self.set("off")

    @property
    def mode(self) -> str:
        return str(self.get()["mode"])

    @property
    def engaged(self) -> bool:
        return bool(self.get()["engaged"])


class Tracking(_Module):
    """The streams. Subscribe, then read; or just read and be subscribed for you."""

    def subscribe(
        self,
        streams: Iterable[str] | None = None,
        *,
        landmarks: bool = False,
        interval_ms: float = 0.0,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"landmarks": landmarks, "interval_ms": interval_ms}
        if streams is not None:
            params["streams"] = list(streams)
        return self._backend.call("tracking.subscribe", params)

    def unsubscribe(self, streams: Iterable[str] | None = None) -> dict[str, Any]:
        params = {} if streams is None else {"streams": list(streams)}
        return self._backend.call("tracking.unsubscribe", params)

    def events(
        self,
        streams: Iterable[str] | None = None,
        *,
        timeout: float | None = None,
        landmarks: bool = False,
        interval_ms: float = 0.0,
    ) -> Iterator[Delivery]:
        """Deliveries as they arrive, subscribing first if nothing is flowing yet.

        With a timeout the loop ends after that long without a frame, which is
        the difference between "watch for a moment" and "watch until quit".
        """
        self.subscribe(streams, landmarks=landmarks, interval_ms=interval_ms)
        return self._backend.events(timeout)

    def gestures(self, *, timeout: float | None = None) -> Iterator[Any]:
        """Only the intents, unwrapped. The stream most consumers want.

        Deliberately not a generator function: subscribing has to happen when
        this is called, not when the first frame is pulled, or a consumer that
        sets up its loop and then acts would miss everything in between.
        """
        stream = self.events([GESTURES], timeout=timeout)
        return (delivery.payload for delivery in stream if delivery.stream == GESTURES)


class Input(_Module):
    """Driving the cursor directly, through the same path a gesture takes.

    Which means through the native helper when it is running, so a warp from a
    consumer is smoothed and snapped exactly as a pinch would be. None of these
    require control to be engaged: an explicit call is not a gesture, and
    refusing it while the hands are down would make the module useless.
    """

    def move_by(self, dx: float, dy: float) -> dict[str, Any]:
        return self._backend.call("input.move_by", {"dx": dx, "dy": dy})

    def move_to(self, x: float, y: float) -> dict[str, Any]:
        """Warp to a fraction of the calibrated display, as a gaze warp does."""
        return self._backend.call("input.move_to", {"x": x, "y": y})

    def click(self, button: str = "left") -> dict[str, Any]:
        return self._backend.call("input.click", {"button": button})

    def press(self, button: str = "left") -> dict[str, Any]:
        return self._backend.call("input.press", {"button": button})

    def release(self, button: str | None = None) -> dict[str, Any]:
        params = {} if button is None else {"button": button}
        return self._backend.call("input.release", params)

    def scroll(self, dx: float, dy: float) -> dict[str, Any]:
        return self._backend.call("input.scroll", {"dx": dx, "dy": dy})


class System(_Module):
    """Calibration, config, and letting go of the cameras."""

    def calibrate(self) -> dict[str, Any]:
        """Start the nine-point calibration. Returns as soon as it is running."""
        return self._backend.call("system.calibrate")

    def reload_config(self) -> dict[str, Any]:
        return self._backend.call("system.reload_config")

    def pause(self) -> dict[str, Any]:
        return self._backend.call("system.pause")

    def resume(self) -> dict[str, Any]:
        return self._backend.call("system.resume")

    def describe(self) -> dict[str, Any]:
        """The whole surface as data, for generating a client or a menu."""
        return self._backend.call("system.describe")


class MindControl:
    """The API, as one object."""

    def __init__(self, backend: Backend, *, owns: Callable[[], None] | None = None) -> None:
        self._backend = backend
        self._owns = owns
        self.status = Status(backend)
        self.modes = Modes(backend)
        self.tracking = Tracking(backend)
        self.input = Input(backend)
        self.system = System(backend)

    # ------------------------------------------------------------------ opening

    @classmethod
    def connect(
        cls,
        socket_path: Path | str | None = None,
        *,
        timeout: float = 5.0,
    ) -> MindControl:
        """Attach to a MindControl that is already running."""
        from .client import Client

        client = Client(socket_path, timeout=timeout)
        client.open()
        return cls(client, owns=client.close)

    @classmethod
    def launch(
        cls,
        cfg: Config | None = None,
        *,
        config_path: Path | None = None,
        overlay: bool = False,
    ) -> MindControl:
        """Run the pipeline inside this process and drive it directly.

        For a consumer that wants tracking without a menu bar -- a kiosk, a test
        harness, another application embedding the tracker. The cameras, the
        models and the native helper all belong to the caller's process, so only
        one of these may exist at a time on one machine.
        """
        from ..config import ensure_dirs, load
        from ..pipeline import Pipeline
        from .runtime import Runtime

        ensure_dirs()
        resolved = cfg or load(config_path)
        if overlay:
            resolved.debug.overlay = True
        pipeline = Pipeline(resolved)
        runtime = Runtime(pipeline, config_path=config_path)
        pipeline.start()
        runtime.attach()
        backend = Local(runtime)

        def shutdown() -> None:
            runtime.close()
            pipeline.stop()

        return cls(backend, owns=shutdown)

    # ------------------------------------------------------------------ closing

    def close(self) -> None:
        self._backend.close()
        if self._owns is not None:
            self._owns()
            self._owns = None

    def __enter__(self) -> MindControl:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        self.close()

    # ------------------------------------------------------------------- direct

    def call(self, verb: str, params: dict[str, Any] | None = None) -> Any:
        """Any verb by name, for a consumer generating calls from ``system.describe``."""
        return self._backend.call(verb, params)

    def __call__(self, verb: str, params: dict[str, Any] | None = None) -> Any:
        return self.call(verb, params)


class Local:
    """Backend for a pipeline in this process. No serialisation anywhere."""

    def __init__(self, runtime: Runtime) -> None:
        self._session = runtime.session()

    def call(self, verb: str, params: dict[str, Any] | None = None) -> Any:
        return self._session.call(verb, params)

    def events(self, timeout: float | None = None) -> Iterator[Delivery]:
        subscriber = self._session.subscriber
        if subscriber is None:
            return iter(())
        return subscriber.frames(timeout)

    def close(self) -> None:
        self._session.close()


__all__ = ["Backend", "Local", "MindControl"]
