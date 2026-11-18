"""Post-approval processors. The title overlay is specified, not rendered."""

from cliphunter.processing.base import ClipProcessor
from cliphunter.processing.title_overlay import PendingTitleOverlay, TitleSpan, TitleStyle, plan_title_spans

__all__ = ["ClipProcessor", "PendingTitleOverlay", "TitleSpan", "TitleStyle", "plan_title_spans"]
