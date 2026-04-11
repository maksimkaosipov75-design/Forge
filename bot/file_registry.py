"""
Short-ID registry for file paths used in Telegram callback_data.

Telegram limits callback_data to 64 bytes.  Absolute file paths easily
exceed that limit, so we register each path under a 12-char SHA-1 prefix
and store the full path in memory.

The registry is module-level (one per process).  It evicts the oldest half
of entries once it reaches _MAX_SIZE, which is more than enough for any
realistic interactive session.
"""

from __future__ import annotations

import hashlib

_registry: dict[str, str] = {}   # short_id → absolute_path
_MAX_SIZE = 500


def register(path: str) -> str:
    """Return a stable 12-char ID for *path*, registering it if needed.

    The returned ID is always 12 ASCII hex chars, so
    ``"view_file:" + register(path)`` is always 22 bytes — well within the
    64-byte Telegram limit.
    """
    fid = hashlib.sha1(path.encode()).hexdigest()[:12]
    if fid not in _registry:
        if len(_registry) >= _MAX_SIZE:
            # Evict the first half (oldest insertions — dict preserves order)
            keys_to_drop = list(_registry)[:_MAX_SIZE // 2]
            for k in keys_to_drop:
                del _registry[k]
        _registry[fid] = path
    return fid


def resolve(fid: str) -> str | None:
    """Return the full path for *fid*, or None if it is not in the registry."""
    return _registry.get(fid)
