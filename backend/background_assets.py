"""Resolve the small runtime pack for source and PyInstaller builds."""
import os
import sys
import mimetypes
from pathlib import Path

# Some Windows registries do not associate WebP with an image MIME type.
mimetypes.add_type("image/webp", ".webp")


def background_directory() -> Path:
    configured = os.getenv("STORY_ENGINE_BACKGROUND_DIR")
    if configured:
        return Path(configured)
    if getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS) / "backgrounds"
    return Path(__file__).with_name("backgrounds")
