"""Bearer-token store. Persisted as JSONL — one row per token.

Stored fields: {token_sha256, prefix, tier, owner_email, created_at, revoked}.
The plaintext token is shown to the user once at issue time and never
written to disk.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

Tier = Literal["demo", "issued"]


def generate_token() -> str:
    """Return a new plaintext token of the form pyth_<8hex>_<32hex>."""
    prefix = secrets.token_hex(4)
    suffix = secrets.token_hex(16)
    return f"pyth_{prefix}_{suffix}"


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


@dataclass
class Token:
    sha256: str
    prefix: str
    tier: Tier
    owner_email: str
    created_at: float
    revoked: bool = False


class TokenStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._tokens: dict[str, Token] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                tok = Token(
                    sha256=row["token_sha256"],
                    prefix=row["prefix"],
                    tier=row["tier"],
                    owner_email=row["owner_email"],
                    created_at=row["created_at"],
                    revoked=bool(row.get("revoked", False)),
                )
                self._tokens[tok.sha256] = tok
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning("skipping malformed token row: %s", exc)

    def _append(self, tok: Token) -> None:
        row = {
            "token_sha256": tok.sha256,
            "prefix": tok.prefix,
            "tier": tok.tier,
            "owner_email": tok.owner_email,
            "created_at": tok.created_at,
            "revoked": tok.revoked,
        }
        with self.path.open("a") as f:
            f.write(json.dumps(row) + "\n")

    def issue(self, *, owner_email: str, tier: Tier) -> str:
        plain = generate_token()
        prefix = plain.split("_")[1]
        sha = _sha256(plain)
        tok = Token(
            sha256=sha,
            prefix=prefix,
            tier=tier,
            owner_email=owner_email,
            created_at=time.time(),
        )
        with self._lock:
            self._tokens[sha] = tok
            self._append(tok)
        return plain

    def verify(self, plain: str) -> Token | None:
        if not plain or not plain.startswith("pyth_"):
            return None
        sha = _sha256(plain)
        tok = self._tokens.get(sha)
        if tok is None or tok.revoked:
            return None
        return tok

    def revoke(self, prefix: str) -> bool:
        """Revoke the (single) token with this prefix. Returns True if found."""
        with self._lock:
            for tok in self._tokens.values():
                if tok.prefix == prefix and not tok.revoked:
                    tok.revoked = True
                    self._append(tok)
                    return True
        return False

    def list(self) -> list[Token]:
        return list(self._tokens.values())
