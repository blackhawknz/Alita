"""
Away mode state.

Away mode pauses autonomous work (scheduled jobs, kanban, external
delegation) while leaving chat fully usable. The manual toggle is persisted in
the shared SQLite store's ``app_settings`` table so it survives a restart.
Automatic away mode is derived lazily from the most recent chat activity — no
background timer is involved.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

_TRUTHY = {"1", "true", "yes", "on"}


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class AwayState:
    """Read and persist away-mode state.

    ``settings`` may be an :class:`AgentSettings` instance or a callable that
    returns the current one (settings are replaced when the UI reloads them).
    """

    MANUAL_KEY = "away_mode"
    SINCE_KEY = "away_since"

    def __init__(self, sqlite: Any = None, settings: Any = None):
        self.sqlite = sqlite
        self._settings = settings

    def _settings_obj(self) -> Any:
        settings = self._settings
        return settings() if callable(settings) else settings

    def _stored(self, key: str) -> Optional[str]:
        if self.sqlite is None:
            return None
        try:
            return self.sqlite.get_setting(key)
        except Exception:  # noqa: BLE001
            return None

    def _store(self, key: str, value: str) -> None:
        if self.sqlite is None:
            return
        try:
            self.sqlite.set_setting(key, value)
        except Exception:  # noqa: BLE001
            pass

    # ─── Manual toggle ────────────────────────────────────────────

    def manual_enabled(self) -> bool:
        stored = self._stored(self.MANUAL_KEY)
        if stored is not None:
            return str(stored).strip().lower() in _TRUTHY
        return bool(getattr(self._settings_obj(), "away_mode", False))

    def set_manual(self, enabled: bool, now: Optional[datetime] = None) -> None:
        enabled = bool(enabled)
        self._store(self.MANUAL_KEY, "1" if enabled else "0")
        if enabled:
            stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
            self._store(self.SINCE_KEY, stamp)
        else:
            self._store(self.SINCE_KEY, "")

    # ─── Automatic away ───────────────────────────────────────────

    @property
    def auto_after_hours(self) -> int:
        try:
            return max(0, int(getattr(self._settings_obj(), "away_auto_after_hours", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def latest_chat_updated_at(self) -> Optional[datetime]:
        if self.sqlite is None:
            return None
        try:
            row = self.sqlite.query_one(
                "SELECT MAX(updated_at) AS ts FROM sessions WHERE kind = 'chat'"
            )
        except Exception:  # noqa: BLE001
            return None
        return _parse_timestamp(row.get("ts") if row else None)

    def auto_active(self, now: Optional[datetime] = None) -> bool:
        hours = self.auto_after_hours
        if hours <= 0:
            return False
        latest = self.latest_chat_updated_at()
        if latest is None:
            return False
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
        return current - latest > timedelta(hours=hours)

    # ─── Combined state ───────────────────────────────────────────

    def is_away(self, now: Optional[datetime] = None) -> bool:
        try:
            return self.manual_enabled() or self.auto_active(now)
        except Exception:  # noqa: BLE001
            return False

    def since(self, now: Optional[datetime] = None) -> str:
        if not self.is_away(now):
            return ""
        if self.manual_enabled():
            stored = self._stored(self.SINCE_KEY)
            if stored:
                return stored
        latest = self.latest_chat_updated_at()
        if latest is None:
            return ""
        return (latest + timedelta(hours=self.auto_after_hours)).isoformat()

    def status(self, now: Optional[datetime] = None) -> dict:
        return {
            "away_mode": self.is_away(now),
            "auto_after_hours": self.auto_after_hours,
            "auto_active": self.auto_active(now),
            "since": self.since(now),
        }
