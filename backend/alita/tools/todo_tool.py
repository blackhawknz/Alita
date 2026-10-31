"""
TodoStore -- in-memory task tracking for agent task planning.

Supports incremental edits (add, update, delete) as well as writing the whole
list at once via :meth:`TodoStore.replace_all`, which is how modern agents
usually maintain their plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

#: Allowed todo statuses.
VALID_STATUSES = ("pending", "in_progress", "completed", "cancelled")

_STATUS_ORDER = {"in_progress": 0, "pending": 1, "completed": 2, "cancelled": 3}
_STATUS_ALIASES = {
    "todo": "pending",
    "not_started": "pending",
    "in-progress": "in_progress",
    "inprogress": "in_progress",
    "active": "in_progress",
    "doing": "in_progress",
    "done": "completed",
    "complete": "completed",
    "canceled": "cancelled",
}
_MAX_TITLE = 500


def _now() -> str:
    return datetime.now().isoformat()


def normalize_status(status: Any) -> str:
    """Return a canonical status or raise ``ValueError`` for unknown values."""
    if not isinstance(status, str):
        raise ValueError(f"Invalid status {status!r}; must be one of {', '.join(VALID_STATUSES)}")
    s = status.strip().lower().replace(" ", "_")
    s = _STATUS_ALIASES.get(s, s)
    if s not in VALID_STATUSES:
        raise ValueError(f"Invalid status {status!r}; must be one of {', '.join(VALID_STATUSES)}")
    return s


def _validate_title(title: Any) -> str:
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title must be a non-empty string")
    return title.strip()[:_MAX_TITLE]


@dataclass
class TodoItem:
    """A single todo item."""

    id: int
    title: str
    status: str = "pending"  # pending | in_progress | completed | cancelled
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class TodoStore:
    """In-memory todo list for agent task planning."""

    def __init__(self, max_items: int = 50):
        self._todos: Dict[int, TodoItem] = {}
        self._next_id = 1
        self.max_items = max_items

    def add(self, title: str, status: str = "pending") -> TodoItem:
        """Add a new todo item.

        When the list is full, the oldest completed/cancelled item is evicted;
        if there is none, ``ValueError`` is raised.
        """
        title = _validate_title(title)
        status = normalize_status(status)
        if len(self._todos) >= self.max_items:
            finished = [t for t in self._todos.values() if t.status in ("completed", "cancelled")]
            if not finished:
                raise ValueError(
                    f"Todo list is full ({self.max_items} items); complete, cancel or delete items first"
                )
            oldest = min(finished, key=lambda t: (t.updated_at, t.id))
            del self._todos[oldest.id]

        item = TodoItem(id=self._next_id, title=title, status=status)
        self._todos[item.id] = item
        self._next_id += 1
        return item

    def update(self, todo_id: int, **kwargs: Any) -> Optional[TodoItem]:
        """Update ``title`` and/or ``status`` of a todo. Raises ValueError on bad values."""
        item = self._todos.get(todo_id)
        if not item:
            return None
        if "status" in kwargs and kwargs["status"] is not None:
            item.status = normalize_status(kwargs["status"])
        if "title" in kwargs and kwargs["title"] is not None:
            item.title = _validate_title(kwargs["title"])
        item.updated_at = _now()
        return item

    def replace_all(self, items: Iterable[Dict[str, Any]]) -> List[TodoItem]:
        """Replace the whole list with ``items`` (each ``{"title", "status"?, "id"?}``).

        Validation happens before anything is changed, so a bad entry leaves
        the current list intact. Items keep their ``id`` when provided (and
        unique); otherwise new ids are assigned. ``created_at`` is preserved
        for ids that already existed.
        """
        entries = list(items or [])
        if len(entries) > self.max_items:
            raise ValueError(f"Too many todos ({len(entries)}); the maximum is {self.max_items}")

        parsed: List[Dict[str, Any]] = []
        used_ids = set()
        for i, entry in enumerate(entries):
            if isinstance(entry, str):
                entry = {"title": entry}
            if not isinstance(entry, dict):
                raise ValueError(f"Todo #{i + 1} must be an object with a 'title'")
            title = _validate_title(entry.get("title") or entry.get("content"))
            status = normalize_status(entry.get("status", "pending"))
            todo_id = entry.get("id")
            if isinstance(todo_id, bool) or not isinstance(todo_id, int) or todo_id <= 0 or todo_id in used_ids:
                todo_id = None
            else:
                used_ids.add(todo_id)
            parsed.append({"id": todo_id, "title": title, "status": status})

        old = self._todos
        next_id = max([self._next_id - 1, *used_ids], default=0) + 1
        now = _now()
        new: Dict[int, TodoItem] = {}
        for p in parsed:
            todo_id = p["id"]
            if todo_id is None:
                todo_id = next_id
                next_id += 1
            prev = old.get(todo_id)
            changed = prev is None or prev.title != p["title"] or prev.status != p["status"]
            new[todo_id] = TodoItem(
                id=todo_id,
                title=p["title"],
                status=p["status"],
                created_at=prev.created_at if prev else now,
                updated_at=now if changed else prev.updated_at,
            )
        self._todos = new
        self._next_id = next_id
        return list(new.values())

    def get(self, todo_id: int) -> Optional[TodoItem]:
        return self._todos.get(todo_id)

    def list(self, status: Optional[str] = None) -> List[TodoItem]:
        """List todos (in_progress, pending, completed, cancelled), optionally filtered."""
        todos = list(self._todos.values())
        if status:
            wanted = normalize_status(status)
            todos = [t for t in todos if t.status == wanted]
        todos.sort(key=lambda t: (_STATUS_ORDER.get(t.status, 99), t.id))
        return todos

    def delete(self, todo_id: int) -> bool:
        """Delete a todo item."""
        if todo_id in self._todos:
            del self._todos[todo_id]
            return True
        return False

    def clear(self) -> None:
        """Remove every todo."""
        self._todos.clear()

    def clear_completed(self) -> int:
        """Remove all completed/cancelled todos. Returns count removed."""
        to_remove = [
            tid for tid, t in self._todos.items()
            if t.status in ("completed", "cancelled")
        ]
        for tid in to_remove:
            del self._todos[tid]
        return len(to_remove)

    def to_context_string(self) -> str:
        """Render the todo list as a context string for the system prompt."""
        items = self.list()
        if not items:
            return "No active todos."
        markers = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]", "cancelled": "[-]"}
        lines = ["## Active Todos"]
        for item in items:
            lines.append(f"{markers.get(item.status, '[?]')} [{item.id}] {item.title} ({item.status})")
        return "\n".join(lines)

    def to_dict_list(self) -> List[Dict[str, Any]]:
        """Serialise todos in insertion order (round-trips with ``from_dict_list``)."""
        return [t.to_dict() for t in self._todos.values()]

    def from_dict_list(self, data: List[Dict[str, Any]]) -> None:
        """Restore todos from :meth:`to_dict_list` output. Invalid entries are skipped."""
        self._todos.clear()
        max_id = 0
        for d in data or []:
            if not isinstance(d, dict):
                continue
            try:
                todo_id = int(d["id"])
                title = _validate_title(d.get("title"))
                status = normalize_status(d.get("status", "pending"))
            except (KeyError, TypeError, ValueError):
                continue
            created = d.get("created_at") or _now()
            item = TodoItem(
                id=todo_id,
                title=title,
                status=status,
                created_at=created,
                updated_at=d.get("updated_at") or created,
            )
            self._todos[item.id] = item
            max_id = max(max_id, item.id)
        self._next_id = max_id + 1

    @property
    def count(self) -> int:
        return len(self._todos)

    @property
    def active_count(self) -> int:
        return sum(1 for t in self._todos.values() if t.status in ("pending", "in_progress"))
