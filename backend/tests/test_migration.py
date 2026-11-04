"""Schema v1 -> v2 migration of state.db and legacy persona clean-up."""

import sqlite3

from alita.core.prompt_builder import clean_legacy_persona
from alita.memory.sqlite_store import SCHEMA_VERSION, SQLiteStore


def _make_v1_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE conversations (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('user','assistant','system','tool')), content TEXT NOT NULL,
            tool_calls TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, metadata TEXT);
        CREATE TABLE sessions (id TEXT PRIMARY KEY, summary TEXT, status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, closed_at TIMESTAMP);
        CREATE VIRTUAL TABLE conversations_fts USING fts5(content, session_id, content_rowid='id',
            tokenize='porter unicode61');
        CREATE TRIGGER conversations_ai AFTER INSERT ON conversations BEGIN
            INSERT INTO conversations_fts(rowid, content, session_id) VALUES (new.id, new.content, new.session_id);
        END;
        INSERT INTO sessions (id, summary) VALUES ('chat1', ''), ('job1', 'Scheduled job: abc'), ('empty', '');
        INSERT INTO conversations (session_id, role, content) VALUES
            ('chat1', 'user', 'How do I bake sourdough bread?'),
            ('chat1', 'user', 'How do I bake sourdough bread?'),
            ('chat1', 'assistant', ''),
            ('chat1', 'tool', '{"success": true}'),
            ('chat1', 'assistant', 'Start with a starter.'),
            ('chat1', 'assistant', 'Start with a starter.'),
            ('job1', 'user', 'daily summary');
        """
    )
    conn.commit()
    conn.close()


def test_v1_database_is_cleaned_up(tmp_path):
    path = str(tmp_path / "state.db")
    _make_v1_db(path)
    store = SQLiteStore(path)
    sessions = {s["id"]: s for s in store.list_sessions(kind=None)}
    assert set(sessions) == {"chat1", "job1"}  # empty session pruned
    assert sessions["job1"]["kind"] == "job"
    assert sessions["chat1"]["kind"] == "chat"
    assert sessions["chat1"]["title"] == "How do I bake sourdough bread?"
    msgs = store.load_messages("chat1")
    assert [(m["role"], m["content"]) for m in msgs] == [
        ("user", "How do I bake sourdough bread?"), ("assistant", "Start with a starter.")]
    hits = store.search_messages("sourdough")
    assert len(hits) == 1 and hits[0]["session_id"] == "chat1"
    assert store.query_one("PRAGMA user_version")["user_version"] == SCHEMA_VERSION
    store.close()
    # Re-opening is idempotent.
    again = SQLiteStore(path)
    assert len(again.load_messages("chat1")) == 2
    again.close()


def test_existing_database_gains_external_handles_without_data_loss(tmp_path):
    path = str(tmp_path / "state.db")
    _make_v1_db(path)

    store = SQLiteStore(path)
    store.save_external_handle("ses_1", "opencode", str(tmp_path), cost=0.2)
    record = store.load_external_handle("ses_1")
    assert record["cost"] == 0.2
    # Pre-existing rows are untouched by the additive table.
    assert len(store.load_messages("chat1")) == 2
    store.close()

    reopened = SQLiteStore(path)
    assert reopened.load_external_handle("ses_1")["cost"] == 0.2
    reopened.close()


def test_clean_legacy_persona_strips_generated_sections():
    text = (
        "You are Alita.\n\n## Core Principles\n- Be kind\n\n## Memory Usage\n- generated\n\n"
        "## Environment\n- Working directory: X\n\n## Memory Usage\n- generated again\n"
        "\n---\n## Custom Chat Instructions\nTalk like a pirate."
    )
    persona, custom = clean_legacy_persona(text)
    assert "Core Principles" in persona and "Be kind" in persona
    assert "Memory Usage" not in persona and "Working directory" not in persona
    assert custom == "Talk like a pirate."
