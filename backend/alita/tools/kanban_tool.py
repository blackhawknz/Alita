"""
Kanban Tool — agent-side tool for managing Kanban cards.

Exposes a ``kanban`` tool to the Alita agent so it can create, list, and
inspect cards while working on tasks. This enables the agent to break a
complex parent task into sub-tasks and spawn child cards in the 'Idea'
column for the user to curate.

Agent restrictions (enforced here, not in the store):
    * ``add_card``: agent may only create cards in the 'Idea' column.
    * ``move_card``: agent may NOT move cards between columns (human-only).
    * ``delete_card``: agent may NOT delete cards (human-only).
    * ``update_card``: agent may update description/tags/priority of cards
      it created, but not status/column/result.

The human-only operations (move, delete, advance columns) are performed via
the REST API / drag-and-drop UI, not through this tool.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..kanban.store import KanbanStore, COLUMNS
from .registry import Tool

_MAX_TITLE = 200
_MAX_TAGS = 20


def _validate_priority(value: Any) -> Tuple[Optional[int], Optional[str]]:
    """Return (priority, error). Accepts ints and integral strings; rejects bools."""
    if isinstance(value, bool):
        return None, "priority must be an integer"
    if isinstance(value, int):
        return value, None
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip()), None
    if isinstance(value, float) and value.is_integer():
        return int(value), None
    return None, "priority must be an integer"


def _validate_tags(value: Any) -> Tuple[Optional[List[str]], Optional[str]]:
    """Return (tags, error). Tags must be a list of non-empty strings."""
    if value is None:
        return [], None
    if not isinstance(value, list) or not all(isinstance(t, str) for t in value):
        return None, "tags must be a list of strings"
    tags = [t.strip() for t in value if t.strip()]
    if len(tags) > _MAX_TAGS:
        return None, f"at most {_MAX_TAGS} tags are allowed"
    return tags, None


def _validate_title(value: Any) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(value, str) or not value.strip():
        return None, "title is required and must be a non-empty string"
    title = value.strip()
    if len(title) > _MAX_TITLE:
        return None, f"title must be at most {_MAX_TITLE} characters"
    return title, None


KANBAN_TOOL_SCHEMA = {
    "name": "kanban",
    "description": (
        "Manage Kanban board cards. Create sub-task cards in the 'Idea' "
        "column, list cards across the board, or inspect a specific card. "
        "You can only create cards in the 'Idea' column — the human curates "
        "them before execution. Use this to break complex tasks into trackable, "
        "parallelisable sub-tasks."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add_card", "list_cards", "get_card", "update_card"],
                "description": "The kanban action to perform.",
            },
            "title": {
                "type": "string",
                "description": "Card title (for add_card).",
            },
            "description": {
                "type": "string",
                "description": "Full task description / prompt (for add_card or update_card).",
            },
            "card_id": {
                "type": "string",
                "description": "Card ID (for get_card / update_card).",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tags for the card (e.g. [\"coding\", \"research\"]).",
            },
            "priority": {
                "type": "integer",
                "description": "Sort priority within the column (lower = higher).",
                "default": 0,
            },
            "column": {
                "type": "string",
                "enum": COLUMNS,
                "description": "Column to list cards from (list_cards).",
            },
        },
        "required": ["action"],
    },
}


def make_kanban_tool(store: KanbanStore) -> Tool:
    """Build the kanban tool bound to a KanbanStore."""

    async def _handler(**kwargs) -> Dict[str, Any]:
        action = kwargs.get("action", "")

        if action == "add_card":
            title, err = _validate_title(kwargs.get("title"))
            if err:
                return {"success": False, "error": f"{err} (add_card)"}
            description = kwargs.get("description") or ""
            if not isinstance(description, str):
                return {"success": False, "error": "description must be a string"}
            tags, err = _validate_tags(kwargs.get("tags"))
            if err:
                return {"success": False, "error": err}
            priority, err = _validate_priority(kwargs.get("priority", 0))
            if err:
                return {"success": False, "error": err}
            # Agent may only create cards in the 'idea' column.
            card = store.create_card(
                title=title,
                description=description,
                column="idea",
                priority=priority,
                tags=tags,
            )
            return {"success": True, "card": card.to_dict()}

        elif action == "list_cards":
            column = kwargs.get("column")
            if column and column not in COLUMNS:
                return {"success": False, "error": f"Unknown column {column!r}; valid: {COLUMNS}"}
            cards = store.list_cards(column=column) if column else store.list_cards()
            return {
                "success": True,
                "cards": [c.to_dict() for c in cards],
                "count": len(cards),
            }

        elif action == "get_card":
            card_id = kwargs.get("card_id", "")
            if not card_id:
                return {"success": False, "error": "card_id is required for get_card"}
            card = store.get_card(card_id)
            if card is None:
                return {"success": False, "error": f"Card {card_id} not found"}
            return {"success": True, "card": card.to_dict()}

        elif action == "update_card":
            card_id = kwargs.get("card_id", "")
            if not card_id:
                return {"success": False, "error": "card_id is required for update_card"}
            card = store.get_card(card_id)
            if card is None:
                return {"success": False, "error": f"Card {card_id} not found"}
            # Agent can only update description, tags, priority — NOT column, status, result.
            updates: Dict[str, Any] = {}
            if kwargs.get("description") is not None:
                if not isinstance(kwargs["description"], str):
                    return {"success": False, "error": "description must be a string"}
                updates["description"] = kwargs["description"]
            if kwargs.get("title") is not None:
                title, err = _validate_title(kwargs["title"])
                if err:
                    return {"success": False, "error": err}
                updates["title"] = title
            if kwargs.get("tags") is not None:
                tags, err = _validate_tags(kwargs["tags"])
                if err:
                    return {"success": False, "error": err}
                updates["tags"] = tags
            if kwargs.get("priority") is not None:
                priority, err = _validate_priority(kwargs["priority"])
                if err:
                    return {"success": False, "error": err}
                updates["priority"] = priority
            if not updates:
                return {"success": False, "error": "No updatable fields provided"}
            updated = store.update_card(card_id, **updates)
            return {"success": True, "card": updated.to_dict() if updated else None}

        else:
            return {"success": False, "error": f"Unknown action: {action}"}

    return Tool(
        name="kanban",
        description="Manage Kanban board cards: create sub-task cards, list cards, inspect or update card details.",
        definition=KANBAN_TOOL_SCHEMA,
        handler=_handler,
        tags=["planning", "kanban"],
    )