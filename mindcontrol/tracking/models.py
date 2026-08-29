"""MediaPipe task-model bundles.

MediaPipe 1.x ships no bundled models, so the ``.task`` files are fetched once
into the user cache on first run and reused from then on.
"""

from __future__ import annotations

import shutil
import urllib.error
import urllib.request
from pathlib import Path

from ..config import CACHE_DIR

MODELS_DIR = CACHE_DIR / "models"

_BASE = "https://storage.googleapis.com/mediapipe-models"
SOURCES: dict[str, str] = {
    name: f"{_BASE}/{stem}/{stem}/float16/latest/{name}"
    for stem, name in (
        ("hand_landmarker", "hand_landmarker.task"),
        ("face_landmarker", "face_landmarker.task"),
    )
}


class ModelUnavailableError(RuntimeError):
    """A required model is neither cached nor downloadable."""


def ensure(name: str) -> Path:
    """Return the local path to a task bundle, downloading it if needed."""
    if name not in SOURCES:
        raise KeyError(f"unknown model {name!r}")
    target = MODELS_DIR / name
    if target.is_file() and target.stat().st_size > 0:
        return target

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    url = SOURCES[name]
    print(f"[models] downloading {name} ...")
    staging = target.with_suffix(target.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=60) as response, staging.open("wb") as out:
            shutil.copyfileobj(response, out)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        staging.unlink(missing_ok=True)
        raise ModelUnavailableError(
            f"could not download {name} from {url}: {exc}. "
            f"Download it manually and place it at {target}."
        ) from exc
    staging.replace(target)
    print(f"[models] cached {name} ({target.stat().st_size // 1024} KiB)")
    return target


def ensure_all() -> dict[str, Path]:
    return {name: ensure(name) for name in SOURCES}
