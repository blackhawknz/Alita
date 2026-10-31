"""
Chat attachments.

Uploaded files are stored in ``~/.alita/uploads/<id>/`` (with a ``meta.json``)
and copied into ``<workspace>/attachments/`` so file tools can work on them.

User message content stores images as ``upload://<id>`` image parts; these
are resolved to base64 data URLs only when a request is sent to the model.
Text-like files are inlined (capped) so any model can read them.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_INLINE_CHARS = 120_000
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".xml", ".html", ".htm", ".css", ".js", ".jsx", ".ts", ".tsx", ".py", ".rb", ".go", ".rs", ".java", ".kt",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".sh", ".ps1", ".bat", ".sql", ".log", ".env", ".r", ".swift",
    ".vue", ".svelte", ".lua", ".pl", ".dart", ".scala", ".tex",
}
IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def _safe_name(name: str) -> str:
    name = Path(name or "file").name
    name = re.sub(r"[^\w.\- ]+", "_", name).strip() or "file"
    return name[:120]


def save_upload(name: str, data: bytes, mime: Optional[str] = None, workspace: Optional[str] = None) -> Dict[str, Any]:
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"File too large ({len(data) // 1024 // 1024} MB; max {MAX_UPLOAD_BYTES // 1024 // 1024} MB)")
    upload_id = uuid.uuid4().hex[:16]
    safe = _safe_name(name)
    mime = mime or mimetypes.guess_type(safe)[0] or "application/octet-stream"
    folder = config.uploads_dir() / upload_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / safe
    path.write_bytes(data)
    kind = "image" if mime in IMAGE_MIMES else ("text" if _is_text(safe, mime, data) else "file")
    workspace_path = ""
    if workspace:
        dest_dir = Path(workspace) / "attachments"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / safe
        if dest.exists():
            dest = dest_dir / f"{upload_id[:6]}_{safe}"
        shutil.copyfile(path, dest)
        workspace_path = str(dest)
    meta = {"id": upload_id, "name": safe, "mime": mime, "size": len(data), "kind": kind,
            "workspace_path": workspace_path}
    (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return meta


def _is_text(name: str, mime: str, data: bytes) -> bool:
    if Path(name).suffix.lower() in TEXT_EXTENSIONS or mime.startswith("text/"):
        return True
    try:
        data[:4096].decode("utf-8")
        return b"\x00" not in data[:4096]
    except UnicodeDecodeError:
        return False


def get_upload(upload_id: str) -> Optional[Tuple[Path, Dict[str, Any]]]:
    if not re.fullmatch(r"[0-9a-f]{16}", upload_id or ""):
        return None
    folder = config.uploads_dir() / upload_id
    meta_path = folder / "meta.json"
    if not meta_path.exists():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    path = folder / meta["name"]
    return (path, meta) if path.exists() else None


def build_user_content(text: str, attachment_ids: List[str]) -> Any:
    """Return plain text, or multimodal parts when attachments are present."""
    if not attachment_ids:
        return text
    parts: List[Dict[str, Any]] = []
    notes: List[str] = []
    for upload_id in attachment_ids:
        found = get_upload(upload_id)
        if not found:
            continue
        path, meta = found
        where = f" (saved at {meta['workspace_path']})" if meta.get("workspace_path") else ""
        if meta["kind"] == "image":
            parts.append({"type": "image_url", "image_url": {"url": f"upload://{upload_id}"},
                          "_name": meta["name"]})
            notes.append(f"[Attached image: {meta['name']}{where}]")
        elif meta["kind"] == "text":
            body = path.read_text(encoding="utf-8", errors="replace")
            truncated = len(body) > MAX_INLINE_CHARS
            body = body[:MAX_INLINE_CHARS]
            trunc_attr = ' truncated="true"' if truncated else ""
            notes.append(f'<attachment name="{meta["name"]}"{trunc_attr}>{where}\n{body}\n</attachment>')
        else:
            notes.append(f"[Attached file: {meta['name']} ({meta['mime']}, {meta['size']} bytes){where} — "
                         "use tools to read or convert it]")
    content_text = text
    if notes:
        content_text = (text + "\n\n" if text else "") + "\n\n".join(notes)
    return [{"type": "text", "text": content_text}] + parts


def resolve_for_llm(messages: List[Dict[str, Any]]) -> None:
    """In-place: turn ``upload://`` image refs into data URLs and drop private keys from parts."""
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        new_parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            part = {k: v for k, v in part.items() if not k.startswith("_")}
            if part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                if url.startswith("upload://"):
                    found = get_upload(url[len("upload://"):])
                    if not found:
                        new_parts.append({"type": "text", "text": "[image no longer available]"})
                        continue
                    path, meta = found
                    data = base64.b64encode(path.read_bytes()).decode("ascii")
                    part = {"type": "image_url", "image_url": {"url": f"data:{meta['mime']};base64,{data}"}}
            new_parts.append(part)
        m["content"] = new_parts
