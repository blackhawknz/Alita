"""Context variables carrying per-turn state into tool handlers."""

from contextvars import ContextVar
from typing import Any, Optional

#: The :class:`~alita.core.session.Session` whose turn is currently executing.
current_session: ContextVar[Optional[Any]] = ContextVar("alita_current_session", default=None)
