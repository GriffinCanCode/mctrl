"""Muffling library logging without losing it when it matters."""

from __future__ import annotations

import os
import subprocess
import sys

from mindcontrol.logs import QUIET_ENV, VERBOSE_VAR, quiet

# Run in a subprocess: this manipulates file descriptor 2, and pytest's own
# capture also owns it, so doing it in-process would test the harness instead.
SCRIPT = """
import sys
from mindcontrol.logs import muffled
{body}
"""


def _run(body: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", SCRIPT.format(body=body)],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        check=False,
    )


def test_noise_is_swallowed_on_success():
    result = _run(
        "with muffled():\n"
        "    print('NOISE', file=sys.stderr)\n"
        "    sys.stderr.flush()\n"
        "print('done')\n"
    )
    assert "NOISE" not in result.stderr
    assert "done" in result.stdout


def test_noise_is_returned_when_something_fails():
    """The one time this logging is wanted is when initialisation broke."""
    result = _run(
        "try:\n"
        "    with muffled():\n"
        "        print('DIAGNOSTIC', file=sys.stderr)\n"
        "        sys.stderr.flush()\n"
        "        raise RuntimeError('boom')\n"
        "except RuntimeError:\n"
        "    pass\n"
    )
    assert "DIAGNOSTIC" in result.stderr


def test_stderr_survives_the_round_trip():
    """A leaked redirection would silence the whole process from then on."""
    result = _run(
        "with muffled():\n"
        "    pass\n"
        "print('AFTER', file=sys.stderr)\n"
        "sys.stderr.flush()\n"
    )
    assert "AFTER" in result.stderr


def test_verbose_disables_muffling():
    result = _run(
        "with muffled():\n"
        "    print('KEPT', file=sys.stderr)\n"
        "    sys.stderr.flush()\n",
        env={VERBOSE_VAR: "1"},
    )
    assert "KEPT" in result.stderr


def test_exceptions_still_propagate():
    result = _run("with muffled():\n    raise ValueError('propagate me')\n")
    assert result.returncode != 0
    assert "propagate me" in result.stderr


def test_quiet_sets_levels():
    quiet()
    for name in QUIET_ENV:
        assert name in os.environ


def test_quiet_respects_an_explicit_choice(monkeypatch):
    """Someone who exported a level meant it."""
    # glog's variable really is mixed case; capitalising it stops working.
    name = "GLOG_minloglevel"
    monkeypatch.setenv(name, "0")
    monkeypatch.delenv(VERBOSE_VAR, raising=False)
    quiet()
    assert os.environ[name] == "0"


def test_quiet_does_nothing_when_verbose(monkeypatch):
    monkeypatch.setenv(VERBOSE_VAR, "1")
    for name in QUIET_ENV:
        monkeypatch.delenv(name, raising=False)
    quiet()
    assert not any(name in os.environ for name in QUIET_ENV)
