"""
claw CLI - Unified command-line interface for claw Agent.
"""

import importlib.metadata
import os
from datetime import datetime
from pathlib import Path

try:
    __version__ = importlib.metadata.version("claw-agent")
except importlib.metadata.PackageNotFoundError:
    __version__ = "0.0.0-dev"

def _get_release_date():
    """
    Try to get the release date dynamically.
    1. Check package metadata for install time.
    2. Fallback to the modification time of the package directory.
    """
    try:
        # Some package managers store this, but it's not guaranteed in metadata
        # As a robust fallback for 'release date' in a dev environment:
        pkg_path = Path(__file__).parent
        mtime = os.path.getmtime(pkg_path)
        return datetime.fromtimestamp(mtime).strftime("%Y.%m.%d")
    except Exception:
        return datetime.now().strftime("%Y.%m.%d")

__release_date__ = _get_release_date()
