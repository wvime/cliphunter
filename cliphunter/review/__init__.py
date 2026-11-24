"""Human review. Telegram when it is configured, localhost page as fallback."""

from cliphunter.review.base import Decision, Notifier, ReviewBackend
from cliphunter.review.localapp import LocalNotifier
from cliphunter.review.telegram import TelegramNotifier

__all__ = ["Decision", "LocalNotifier", "Notifier", "ReviewBackend", "TelegramNotifier"]
