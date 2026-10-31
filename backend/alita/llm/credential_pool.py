"""
Credential Pool — Round-robin API key rotation with cooldown on rate limits.

Usage:
    pool = CredentialPool(["key1", "key2", "key3"])
    key = pool.get_available()
    if key is None:
        raise RuntimeError("All API keys exhausted")
    try:
        await call_api(key)
    except RateLimitError:
        pool.mark_exhausted(key, cooldown_seconds=60)

Multiple keys can be supplied via environment variables as comma-separated values:
    OPENAI_API_KEY=key1,key2,key3
"""

import os
import time
from typing import Dict, List, Optional


class CredentialPool:
    """
    Sticky pool of API keys with per-key cooldown on exhaustion.

    ``get_available()`` keeps returning the same (current) key until it is
    rate-limited (``mark_exhausted``) or the caller explicitly ``rotate()``s,
    so callers can cache per-key HTTP clients. Exhausted keys are skipped
    until their cooldown expires.

    Intended for use from a single event loop (no locking).
    """

    def __init__(self, keys: List[str], default_cooldown: int = 60) -> None:
        self._keys: List[str] = [k.strip() for k in keys if k and k.strip()]
        self._exhausted: Dict[str, float] = {}  # key -> expiry timestamp
        self._current_idx: int = 0
        self.default_cooldown: int = default_cooldown

    # ── Internals ────────────────────────────────────────────────────────────

    def _purge_expired(self) -> None:
        now = time.monotonic()
        self._exhausted = {k: exp for k, exp in self._exhausted.items() if exp > now}

    def _select_from(self, start: int) -> Optional[str]:
        """Return the first non-exhausted key at or after ``start`` and make it current."""
        n = len(self._keys)
        for offset in range(n):
            idx = (start + offset) % n
            key = self._keys[idx]
            if key not in self._exhausted:
                self._current_idx = idx
                return key
        return None

    # ── Public API ───────────────────────────────────────────────────────────

    def get_available(self) -> Optional[str]:
        """
        Return the current API key, skipping keys that are cooling down.

        Does not advance the rotation: repeated calls return the same key
        until it is exhausted or ``rotate()`` is called. Returns None if all
        keys are currently rate-limited.
        """
        if not self._keys:
            return None
        self._purge_expired()
        return self._select_from(self._current_idx)

    @property
    def current(self) -> Optional[str]:
        """Alias for :meth:`get_available`."""
        return self.get_available()

    def mark_exhausted(self, key: str, cooldown_seconds: Optional[int] = None) -> None:
        """Mark a key as rate-limited; it is skipped until its cooldown expires.

        If ``key`` is the current key, the rotation advances to the next one.
        """
        cooldown = cooldown_seconds if cooldown_seconds is not None else self.default_cooldown
        self._exhausted[key] = time.monotonic() + cooldown
        if self._keys and self._keys[self._current_idx % len(self._keys)] == key:
            self._current_idx = (self._current_idx + 1) % len(self._keys)

    def rotate(self) -> Optional[str]:
        """Advance to the next available key and return it (None if all exhausted)."""
        if not self._keys:
            return None
        self._purge_expired()
        return self._select_from((self._current_idx + 1) % len(self._keys))

    # ── Properties ──────────────────────────────────────────────────────────

    @property
    def total(self) -> int:
        """Total number of keys in the pool (including exhausted)."""
        return len(self._keys)

    @property
    def available_count(self) -> int:
        """Number of keys that are not currently rate-limited."""
        now = time.monotonic()
        return sum(1 for k in self._keys if self._exhausted.get(k, 0) <= now)

    @property
    def is_empty(self) -> bool:
        """True if the pool has no keys configured."""
        return len(self._keys) == 0

    def __repr__(self) -> str:
        return (
            f"CredentialPool(total={self.total}, "
            f"available={self.available_count}, "
            f"exhausted={len(self._exhausted)})"
        )


def pool_from_env(env_var: str, separator: str = ",") -> CredentialPool:
    """
    Build a CredentialPool from a potentially comma-separated environment variable.

    Example:
        OPENAI_API_KEY=sk-key1,sk-key2  →  CredentialPool(["sk-key1", "sk-key2"])
        OPENAI_API_KEY=sk-key1          →  CredentialPool(["sk-key1"])
    """
    raw = os.getenv(env_var, "")
    keys = [k.strip() for k in raw.split(separator) if k.strip()]
    return CredentialPool(keys)


def pool_from_value(value: str, separator: str = ",") -> CredentialPool:
    """Build a CredentialPool from a potentially comma-separated string value."""
    keys = [k.strip() for k in value.split(separator) if k.strip()]
    return CredentialPool(keys)
