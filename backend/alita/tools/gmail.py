"""
Gmail Tool — read and search emails via the Gmail API (OAuth2).

Setup:
    1. Go to https://console.cloud.google.com/apis/credentials
    2. Create an OAuth 2.0 Client ID (Desktop app)
    3. Download the credentials JSON
    4. Save it as ``~/.alita/gmail_client_secret.json``
       or set ``GMAIL_CLIENT_SECRET_PATH`` env var
    5. Set ``ALITA_GMAIL_REDIRECT_URI`` to your callback URL
       (default: ``http://localhost:8080/gmail/callback``)
    6. Start the backend, then visit ``/api/gmail/auth`` in your browser
       to authorise Alita to access your Gmail.
    7. Alita can now read and search your emails via the ``gmail`` tool.

Scopes (read-only v1):
    - gmail.readonly — read/search emails + attachments
    - gmail.compose  — draft creation (future)
    (Sending requires a separate scope + approval gate, not v1.)

Token storage:
    The OAuth refresh token is stored at ``~/.alita/gmail_token.json``.
    Delete this file to deauthorise Alita's access.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Gmail API scopes — read-only for v1
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
]


def _token_path() -> Path:
    from ..core.config import alita_home
    return alita_home() / "gmail_token.json"


def _client_secret_path() -> Path:
    from ..core.config import alita_home
    custom = os.getenv("GMAIL_CLIENT_SECRET_PATH")
    if custom:
        return Path(os.path.expanduser(custom))
    return alita_home() / "gmail_client_secret.json"


def _redirect_uri() -> str:
    return os.getenv(
        "ALITA_GMAIL_REDIRECT_URI",
        "http://localhost:8080/gmail/callback",
    )


class GmailTool:
    """
    Gmail integration tool backed by the Gmail API.
    """

    def __init__(self):
        self._service = None
        self._flow = None  # Persist the Flow across auth/callback

    @property
    def definition(self) -> Dict[str, Any]:
        return {
            "name": "gmail",
            "description": (
                "Read and search emails in the user's Gmail account. "
                "Actions: 'list' (recent inbox), 'search' (Gmail query), "
                "'read' (full email by ID), 'unread' (unread list), "
                "'attachments' (list attachment names). The user has "
                "authorised read-only access — you cannot send, delete, "
                "or modify emails."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "search", "read", "unread", "attachments"],
                        "description": "The Gmail action.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Gmail search query (for 'search'). E.g. 'from:boss@company.com subject:report has:attachment newer_than:7d'",
                    },
                    "message_id": {
                        "type": "string",
                        "description": "Gmail message ID (for 'read' or 'attachments').",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max emails to return (default 10, max 50).",
                        "default": 10,
                    },
                },
                "required": ["action"],
            },
        }

    # ─── OAuth2 ────────────────────────────────────────────────────

    def get_auth_url(self) -> Optional[str]:
        """Generate the OAuth2 authorization URL. Returns None if no client secret."""
        from google_auth_oauthlib.flow import Flow

        secret_path = _client_secret_path()
        if not secret_path.exists():
            return None

        self._flow = Flow.from_client_secrets_file(
            str(secret_path),
            scopes=SCOPES,
            redirect_uri=_redirect_uri(),
        )
        auth_url, _ = self._flow.authorization_url(
            access_type="offline",
            prompt="consent",
            include_granted_scopes="true",
        )
        return auth_url

    def exchange_code(self, auth_code: str) -> bool:
        """Exchange an authorization code for tokens (saved to disk)."""
        from google_auth_oauthlib.flow import Flow

        # Reuse the same Flow that generated the auth URL (preserves PKCE
        # verifier). If the server was restarted between auth and callback,
        # fall back to creating a new Flow (PKCE won't be available, but
        # Google may still accept the code without PKCE for Desktop apps).
        if self._flow is None:
            secret_path = _client_secret_path()
            if not secret_path.exists():
                return False
            self._flow = Flow.from_client_secrets_file(
                str(secret_path),
                scopes=SCOPES,
                redirect_uri=_redirect_uri(),
            )

        try:
            self._flow.fetch_token(code=auth_code)
        except Exception as exc:
            logger.warning("Gmail OAuth exchange failed: %s", exc)
            return False

        creds = self._flow.credentials

        token_data = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes or SCOPES),
        }
        _token_path().parent.mkdir(parents=True, exist_ok=True)
        _token_path().write_text(json.dumps(token_data, indent=2), encoding="utf-8")
        return True

    def is_authorized(self) -> bool:
        """Check if a valid token file exists."""
        return _token_path().exists()

    def _get_service(self):
        """Build the Gmail API service from stored token."""
        if self._service is not None:
            return self._service

        token_file = _token_path()
        if not token_file.exists():
            raise RuntimeError("Gmail not authorised. Visit /api/gmail/auth to authorise.")

        token_data = json.loads(token_file.read_text(encoding="utf-8"))

        from google.oauth2.credentials import Credentials
        creds = Credentials(
            token=token_data.get("token"),
            refresh_token=token_data.get("refresh_token"),
            token_uri=token_data.get("token_uri"),
            client_id=token_data.get("client_id"),
            client_secret=token_data.get("client_secret"),
            scopes=token_data.get("scopes", SCOPES),
        )

        from googleapiclient.discovery import build
        self._service = build("gmail", "v1", credentials=creds)
        return self._service

    # ─── Tool execution ────────────────────────────────────────────

    async def execute(
        self,
        action: str,
        query: str = "",
        message_id: str = "",
        max_results: int = 10,
        **kwargs,
    ) -> Dict[str, Any]:
        """Execute a Gmail action."""
        try:
            service = self._get_service()
        except RuntimeError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": f"Gmail API error: {e}"}

        try:
            if action == "list":
                return self._list_messages(service, max_results)
            elif action == "search":
                if not query:
                    return {"success": False, "error": "query is required for search"}
                return self._search_messages(service, query, max_results)
            elif action == "read":
                if not message_id:
                    return {"success": False, "error": "message_id is required for read"}
                return self._read_message(service, message_id)
            elif action == "unread":
                return self._list_unread(service, max_results)
            elif action == "attachments":
                if not message_id:
                    return {"success": False, "error": "message_id is required for attachments"}
                return self._list_attachments(service, message_id)
            else:
                return {"success": False, "error": f"Unknown action: {action}"}
        except Exception as e:
            return {"success": False, "error": f"Gmail action failed: {e}"}

    # ─── Actions ────────────────────────────────────────────────────

    def _list_messages(self, service, max_results: int) -> Dict[str, Any]:
        """List recent inbox messages."""
        max_results = min(max(1, max_results), 50)
        results = service.users().messages().list(
            userId="me", maxResults=max_results,
        ).execute()
        messages = results.get("messages", [])
        return self._summarize_messages(service, messages)

    def _list_unread(self, service, max_results: int) -> Dict[str, Any]:
        """List unread messages."""
        max_results = min(max(1, max_results), 50)
        results = service.users().messages().list(
            userId="me", labelIds=["INBOX", "UNREAD"], maxResults=max_results,
        ).execute()
        messages = results.get("messages", [])
        if not messages:
            return {"success": True, "emails": [], "count": 0, "message": "No unread emails."}
        return self._summarize_messages(service, messages)

    def _search_messages(self, service, query: str, max_results: int) -> Dict[str, Any]:
        """Search messages using Gmail query syntax."""
        max_results = min(max(1, max_results), 50)
        results = service.users().messages().list(
            userId="me", q=query, maxResults=max_results,
        ).execute()
        messages = results.get("messages", [])
        if not messages:
            return {"success": True, "emails": [], "count": 0,
                    "message": f"No emails matched query: {query}"}
        return self._summarize_messages(service, messages)

    def _read_message(self, service, message_id: str) -> Dict[str, Any]:
        """Read a single email in full."""
        msg = service.users().messages().get(
            userId="me", id=message_id, format="full",
        ).execute()

        headers = msg.get("payload", {}).get("headers", [])
        subject = next((h["value"] for h in headers if h["name"] == "Subject"), "(no subject)")
        sender = next((h["value"] for h in headers if h["name"] == "From"), "(unknown)")
        to = next((h["value"] for h in headers if h["name"] == "To"), "")
        date = next((h["value"] for h in headers if h["name"] == "Date"), "")

        body_text = self._extract_body(msg.get("payload", {}))

        return {
            "success": True,
            "message_id": message_id,
            "subject": subject,
            "from": sender,
            "to": to,
            "date": date,
            "body": body_text[:20000],  # Cap to keep context bounded
            "snippet": msg.get("snippet", ""),
        }

    def _list_attachments(self, service, message_id: str) -> Dict[str, Any]:
        """List attachment metadata for an email."""
        msg = service.users().messages().get(
            userId="me", id=message_id, format="full",
        ).execute()
        attachments = []
        parts = self._find_attachment_parts(msg.get("payload", {}))
        for part in parts:
            attachments.append({
                "filename": part.get("filename", ""),
                "mime_type": part.get("mimeType", ""),
                "size": part.get("body", {}).get("size", 0),
                "attachment_id": part.get("body", {}).get("attachmentId", ""),
            })
        return {
            "success": True,
            "message_id": message_id,
            "attachments": attachments,
            "count": len(attachments),
        }

    # ─── Helpers ───────────────────────────────────────────────────

    def _summarize_messages(self, service, messages: List[Dict]) -> Dict[str, Any]:
        """Batch-fetch message details for a list of message IDs."""
        if not messages:
            return {"success": True, "emails": [], "count": 0}

        emails = []
        for msg_ref in messages[:50]:
            msg_id = msg_ref["id"]
            try:
                # Use metadata format for speed — only headers, no body
                msg = service.users().messages().get(
                    userId="me", id=msg_id, format="metadata",
                    metadataHeaders=["Subject", "From", "Date"],
                ).execute()
                headers = msg.get("payload", {}).get("headers", [])
                subject = next((h["value"] for h in headers if h["name"] == "Subject"), "(no subject)")
                sender = next((h["value"] for h in headers if h["name"] == "From"), "(unknown)")
                date = next((h["value"] for h in headers if h["name"] == "Date"), "")
                emails.append({
                    "id": msg_id,
                    "subject": subject,
                    "from": sender,
                    "date": date,
                    "snippet": msg.get("snippet", "")[:200],
                    "unread": "UNREAD" in msg.get("labelIds", []),
                })
            except Exception as e:
                logger.warning("Gmail: failed to fetch message %s: %s", msg_id, e)
                continue
        return {"success": True, "emails": emails, "count": len(emails)}

    def _extract_body(self, payload: Dict) -> str:
        """Extract plain text body from a Gmail message payload."""
        mime_type = payload.get("mimeType", "")
        body = payload.get("body", {})

        # If this part has the data directly
        if body.get("data"):
            data = base64.urlsafe_b64decode(body["data"])
            try:
                return data.decode("utf-8", errors="replace")
            except Exception:
                return ""

        # If multipart, look for text/plain or text/html parts
        if mime_type.startswith("multipart"):
            parts = payload.get("parts", [])
            # Prefer plain text
            for part in parts:
                if part.get("mimeType") == "text/plain":
                    text = self._extract_body(part)
                    if text.strip():
                        return text
            # Fall back to HTML, strip tags
            for part in parts:
                if part.get("mimeType") == "text/html":
                    html = self._extract_body(part)
                    if html.strip():
                        # Basic HTML tag removal
                        import re
                        text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
                        text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
                        text = re.sub(r"<[^>]+>", " ", text)
                        text = re.sub(r"&nbsp;", " ", text)
                        text = re.sub(r"&amp;", "&", text)
                        text = re.sub(r"&lt;", "<", text)
                        text = re.sub(r"&gt;", ">", text)
                        text = re.sub(r"\s+", " ", text).strip()
                        return text
            # Recurse into nested multipart
            for part in parts:
                text = self._extract_body(part)
                if text.strip():
                    return text
        return ""

    def _find_attachment_parts(self, payload: Dict) -> List[Dict]:
        """Recursively find all parts with attachment filenames."""
        attachments = []
        if payload.get("filename"):
            attachments.append(payload)
        for part in payload.get("parts", []):
            attachments.extend(self._find_attachment_parts(part))
        return attachments