"""Heavy processing, only after a human approves a clip.

Phase 1 does not reframe. ``reframe_vertical`` is the 9:16 step for later.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ClipProcessor(Protocol):
    async def reframe_vertical(self, source: Path, dest: Path) -> Path: ...
