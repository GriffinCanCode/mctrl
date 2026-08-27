"""mindcontrol: camera-driven hand and gaze control for macOS."""

from .logs import quiet

# Before anything pulls in MediaPipe: its loggers read these levels from the
# environment as their C++ extension modules load, so a later call would be too
# late to have any effect.
quiet()

__version__ = "0.1.0"
