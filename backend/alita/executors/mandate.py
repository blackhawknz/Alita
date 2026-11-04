"""
The permission envelope for delegated work.

A :class:`Mandate` is an immutable description of what a delegated worker is
allowed to do.  It is deliberately conservative: the default is read-only, and
a caller that cannot describe a mandate gets one that may read the working
directory but may not write, run a shell or fetch the web.

The mandate is *not* a prose note.  :meth:`Mandate.to_opencode_permissions`
turns it into an OpenCode ``permission`` block, which the OpenCode client
enforces before a tool runs.  In particular, ``bash`` is never allowed while a
narrow write scope is requested: a shell can write anywhere the OS user can, so
that combination would be theatre rather than a boundary.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Sequence, Tuple

_MANDATE_FIELDS = {
    "read",
    "write",
    "allow_bash",
    "allow_webfetch",
    "allow_subagents",
    "source",
    "note",
    "created_at",
}

_SOURCES = ("user", "job", "subagent")

_FULL_WRITE_GLOBS = {
    "*",
    "**",
    "**/*",
    "./*",
    "./**",
    "./**/*",
    "/",
}

_GLOB_CHARS = ("*", "?", "[")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _posix(value: str) -> str:
    return str(value).replace("\\", "/")


def _norm_glob(value: str) -> str:
    text = _posix(value).strip()
    while text.startswith("./"):
        text = text[2:]
    return text.rstrip("/") or text


def _is_full_write(patterns: Sequence[str]) -> bool:
    """True when a write scope covers the whole working directory."""
    for pattern in patterns:
        normalized = _norm_glob(pattern)
        if normalized in _FULL_WRITE_GLOBS:
            return True
        if normalized in {"**/*", "**"}:
            return True
    return False


def _string_tuple(value: Any, *, name: str) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        # A single glob is a common mistake; accept it rather than iterating it
        # character by character.
        values: Iterable[Any] = [value]
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        raise ValueError(f"mandate.{name} must be a list of glob strings")
    out = []
    for item in values:
        if not isinstance(item, str):
            raise ValueError(f"mandate.{name} entries must be strings")
        text = item.strip()
        if not text:
            raise ValueError(f"mandate.{name} entries must not be empty")
        out.append(text)
    return tuple(out)


@dataclass(frozen=True)
class Mandate:
    """An immutable permission envelope for one delegated run."""

    # Filesystem scopes, globs relative to the workdir (absolute paths are
    # accepted only when they stay inside the workdir).
    read: Tuple[str, ...] = ()
    write: Tuple[str, ...] = ()
    # Capabilities.
    allow_bash: bool = False
    allow_webfetch: bool = False
    allow_subagents: bool = False
    # Provenance.
    source: str = "user"  # "user" | "job" | "subagent"
    note: str = ""  # human-readable only; never enforced
    created_at: str = ""

    # ── constructors ─────────────────────────────────────────────────

    @classmethod
    def read_only(cls, *, source: str = "user", note: str = "") -> "Mandate":
        """The default mandate: read the workdir, nothing else."""
        return cls(
            read=("*",),
            write=(),
            allow_bash=False,
            allow_webfetch=False,
            allow_subagents=False,
            source=source,
            note=note,
            created_at=_now(),
        )

    @classmethod
    def from_dict(cls, data: Any) -> "Mandate":
        """Build a mandate from a persisted or structured value."""
        if isinstance(data, Mandate):
            return data
        if not isinstance(data, Mapping):
            raise ValueError("mandate must be an object")
        unknown = set(data) - _MANDATE_FIELDS
        if unknown:
            raise ValueError(
                "unknown mandate key(s): " + ", ".join(sorted(str(k) for k in unknown))
            )
        return cls(
            read=_string_tuple(data.get("read"), name="read"),
            write=_string_tuple(data.get("write"), name="write"),
            allow_bash=bool(data.get("allow_bash", False)),
            allow_webfetch=bool(data.get("allow_webfetch", False)),
            allow_subagents=bool(data.get("allow_subagents", False)),
            source=str(data.get("source") or "user"),
            note=str(data.get("note") or ""),
            created_at=str(data.get("created_at") or _now()),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "read": list(self.read),
            "write": list(self.write),
            "allow_bash": self.allow_bash,
            "allow_webfetch": self.allow_webfetch,
            "allow_subagents": self.allow_subagents,
            "source": self.source,
            "note": self.note,
            "created_at": self.created_at,
        }

    # ── invariants ───────────────────────────────────────────────────

    def validate(self) -> None:
        """Raise ``ValueError`` when the mandate is self-contradictory."""
        if self.source not in _SOURCES:
            raise ValueError(
                f"mandate.source must be one of {', '.join(_SOURCES)}; "
                f"got {self.source!r}"
            )
        for name, patterns in (("read", self.read), ("write", self.write)):
            for pattern in patterns:
                if not isinstance(pattern, str) or not pattern.strip():
                    raise ValueError(f"mandate.{name} entries must be non-empty strings")
        if self.allow_bash and not _is_full_write(self.write):
            raise ValueError(
                "allow_bash=True is only valid with a full write scope "
                "(write=['*']) because a shell can write anywhere the OS user "
                "can; deny bash or accept full write explicitly"
            )

    def validate_globs(self, workdir: Path | str) -> None:
        """Reject read/write globs that can escape ``workdir``."""
        root = Path(workdir).resolve()
        for name, patterns in (("read", self.read), ("write", self.write)):
            for pattern in patterns:
                self._validate_glob(name, pattern, root)

    @staticmethod
    def _validate_glob(name: str, pattern: str, root: Path) -> None:
        text = _posix(pattern).strip()
        if not text:
            raise ValueError(f"mandate.{name} glob must not be empty")
        if text.startswith("~"):
            raise ValueError(
                f"mandate.{name} glob {pattern!r} uses '~', which can escape the workdir"
            )
        drive, _tail = os.path.splitdrive(pattern)
        if drive and not Path(pattern).is_absolute():
            raise ValueError(
                f"mandate.{name} glob {pattern!r} has a drive but is not absolute"
            )

        parts = text.split("/")
        if any(part == ".." for part in parts):
            raise ValueError(
                f"mandate.{name} glob {pattern!r} contains '..', which can escape the workdir"
            )

        if Path(pattern).is_absolute():
            static = _static_prefix(text)
            candidate = Path(static) if static else Path(text)
        else:
            static = _static_prefix(text)
            candidate = root / static if static else root

        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError):
            raise ValueError(f"mandate.{name} glob {pattern!r} cannot be resolved")
        if not _within(resolved, root):
            raise ValueError(
                f"mandate.{name} glob {pattern!r} resolves to {resolved}, "
                f"outside the workdir {root}"
            )

    def is_read_only(self) -> bool:
        """True when no capability beyond reading is requested."""
        return not (
            self.write
            or self.allow_bash
            or self.allow_webfetch
            or self.allow_subagents
        )

    # ── composition ──────────────────────────────────────────────────

    def intersect(self, other: "Mandate") -> "Mandate":
        """Return the intersection of two mandates (never a union).

        Used when a caller asks to resume a session with a fresh mandate.  A
        resumed session can be narrowed but never widened.
        """
        if not isinstance(other, Mandate):
            raise TypeError("intersect() requires another Mandate")
        read = _intersect_globs(self.read, other.read)
        write = _intersect_globs(self.write, other.write)
        allow_bash = self.allow_bash and other.allow_bash
        # If the intersection narrowed writes, bash can no longer be safely
        # combined with it (bash defeats any narrow write scope).
        if allow_bash and not _is_full_write(write):
            allow_bash = False
        return Mandate(
            read=read,
            write=write,
            allow_bash=allow_bash,
            allow_webfetch=self.allow_webfetch and other.allow_webfetch,
            allow_subagents=self.allow_subagents and other.allow_subagents,
            source=self.source if self.source == other.source else "subagent",
            note=other.note or self.note,
            created_at=_now(),
        )

    # ── OpenCode rendering ───────────────────────────────────────────

    def to_opencode_permissions(self, workdir: Path | str) -> Dict[str, Any]:
        """Render an OpenCode ``permission`` block for ``workdir``.

        Every security-relevant key is set explicitly to ``allow`` or
        ``deny``.  OpenCode's ``--dangerously-skip-permissions`` only
        auto-approves *undecided* rules, so an explicit deny cannot be
        overridden by it.
        """
        root = Path(workdir).resolve()
        root_posix = _posix(root)

        read_rules: Dict[str, str] = {"*": "deny"}
        for pattern in self.read:
            read_rules[_absolute_read_glob(root_posix, pattern)] = "allow"

        edit: Any
        if not self.write:
            edit = "deny"
        elif _is_full_write(self.write):
            edit = "allow"
        else:
            edit_rules: Dict[str, str] = {"*": "deny"}
            for pattern in self.write:
                edit_rules[_relative_edit_glob(root, pattern)] = "allow"
            edit = edit_rules

        permissions: Dict[str, Any] = {
            "read": read_rules,
            "edit": edit,
            "bash": "allow" if self.allow_bash else "deny",
            "webfetch": "allow" if self.allow_webfetch else "deny",
            "websearch": "allow" if self.allow_webfetch else "deny",
            "task": "allow" if self.allow_subagents else "deny",
            # Never inherited from a hand-written config: Alita only permits
            # access outside the workdir when a future mandate type explicitly
            # models that grant.  Today it is always denied.
            "external_directory": "deny",
        }
        return permissions


def _within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath(
            [os.path.normcase(os.path.normpath(str(path))),
             os.path.normcase(os.path.normpath(str(root)))]
        ) == os.path.normcase(os.path.normpath(str(root)))
    except ValueError:  # different drives on Windows
        return False


def _static_prefix(text: str) -> str:
    """Return the non-glob prefix of a POSIX-style path string."""
    parts = _posix(text).split("/")
    for index, part in enumerate(parts):
        if any(char in part for char in _GLOB_CHARS):
            return "/".join(parts[:index])
    return "/".join(parts)


def _absolute_read_glob(root_posix: str, pattern: str) -> str:
    text = _posix(pattern).strip()
    if Path(pattern).is_absolute():
        return text
    if text in {"*", "**", "**/*"}:
        return root_posix.rstrip("/") + "/*"
    if text.startswith("./"):
        text = text[2:]
    return root_posix.rstrip("/") + "/" + text


def _relative_edit_glob(root: Path, pattern: str) -> str:
    text = _posix(pattern).strip()
    if Path(pattern).is_absolute():
        try:
            return _posix(os.path.relpath(pattern, root))
        except (OSError, ValueError):
            return text
    if text.startswith("./"):
        text = text[2:]
    return text


def _intersect_globs(left: Sequence[str], right: Sequence[str]) -> Tuple[str, ...]:
    """Conservative glob-set intersection.

    This is intentionally not clever: when scopes differ we only keep patterns
    that are literally present on both sides, except that a full scope on one
    side lets the other side's patterns through.  The result can be narrower
    than mathematically necessary, but it can never be wider than either input.
    """
    if not left or not right:
        return ()
    if _is_full_write(left):
        return tuple(right)
    if _is_full_write(right):
        return tuple(left)
    right_norm = {_norm_glob(item) for item in right}
    return tuple(item for item in left if _norm_glob(item) in right_norm)
