"""``mindcontrol api`` -- the API from a shell.

Which makes it the API from any language, including the ones with no socket
library worth using: a subprocess and a pipe are enough to read hands or move
the cursor. It is also the fastest way to see whether the app is answering at
all, which is why it prints the catalogue when asked for nothing.

Everything it writes to stdout is JSON, one document per call and one object per
line while watching, so its output is meant to be piped rather than read.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from . import contract as api
from .client import Client
from .contract import ApiError


def run(
    verb: str | None = None,
    params: list[str] | None = None,
    *,
    watch: str | None = None,
    seconds: float | None = None,
    socket: Path | None = None,
) -> int:
    """Call one verb, or watch streams, against a running MindControl."""
    if verb is None and watch is None:
        return _summarise()

    try:
        arguments = _arguments(params or [])
    except ValueError as problem:
        print(f"[api] {problem}", file=sys.stderr)
        return 2

    try:
        with Client(socket).open() as client:
            if client.error:
                print(f"[api] {client.error}", file=sys.stderr)
            if verb is not None:
                print(json.dumps(client.call(verb, arguments), indent=2, sort_keys=True))
            if watch is not None:
                _watch(client, watch, seconds)
    except ApiError as problem:
        print(f"[api] {problem.code}: {problem.message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


def _summarise() -> int:
    """The catalogue, without needing the app to be running to see it."""
    print(json.dumps(api.catalogue(), indent=2, sort_keys=True))
    return 0


def bind(
    gesture: str | None = None,
    action: str | None = None,
    *,
    app: str | None = None,
    clear: bool = False,
    socket: Path | None = None,
) -> int:
    """``mindcontrol bind`` -- read or rewrite the gesture bindings.

    Written for a human rather than a pipe, unlike the rest of this module: it is
    the one thing here somebody runs to set the program up rather than to drive
    it. Everything it does is also reachable as ``bindings.get``, ``bindings.set``
    and ``bindings.clear`` for anything that wants JSON.
    """
    try:
        with Client(socket).open() as client:
            if gesture is None:
                answer = client.call("bindings.get", {"app": app} if app else None)
            else:
                params: dict[str, Any] = {"gesture": gesture}
                if app:
                    params["app"] = app
                if clear or action is None:
                    answer = client.call("bindings.clear", params)
                else:
                    params["action"] = action
                    answer = client.call("bindings.set", params)
            _print_bindings(answer, asked=app)
    except ApiError as problem:
        print(f"[bind] {problem.code}: {problem.message}", file=sys.stderr)
        return 1
    return 0


def _print_bindings(table: dict[str, Any], asked: str | None = None) -> None:
    """The table as a human reads it: what each gesture does, and where from.

    ``asked`` distinguishes the two questions this can answer -- what a named
    application does, and what the one in front of you does -- because a report
    that said "in front" for both would be wrong half the time.
    """
    resolved = table.get("resolved") or {}
    apps = table.get("apps") or {}
    default = table.get("default") or {}
    scope = table.get("scope")
    scoped = apps.get(scope or "", {})

    if asked:
        subject = f"for {asked}"
    else:
        front = table.get("app") or {}
        subject = f"in front: {front.get('name') or front.get('bundle') or 'nothing'}"
    print(subject + (f"  (matching [bindings.{scope}])" if scope else ""))

    for gesture in table.get("gestures", ()):
        action = resolved.get(gesture)
        if gesture in scoped:
            # A scoped binding of `none` is a deliberate silence, and reads as
            # unbound unless it is named as the other thing it is.
            origin = f"[{scope}]" + ("  muted" if action is None else "")
        elif gesture in default:
            origin = ""
        else:
            origin = "(unbound)"
        print(f"  {gesture:<16} {action or '-':<20} {origin}".rstrip())

    for name, rows in sorted(apps.items()):
        if name == scope:
            continue
        listed = ", ".join(f"{key}={value}" for key, value in sorted(rows.items()))
        print(f"  [{name}] {listed}")


def _watch(client: Client, streams: str, seconds: float | None) -> None:
    names = [name.strip() for name in streams.split(",") if name.strip()]
    client.call("tracking.subscribe", {"streams": names} if names else None)
    deadline = None if seconds is None else time.monotonic() + seconds
    for delivery in client.events(timeout=0.5 if deadline else None):
        encode = getattr(delivery.payload, "to_json", None)
        line = {
            "stream": delivery.stream,
            "data": encode() if callable(encode) else delivery.payload,
        }
        if delivery.dropped:
            line["dropped"] = delivery.dropped
        print(json.dumps(line, separators=(",", ":")), flush=True)
        if deadline is not None and time.monotonic() >= deadline:
            return


def _arguments(params: list[str]) -> dict[str, Any] | None:
    """Turn ``key=value`` pairs into parameters, reading each value as JSON.

    So ``dx=40`` is a number, ``mode=active`` is a string, and
    ``streams=["hands"]`` is a list, without a flag per type.
    """
    if not params:
        return None
    arguments: dict[str, Any] = {}
    for pair in params:
        key, separator, raw = pair.partition("=")
        if not separator:
            raise ValueError(f"{pair!r} is not key=value")
        try:
            arguments[key] = json.loads(raw)
        except json.JSONDecodeError:
            arguments[key] = raw
    return arguments
