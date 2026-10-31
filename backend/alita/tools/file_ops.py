"""
File Operations Tool -- workspace-confined read/write/patch/list/search with
checkpoint rollback.

Every path is resolved and verified to live inside the workspace (symlinks
resolved, case-insensitive on Windows). Mutating actions record a checkpoint
that preserves each file's path relative to the workspace, so ``rollback``
restores files to their original locations (and removes files that did not
exist when the checkpoint was taken).
"""

from __future__ import annotations

import asyncio
import difflib
import fnmatch
import json
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

#: Directories never copied into checkpoints nor scanned by search.
IGNORED_DIRS = frozenset({
    ".git", ".alita_checkpoints", ".sandboxes", "venv", ".venv", "node_modules",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
})

#: Internal directories hidden from ``list`` at the workspace root.
_INTERNAL_DIRS = frozenset({".alita_checkpoints", ".sandboxes"})

MAX_READ_BYTES = 200_000
DEFAULT_READ_LIMIT = 2_000
MAX_LIST_ENTRIES = 500
MAX_SEARCH_RESULTS = 200
MAX_SEARCH_FILES = 20_000
MAX_SEARCH_FILE_BYTES = 2_000_000
MAX_CHECKPOINT_FILES = 2_000
MAX_CHECKPOINT_BYTES = 100 * 1024 * 1024
KEEP_CHECKPOINTS = 50

_CHECKPOINT_ID_RE = re.compile(r"^[0-9A-Za-z_]+$")


# ─── Path confinement (shared with database_tool / git_tools) ──────


def is_within(path: Path, base: Path) -> bool:
    """True if resolved ``path`` equals or is inside resolved ``base``."""
    p = Path(os.path.normcase(str(path)))
    b = Path(os.path.normcase(str(base)))
    return p == b or p.is_relative_to(b)


def resolve_within(base: Path, rel_path: Optional[str]) -> Path:
    """Resolve ``rel_path`` against ``base`` and refuse anything outside it.

    Absolute paths are accepted only if they point inside ``base``.
    Raises ``ValueError`` on traversal.
    """
    base_resolved = Path(base).resolve()
    raw = (rel_path or "").strip()
    candidate = Path(raw) if raw else Path(".")
    if not candidate.is_absolute():
        candidate = base_resolved / candidate
    resolved = candidate.resolve()
    if not is_within(resolved, base_resolved):
        raise ValueError(f"Path traversal denied: {rel_path}")
    return resolved


def _is_binary(path: Path, probe: int = 8192) -> bool:
    try:
        with open(path, "rb") as fh:
            return b"\x00" in fh.read(probe)
    except OSError:
        return True


def _normalise_ws(text: str) -> Tuple[str, List[int]]:
    """
    Collapse horizontal whitespace runs and drop trailing whitespace.

    Returns the normalised text plus a map from each normalised character to
    its index in the original string, so a normalised match can be translated
    back into a byte-for-byte replacement.
    """
    out: List[str] = []
    mapping: List[int] = []
    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        if ch in " \t":
            j = i
            while j < length and text[j] in " \t":
                j += 1
            if j >= length or text[j] in "\r\n":
                i = j
                continue
            out.append(" ")
            mapping.append(i)
            i = j
            continue
        out.append(ch)
        mapping.append(i)
        i += 1
    return "".join(out), mapping


def _normalise_line(line: str) -> str:
    return re.sub(r"[ \t]+", " ", line).strip()


def _closest_region(current: str, old: str) -> Dict[str, Any]:
    """Find the file region most similar to ``old`` for a failed patch."""
    lines = current.splitlines()
    if not lines:
        return {"line": 1, "text": ""}
    old_lines = old.splitlines() or [old]
    window = max(1, len(old_lines))
    wanted = [_normalise_line(line) for line in old_lines]
    best_ratio = -1.0
    best_start = 0
    wanted_text = "\n".join(wanted)
    for start in range(max(1, len(lines) - window + 1)):
        candidate = [_normalise_line(line) for line in lines[start:start + window]]
        ratio = difflib.SequenceMatcher(None, wanted_text, "\n".join(candidate)).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_start = start
    snippet = lines[max(0, best_start - 1):best_start + 2]
    return {
        "line": best_start + 1,
        "text": "\n".join(snippet),
        "similarity": round(max(0.0, best_ratio), 2),
    }


def _walk_files(root: Path) -> Iterator[Path]:
    """Yield regular files under ``root`` (sorted), skipping IGNORED_DIRS and symlinks."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        for name in sorted(filenames):
            f = Path(dirpath) / name
            if not f.is_symlink():
                yield f


# ─── Checkpoints ───────────────────────────────────────────────────


class FileCheckpoint:
    """File checkpoints that preserve workspace-relative paths.

    Layout: ``.alita_checkpoints/<id>/manifest.json`` plus
    ``.alita_checkpoints/<id>/files/<relative path>``.
    """

    def __init__(self, base_path: Path):
        self.base_path = Path(base_path).resolve()
        self.checkpoint_dir = self.base_path / ".alita_checkpoints"

    def _collect(self, paths: List[Path]) -> List[Tuple[str, Path, bool]]:
        """Expand ``paths`` into (relative posix path, absolute path, existed)."""
        entries: Dict[str, Tuple[str, Path, bool]] = {}
        total_bytes = 0
        for p in paths:
            p = Path(p)
            if not p.is_absolute():
                p = self.base_path / p
            p = p.resolve()
            if not is_within(p, self.base_path):
                raise ValueError(f"Cannot checkpoint path outside workspace: {p}")
            files: List[Tuple[Path, bool]]
            if p.is_dir():
                files = [(f, True) for f in _walk_files(p)]
            else:
                files = [(p, p.exists())]
            for f, existed in files:
                rel = f.relative_to(self.base_path).as_posix()
                if any(part in IGNORED_DIRS for part in rel.split("/")):
                    continue
                if existed:
                    total_bytes += f.stat().st_size
                entries[rel] = (rel, f, existed)
                if len(entries) > MAX_CHECKPOINT_FILES or total_bytes > MAX_CHECKPOINT_BYTES:
                    raise ValueError(
                        "Checkpoint too large (limit "
                        f"{MAX_CHECKPOINT_FILES} files / {MAX_CHECKPOINT_BYTES // (1024 * 1024)} MB); "
                        "checkpoint a narrower path."
                    )
        return list(entries.values())

    def create(self, paths: List[Path]) -> str:
        """Checkpoint the given files/directories and return the checkpoint id."""
        entries = self._collect(paths)
        checkpoint_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex[:6]}"
        cp_path = self.checkpoint_dir / checkpoint_id
        files_dir = cp_path / "files"
        files_dir.mkdir(parents=True, exist_ok=True)

        manifest = []
        for rel, src, existed in entries:
            if existed:
                dest = files_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
            manifest.append({"path": rel, "existed": existed})
        (cp_path / "manifest.json").write_text(
            json.dumps({"version": 2, "created_at": datetime.now().isoformat(), "entries": manifest}),
            encoding="utf-8",
        )
        self.cleanup(KEEP_CHECKPOINTS)
        return checkpoint_id

    def rollback(self, checkpoint_id: str) -> bool:
        """Restore files from a checkpoint. Returns False if it does not exist."""
        if not checkpoint_id or not _CHECKPOINT_ID_RE.match(checkpoint_id):
            return False
        cp_path = self.checkpoint_dir / checkpoint_id
        if not cp_path.is_dir():
            return False
        manifest_path = cp_path / "manifest.json"
        if not manifest_path.exists():
            return self._legacy_rollback(cp_path)

        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        for entry in data.get("entries", []):
            rel = entry.get("path", "")
            dest = resolve_within(self.base_path, rel)
            if entry.get("existed"):
                src = cp_path / "files" / rel
                if not src.exists():
                    continue
                if dest.is_dir():
                    shutil.rmtree(dest)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
            elif dest.is_file():
                dest.unlink()
        return True

    def _legacy_rollback(self, cp_path: Path) -> bool:
        """Restore pre-manifest checkpoints (stored by basename at the root)."""
        for item in cp_path.iterdir():
            dest = resolve_within(self.base_path, item.name)
            if dest.exists():
                if dest.is_dir():
                    shutil.rmtree(dest)
                else:
                    dest.unlink()
            if item.is_dir():
                shutil.copytree(item, dest, symlinks=True)
            else:
                shutil.copy2(item, dest)
        return True

    def describe(self, checkpoint_id: str) -> List[str]:
        """Return the relative paths covered by a checkpoint."""
        manifest_path = self.checkpoint_dir / checkpoint_id / "manifest.json"
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            return [e.get("path", "") for e in data.get("entries", [])]
        except (OSError, ValueError):
            return []

    def list_checkpoints(self) -> List[str]:
        """List checkpoint ids, newest first."""
        if not self.checkpoint_dir.exists():
            return []
        return sorted(
            [d.name for d in self.checkpoint_dir.iterdir() if d.is_dir()],
            reverse=True,
        )

    def cleanup(self, keep_last: int = 10) -> None:
        """Remove old checkpoints, keeping the most recent ``keep_last``."""
        for old in self.list_checkpoints()[keep_last:]:
            shutil.rmtree(self.checkpoint_dir / old, ignore_errors=True)


# ─── Tool ──────────────────────────────────────────────────────────


class FileOps:
    """Workspace-confined file operations with checkpointing for rollback."""

    ACTIONS = (
        "read", "write", "patch", "list", "search", "mkdir", "delete",
        "checkpoint", "rollback", "list_checkpoints",
    )

    def __init__(self, workspace_dir: Optional[str] = None):
        self.workspace = Path(workspace_dir or os.getcwd()).resolve()
        self.checkpointer = FileCheckpoint(self.workspace)

    @property
    def definition(self) -> Dict[str, Any]:
        return {
            "name": "file_ops",
            "description": (
                "Workspace file operations. Paths are relative to the workspace root. "
                "Actions: read (paged by lines via offset/limit, 200KB cap), write "
                "(create/overwrite), patch (replace old_string with new_string; old_string "
                "must match exactly once unless replace_all=true), list (one directory, "
                "optional glob `pattern`), search (regex `pattern` across files, optional "
                "`glob` filter), mkdir, delete (files only), checkpoint, rollback "
                "(checkpoint_id), list_checkpoints. write/patch/delete return a "
                "checkpoint_id that rollback can restore."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": list(self.ACTIONS),
                        "description": "The file operation to perform.",
                    },
                    "path": {
                        "type": "string",
                        "description": "File or directory path relative to the workspace (default: root for list/search).",
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to write (write action).",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "Exact text to replace (patch action).",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Replacement text (patch action).",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "patch: replace every occurrence instead of requiring a unique match.",
                        "default": False,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "read: 1-based line number to start from (default 1).",
                        "default": 1,
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"read: maximum number of lines to return (default {DEFAULT_READ_LIMIT}).",
                        "default": DEFAULT_READ_LIMIT,
                    },
                    "pattern": {
                        "type": "string",
                        "description": "list: glob on entry names (e.g. '*.py'); search: regex (falls back to literal text if invalid).",
                    },
                    "glob": {
                        "type": "string",
                        "description": "search: only scan files whose name or relative path matches this glob (e.g. '*.ts').",
                    },
                    "ignore_case": {
                        "type": "boolean",
                        "description": "search: case-insensitive matching.",
                        "default": False,
                    },
                    "checkpoint_id": {
                        "type": "string",
                        "description": "Checkpoint ID for the rollback action.",
                    },
                },
                "required": ["action"],
            },
        }

    # ─── Helpers ───────────────────────────────────────────────────

    def _resolve_path(self, rel_path: str) -> Path:
        """Resolve a workspace path, refusing anything outside the workspace."""
        return resolve_within(self.workspace, rel_path)

    def _rel(self, path: Path) -> str:
        try:
            rel = path.relative_to(self.workspace).as_posix()
        except ValueError:
            return str(path)
        return rel or "."

    # ─── Dispatch ──────────────────────────────────────────────────

    async def execute(
        self,
        action: str = "",
        path: str = "",
        content: str = "",
        old_string: str = "",
        new_string: str = "",
        checkpoint_id: str = "",
        offset: int = 1,
        limit: int = DEFAULT_READ_LIMIT,
        pattern: str = "",
        glob: str = "",
        replace_all: bool = False,
        ignore_case: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Execute a file operation in a worker thread; extra kwargs are ignored."""
        path = path or kwargs.get("file_path") or ""
        try:
            return await asyncio.to_thread(
                self._dispatch, action, path, content, old_string, new_string,
                checkpoint_id, offset, limit, pattern, glob, replace_all, ignore_case,
            )
        except Exception as e:  # noqa: BLE001 - surface to the model
            return {"success": False, "error": str(e)}

    def _dispatch(
        self, action: str, path: str, content: str, old_string: str, new_string: str,
        checkpoint_id: str, offset: Any, limit: Any, pattern: str, glob: str,
        replace_all: Any, ignore_case: Any,
    ) -> Dict[str, Any]:
        action = (action or "").strip().lower()
        if action == "read":
            return self._read(path, offset, limit)
        if action == "write":
            return self._write(path, content)
        if action == "patch":
            return self._patch(path, old_string, new_string, _as_bool(replace_all))
        if action == "list":
            return self._list_dir(path, pattern)
        if action == "search":
            return self._search(pattern, glob, path, _as_bool(ignore_case))
        if action == "mkdir":
            return self._mkdir(path)
        if action == "delete":
            return self._delete(path)
        if action == "checkpoint":
            return self._checkpoint(path)
        if action == "rollback":
            return self._rollback(checkpoint_id)
        if action == "list_checkpoints":
            ids = self.checkpointer.list_checkpoints()
            return {"success": True, "checkpoints": ids[:50], "count": len(ids)}
        return {
            "success": False,
            "error": f"Unknown action: {action!r}. Valid actions: {', '.join(self.ACTIONS)}",
        }

    # ─── Actions ───────────────────────────────────────────────────

    def _read(self, path: str, offset: Any = 1, limit: Any = DEFAULT_READ_LIMIT) -> Dict[str, Any]:
        if not path:
            return {"success": False, "error": "path is required for read"}
        filepath = self._resolve_path(path)
        if not filepath.exists():
            return {"success": False, "error": f"File not found: {path}"}
        if filepath.is_dir():
            return {"success": False, "error": f"{path} is a directory; use action='list'"}
        if _is_binary(filepath):
            return {
                "success": False,
                "error": f"{path} appears to be a binary file ({filepath.stat().st_size} bytes)",
            }
        start = max(1, _as_int(offset, 1))
        max_lines = max(1, _as_int(limit, DEFAULT_READ_LIMIT))

        selected: List[str] = []
        selected_bytes = 0
        total_lines = 0
        truncated = False
        next_offset: Optional[int] = None
        # Universal newlines: CRLF is shown as "\n" (patch maps it back on write).
        with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, start=1):
                total_lines = lineno
                if lineno < start or truncated:
                    continue
                if len(selected) >= max_lines:
                    truncated = True
                    next_offset = lineno
                    continue
                size = len(line.encode("utf-8", errors="replace"))
                if selected_bytes + size > MAX_READ_BYTES:
                    if not selected:  # single enormous line: return a slice of it
                        selected.append(line[:MAX_READ_BYTES])
                        next_offset = lineno + 1
                    else:
                        next_offset = lineno
                    truncated = True
                    continue
                selected.append(line)
                selected_bytes += size

        result: Dict[str, Any] = {
            "success": True,
            "path": str(filepath),
            "content": "".join(selected),
            "size_bytes": filepath.stat().st_size,
            "lines": total_lines,
            "offset": start,
            "lines_returned": len(selected),
            "truncated": truncated,
        }
        if truncated and next_offset is not None:
            result["next_offset"] = next_offset
            result["message"] = (
                f"Output truncated; call read again with offset={next_offset} to continue."
            )
        if start > total_lines and total_lines > 0:
            result["message"] = f"offset {start} is past the end of the file ({total_lines} lines)."
        return result

    def _write(self, path: str, content: Any) -> Dict[str, Any]:
        if not path:
            return {"success": False, "error": "path is required for write"}
        if content is None:
            content = ""
        if not isinstance(content, str):
            content = json.dumps(content, indent=2) if isinstance(content, (dict, list)) else str(content)
        filepath = self._resolve_path(path)
        if filepath.is_dir():
            return {"success": False, "error": f"{path} is a directory"}
        existed = filepath.exists()
        cid = self.checkpointer.create([filepath])
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        size = len(content.encode("utf-8"))
        return {
            "success": True,
            "path": str(filepath),
            "size_bytes": size,
            "created": not existed,
            "checkpoint_id": cid,
            "message": f"{'Overwrote' if existed else 'Created'} {self._rel(filepath)} ({size} bytes)",
        }

    def _patch(self, path: str, old_string: str, new_string: str,
               replace_all: bool = False) -> Dict[str, Any]:
        if not path:
            return {"success": False, "error": "path is required for patch"}
        if not old_string:
            return {"success": False, "error": "old_string must be a non-empty string"}
        new_string = new_string or ""
        filepath = self._resolve_path(path)
        if not filepath.is_file():
            return {"success": False, "error": f"File not found: {path}"}

        with open(filepath, "r", encoding="utf-8", newline="") as fh:
            current = fh.read()
        # Match the file's line endings so LF-authored patches apply to CRLF
        # files (and vice versa) without double-converting either string.
        if "\r\n" in current:
            old_string = old_string.replace("\r\n", "\n").replace("\n", "\r\n")
            new_string = new_string.replace("\r\n", "\n").replace("\n", "\r\n")
        else:
            old_string = old_string.replace("\r\n", "\n")
            new_string = new_string.replace("\r\n", "\n")

        count = current.count(old_string)
        fuzzy = False
        if count == 0:
            normalised_current, mapping = _normalise_ws(current)
            normalised_old, _ = _normalise_ws(old_string)
            normalised_count = normalised_current.count(normalised_old) if normalised_old else 0
            if normalised_count > 1:
                return {
                    "success": False,
                    "error": (
                        f"old_string matches {normalised_count} times after normalising "
                        "whitespace; include more surrounding context."
                    ),
                    "matches": normalised_count,
                }
            if normalised_count == 1:
                start = normalised_current.find(normalised_old)
                end = start + len(normalised_old)
                start_original = mapping[start]
                end_original = mapping[end - 1] + 1
                count = 1
                fuzzy = True
            else:
                return {
                    "success": False,
                    "error": "old_string not found in file (it must match exactly, including whitespace)",
                    "matches": 0,
                    "closest": _closest_region(current, old_string),
                    "hint": (
                        "The closest region is shown above. Re-read those lines and retry "
                        "with the exact text, or use write for a larger rewrite."
                    ),
                }
        if count > 1 and not replace_all:
            return {
                "success": False,
                "error": (
                    f"old_string found {count} times; it must be unique. Include more "
                    "surrounding context or set replace_all=true."
                ),
                "matches": count,
            }

        cid = self.checkpointer.create([filepath])
        if fuzzy:
            new_content = current[:start_original] + new_string + current[end_original:]
        else:
            new_content = current.replace(old_string, new_string) if replace_all else current.replace(old_string, new_string, 1)
        with open(filepath, "w", encoding="utf-8", newline="") as fh:
            fh.write(new_content)
        result: Dict[str, Any] = {
            "success": True,
            "path": str(filepath),
            "replacements": count if replace_all else 1,
            "checkpoint_id": cid,
            "message": "Patch applied successfully",
            "diff_lines": abs(new_content.count("\n") - current.count("\n")),
        }
        if fuzzy:
            result["fuzzy"] = True
            result["note"] = "Matched after normalising whitespace."
        return result

    def _list_dir(self, path: str, pattern: str = "") -> Dict[str, Any]:
        dirpath = self._resolve_path(path) if path else self.workspace
        if not dirpath.exists():
            return {"success": False, "error": f"Directory not found: {path}"}
        if not dirpath.is_dir():
            return {"success": False, "error": f"Not a directory: {path}"}

        items: List[Dict[str, Any]] = []
        total = 0
        at_root = dirpath == self.workspace
        entries = sorted(dirpath.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        for entry in entries:
            if at_root and entry.name in _INTERNAL_DIRS:
                continue
            if pattern and not fnmatch.fnmatch(entry.name.lower(), pattern.lower()):
                continue
            total += 1
            if len(items) >= MAX_LIST_ENTRIES:
                continue
            is_dir = entry.is_dir()
            try:
                size = entry.stat().st_size if not is_dir else None
            except OSError:
                size = None
            items.append({"name": entry.name, "type": "directory" if is_dir else "file", "size": size})

        result: Dict[str, Any] = {
            "success": True,
            "path": str(dirpath),
            "items": items,
            "count": len(items),
            "truncated": total > len(items),
        }
        if total > len(items):
            result["total"] = total
        return result

    def _search(self, pattern: str, glob: str = "", path: str = "",
                ignore_case: bool = False) -> Dict[str, Any]:
        if not pattern:
            return {"success": False, "error": "pattern is required for search"}
        root = self._resolve_path(path) if path else self.workspace
        if not root.exists():
            return {"success": False, "error": f"Path not found: {path}"}
        flags = re.IGNORECASE if ignore_case else 0
        literal = False
        try:
            regex = re.compile(pattern, flags)
        except re.error:
            regex = re.compile(re.escape(pattern), flags)
            literal = True

        files = [root] if root.is_file() else _walk_files(root)
        matches: List[Dict[str, Any]] = []
        scanned = 0
        truncated = False
        for f in files:
            rel = self._rel(f)
            if glob and not (fnmatch.fnmatch(f.name, glob) or fnmatch.fnmatch(rel, glob)):
                continue
            scanned += 1
            if scanned > MAX_SEARCH_FILES:
                truncated = True
                break
            try:
                if f.stat().st_size > MAX_SEARCH_FILE_BYTES or _is_binary(f, 4096):
                    continue
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    for lineno, line in enumerate(fh, start=1):
                        if regex.search(line):
                            matches.append({"path": rel, "line": lineno, "text": line.rstrip("\r\n")[:300]})
                            if len(matches) >= MAX_SEARCH_RESULTS:
                                truncated = True
                                break
            except OSError:
                continue
            if truncated:
                break

        result: Dict[str, Any] = {
            "success": True,
            "pattern": pattern,
            "matches": matches,
            "count": len(matches),
            "files_scanned": min(scanned, MAX_SEARCH_FILES),
            "truncated": truncated,
        }
        if literal:
            result["note"] = "pattern was not a valid regex; searched as literal text"
        return result

    def _mkdir(self, path: str) -> Dict[str, Any]:
        if not path:
            return {"success": False, "error": "path is required for mkdir"}
        dirpath = self._resolve_path(path)
        if dirpath.exists() and not dirpath.is_dir():
            return {"success": False, "error": f"A file already exists at {path}"}
        existed = dirpath.is_dir()
        dirpath.mkdir(parents=True, exist_ok=True)
        return {
            "success": True,
            "path": str(dirpath),
            "created": not existed,
            "message": f"Directory {'already exists' if existed else 'created'}: {self._rel(dirpath)}",
        }

    def _checkpoint(self, path: str) -> Dict[str, Any]:
        target = self._resolve_path(path) if path else self.workspace
        if not target.exists():
            return {"success": False, "error": f"Path not found: {path}"}
        cid = self.checkpointer.create([target])
        files = self.checkpointer.describe(cid)
        return {
            "success": True,
            "checkpoint_id": cid,
            "files": len(files),
            "message": f"Checkpoint {cid} created ({len(files)} file(s))",
        }

    def _rollback(self, checkpoint_id: str) -> Dict[str, Any]:
        if not checkpoint_id:
            return {"success": False, "error": "checkpoint_id is required for rollback"}
        ok = self.checkpointer.rollback(checkpoint_id)
        result: Dict[str, Any] = {
            "success": ok,
            "message": f"Rollback {'successful' if ok else 'failed -- checkpoint not found'}",
        }
        if ok:
            result["restored"] = self.checkpointer.describe(checkpoint_id)
        else:
            result["error"] = f"Checkpoint not found: {checkpoint_id}"
        return result

    def _delete(self, path: str) -> Dict[str, Any]:
        if not path:
            return {"success": False, "error": "path is required for delete"}
        filepath = self._resolve_path(path)
        if filepath == self.workspace:
            return {"success": False, "error": "Refusing to delete the workspace root"}
        if not filepath.exists():
            return {"success": False, "error": f"Path not found: {path}"}
        if filepath.is_dir():
            return {"success": False, "error": "delete only supports files; directories cannot be deleted with file_ops"}
        cid = self.checkpointer.create([filepath])
        filepath.unlink()
        return {"success": True, "checkpoint_id": cid, "message": f"Deleted: {self._rel(filepath)}"}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
