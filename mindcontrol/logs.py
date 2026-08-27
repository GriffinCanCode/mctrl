"""Quieting the tracking stack's startup chatter.

MediaPipe, TensorFlow Lite and glog all write to stderr from C++, before any
Python logging configuration can reach them. Between them they announce the GL
version, the XNNPACK delegate and two feedback managers on every single start.

That matters most during a guided recording. The output the user is meant to read
is the prompt and the per-prompt frame counts, and six lines of library trivia
bury them.

Two mechanisms, because one is not enough:

`quiet()` sets the documented logging environment variables. On MediaPipe 0.10
these turn out to change nothing -- its logs come from absl in C++ and honour
neither the glog variables nor any Python-side logging call -- but they are the
supported interface, they are harmless, and they cover the other components and
whatever MediaPipe does next.

`muffled()` is what actually works: it redirects file descriptor 2 for the
duration of a noisy call. That is a blunt instrument, so it is aimed narrowly at
model construction and it keeps what it swallowed: on failure the captured output
is written out, because the one time this logging matters is the time something
broke.
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
from collections.abc import Iterator

# glog levels: 0=INFO, 1=WARNING, 2=ERROR, 3=FATAL. The value is the floor for
# what still prints, so 2 keeps errors and drops the rest.
QUIET_ENV = {
    "GLOG_minloglevel": "2",
    "GLOG_stderrthreshold": "2",
    "TF_CPP_MIN_LOG_LEVEL": "3",
    "ABSL_MIN_LOG_LEVEL": "2",
}

VERBOSE_VAR = "MINDCONTROL_VERBOSE"


def quiet() -> None:
    """Silence library startup logging, unless the environment says otherwise.

    Set ``MINDCONTROL_VERBOSE=1`` to see everything. Any level already exported
    is left untouched, so an explicit choice always wins over this default.
    """
    if os.environ.get(VERBOSE_VAR):
        return
    for name, level in QUIET_ENV.items():
        os.environ.setdefault(name, level)


@contextlib.contextmanager
def muffled() -> Iterator[None]:
    """Swallow C++ stderr for the duration, and give it back if anything fails.

    Only file-descriptor redirection reaches logging emitted from C++, so that is
    what this does. The descriptor is restored in a ``finally`` whatever happens,
    since leaving stderr pointing at a deleted temporary file would silence the
    whole process.
    """
    if os.environ.get(VERBOSE_VAR):
        yield
        return

    sys.stderr.flush()
    saved = os.dup(2)
    try:
        with tempfile.TemporaryFile() as sink:
            os.dup2(sink.fileno(), 2)
            try:
                yield
            finally:
                os.dup2(saved, 2)
                if sys.exc_info()[0] is not None:
                    sink.seek(0)
                    captured = sink.read().decode("utf-8", "replace")
                    if captured:
                        sys.stderr.write(captured)
                        sys.stderr.flush()
    finally:
        os.close(saved)
