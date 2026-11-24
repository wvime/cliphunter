"""Heavy processing, only after a human approves a clip.

Reframe to 9:16 and the burned title are not run yet. The title layout lives
in ``title_overlay.TitleStyle`` and ``plan_title_spans``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ClipProcessor(Protocol):
    async def reframe_vertical(self, source: Path, dest: Path) -> Path: ...
