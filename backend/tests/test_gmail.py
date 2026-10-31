"""
Tests for the Gmail tool (mocked Gmail API — no real Google credentials needed).

The Gmail API service object is mocked so we can exercise the tool's
action parsing, body extraction, and attachment listing without an actual
OAuth token or network access.
"""

import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from alita.tools.gmail import GmailTool


# ─── Fixtures ──────────────────────────────────────────────────────


def _make_msg(msg_id, subject, sender, date, snippet, unread=False):
    """Build a minimal Gmail message object (metadata format)."""
    headers = [
        {"name": "Subject", "value": subject},
        {"name": "From", "value": sender},
        {"name": "Date", "value": date},
    ]
    label_ids = ["INBOX"]
    if unread:
        label_ids.append("UNREAD")
    return {
        "id": msg_id,
        "snippet": snippet,
        "labelIds": label_ids,
        "payload": {"headers": headers},
    }


def _make_full_msg(msg_id, subject, sender, date, body_text, snippet=""):
    """Build a full Gmail message object with a text/plain body."""
    body_data = base64.urlsafe_b64encode(body_text.encode()).decode()
    return {
        "id": msg_id,
        "snippet": snippet,
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
                {"name": "Date", "value": date},
                {"name": "To", "value": "me@example.com"},
            ],
            "body": {"data": body_data},
        },
    }


def _make_multipart_msg(msg_id, subject, sender, date, text_body, html_body,
                         attachment_filename="report.pdf", attachment_size=1024):
    """Build a multipart Gmail message with text/html and attachment."""
    text_data = base64.urlsafe_b64encode(text_body.encode()).decode()
    html_data = base64.urlsafe_b64encode(html_body.encode()).decode()
    return {
        "id": msg_id,
        "snippet": "Snippet here",
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
                {"name": "Date", "value": date},
            ],
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": text_data}},
                        {"mimeType": "text/html", "body": {"data": html_data}},
                    ],
                },
                {
                    "filename": attachment_filename,
                    "mimeType": "application/pdf",
                    "body": {"size": attachment_size, "attachmentId": "att_001"},
                },
            ],
        },
    }


@pytest.fixture
def mock_gmail_tool():
    """GmailTool with a mocked Gmail API service."""
    tool = GmailTool()
    service = MagicMock()
    tool._service = service

    # Build proper method-chaining mocks so that:
    #   service.users().messages().list(keyword=val).execute() -> dict
    #   service.users().messages().get(keyword=val).execute() -> dict
    # The key is making .list() and .get() return separate mock objects
    # whose .execute() returns the right data.
    msgs_messages = MagicMock()
    service.users().messages.return_value = msgs_messages

    list_results = {"messages": []}
    msgs_by_id = {}

    def _mock_list(**kw):
        m = MagicMock()
        m.execute.return_value = list_results
        return m

    def _mock_get(**kw):
        msg_id = kw.get("id", "")
        mv = msgs_by_id.get(msg_id, {})
        m = MagicMock()
        m.execute.return_value = mv
        return m

    msgs_messages.list.side_effect = _mock_list
    msgs_messages.get.side_effect = _mock_get

    # Expose setters for tests to configure
    tool._test_list_results = list_results
    tool._test_msgs_by_id = msgs_by_id

    return tool, service


# ─── List / Search ─────────────────────────────────────────────────


async def test_list_messages(mock_gmail_tool):
    tool, service = mock_gmail_tool
    tool._test_list_results["messages"] = [{"id": "msg1"}, {"id": "msg2"}]
    tool._test_msgs_by_id["msg1"] = _make_msg("msg1", "Hello", "boss@company.com", "Mon, 1 Jan 2026 10:00:00 +0000", "Hi there", unread=True)
    tool._test_msgs_by_id["msg2"] = _make_msg("msg2", "Report", "team@company.com", "Mon, 1 Jan 2026 11:00:00 +0000", "Weekly report")

    result = await tool.execute(action="list", max_results=10)
    assert result["success"] is True
    assert result["count"] == 2
    assert result["emails"][0]["id"] == "msg1"
    assert result["emails"][0]["subject"] == "Hello"
    assert result["emails"][0]["from"] == "boss@company.com"
    assert result["emails"][0]["unread"] is True
    assert result["emails"][1]["unread"] is False


async def test_list_messages_empty(mock_gmail_tool):
    tool, service = mock_gmail_tool
    tool._test_list_results.clear()  # empty dict = no messages
    result = await tool.execute(action="list")
    assert result["success"] is True
    assert result["count"] == 0


async def test_search_messages(mock_gmail_tool):
    tool, service = mock_gmail_tool
    tool._test_list_results["messages"] = [{"id": "msg3"}]
    tool._test_msgs_by_id["msg3"] = _make_msg("msg3", "Re: Budget", "cfo@company.com", "Tue, 2 Jan 2026 09:00:00 +0000", "Budget numbers attached")

    result = await tool.execute(action="search", query="from:cfo@company.com subject:budget")
    assert result["success"] is True
    assert result["count"] == 1
    assert result["emails"][0]["subject"] == "Re: Budget"


async def test_search_no_results(mock_gmail_tool):
    tool, service = mock_gmail_tool
    tool._test_list_results.clear()
    result = await tool.execute(action="search", query="nonexistent")
    assert result["success"] is True
    assert result["count"] == 0


async def test_search_requires_query(mock_gmail_tool):
    tool, service = mock_gmail_tool
    result = await tool.execute(action="search", query="")
    assert result["success"] is False
    assert "query is required" in result["error"]


async def test_unread_messages(mock_gmail_tool):
    tool, service = mock_gmail_tool
    tool._test_list_results["messages"] = [{"id": "msg4"}]
    tool._test_msgs_by_id["msg4"] = _make_msg("msg4", "Urgent", "boss@company.com", "Wed, 3 Jan 2026 14:00:00 +0000", "Please respond ASAP", unread=True)
    result = await tool.execute(action="unread")
    assert result["success"] is True
    assert result["count"] == 1
    assert result["emails"][0]["unread"] is True


# ─── Read ──────────────────────────────────────────────────────────


async def test_read_message(mock_gmail_tool):
    tool, service = mock_gmail_tool
    tool._test_msgs_by_id["msg5"] = _make_full_msg("msg5", "Test Email", "sender@example.com", "Thu, 4 Jan 2026 10:00:00 +0000", "This is the email body.\nHello world!")
    result = await tool.execute(action="read", message_id="msg5")
    assert result["success"] is True
    assert result["subject"] == "Test Email"
    assert result["from"] == "sender@example.com"
    assert result["to"] == "me@example.com"
    assert "This is the email body" in result["body"]
    assert "Hello world" in result["body"]


async def test_read_message_multipart(mock_gmail_tool):
    """Multipart messages should extract the text/plain part."""
    tool, service = mock_gmail_tool
    tool._test_msgs_by_id["msg6"] = _make_multipart_msg("msg6", "With Attachment", "sender@example.com", "Fri, 5 Jan 2026 12:00:00 +0000", text_body="Plain text version", html_body="<p>HTML version</p>", attachment_filename="report.pdf")
    result = await tool.execute(action="read", message_id="msg6")
    assert result["success"] is True
    assert "Plain text version" in result["body"]
    # HTML version should NOT appear when plain text is available
    assert "HTML version" not in result["body"]


async def test_read_requires_message_id(mock_gmail_tool):
    tool, _ = mock_gmail_tool
    result = await tool.execute(action="read")
    assert result["success"] is False
    assert "message_id is required" in result["error"]


# ─── Attachments ───────────────────────────────────────────────────


async def test_list_attachments(mock_gmail_tool):
    tool, service = mock_gmail_tool
    tool._test_msgs_by_id["msg7"] = _make_multipart_msg("msg7", "Report", "cfo@company.com", "Mon, 8 Jan 2026 09:00:00 +0000", text_body="See attached", html_body="<p>See attached</p>", attachment_filename="Q4_Report.pdf", attachment_size=2048576)
    result = await tool.execute(action="attachments", message_id="msg7")
    assert result["success"] is True
    assert result["count"] == 1
    att = result["attachments"][0]
    assert att["filename"] == "Q4_Report.pdf"
    assert att["size"] == 2048576
    assert att["mime_type"] == "application/pdf"
    assert att["attachment_id"] == "att_001"


async def test_list_attachments_no_attachments(mock_gmail_tool):
    tool, service = mock_gmail_tool
    tool._test_msgs_by_id["msg8"] = _make_full_msg("msg8", "No Attach", "sender@example.com", "Tue, 9 Jan 2026 10:00:00 +0000", "Just text, no files")
    result = await tool.execute(action="attachments", message_id="msg8")
    assert result["success"] is True
    assert result["count"] == 0


async def test_attachments_requires_message_id(mock_gmail_tool):
    tool, _ = mock_gmail_tool
    result = await tool.execute(action="attachments")
    assert result["success"] is False
    assert "message_id is required" in result["error"]


# ─── Unknown action ────────────────────────────────────────────────


async def test_unknown_action(mock_gmail_tool):
    tool, _ = mock_gmail_tool
    result = await tool.execute(action="send")
    assert result["success"] is False
    assert "Unknown action" in result["error"]


# ─── Not authorised ────────────────────────────────────────────────


async def test_not_authorized():
    """When no token file exists, all actions return a helpful error."""
    tool = GmailTool()
    tool._service = None  # force _get_service to read the token file
    with patch("alita.tools.gmail._token_path") as mock_path:
        mock_path.return_value = MagicMock(exists=lambda: False)
        result = await tool.execute(action="list")
    assert result["success"] is False
    assert "not authorised" in result["error"].lower()


# ─── Tool definition ──────────────────────────────────────────────


def test_definition_has_required_actions():
    tool = GmailTool()
    defn = tool.definition
    assert defn["name"] == "gmail"
    actions = defn["parameters"]["properties"]["action"]["enum"]
    assert set(actions) == {"list", "search", "read", "unread", "attachments"}


def test_definition_has_required_fields():
    tool = GmailTool()
    props = tool.definition["parameters"]["properties"]
    assert "query" in props
    assert "message_id" in props
    assert "max_results" in props
    assert "action" in tool.definition["parameters"]["required"]