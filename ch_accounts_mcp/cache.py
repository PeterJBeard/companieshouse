"""Content-addressed, never-expiring cache.

Layout under CACHE_DIR:
    docs/<sha256>/original.pdf        exactly as downloaded, never re-saved
    docs/<sha256>/meta.json           source metadata (company, filing, URLs, retrieval time)
    docs/<sha256>/pages/<n>.*.json    per-page triage, OCR tokens per DPI
    docs/<sha256>/*.json              per-document page map, notes index, extraction results
    docs/<sha256>/crops/<id>.png      citation crops
    docs/<sha256>/tiles/<n>.png       viewer page tiles
    index.sqlite                      transaction_id -> sha, citation_id -> sha, company filings
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


class Cache:
    def __init__(self, root: Path):
        self.root = Path(root)
        (self.root / "docs").mkdir(parents=True, exist_ok=True)
        self._mem: OrderedDict[str, Any] = OrderedDict()
        self._mem_lock = threading.Lock()
        self._db_lock = threading.Lock()
        self._db = sqlite3.connect(self.root / "index.sqlite", check_same_thread=False, timeout=30)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tx_map (transaction_id TEXT PRIMARY KEY, sha TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS citation_index (citation_id TEXT PRIMARY KEY, sha TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS company_docs (company_number TEXT, made_up_date TEXT, sha TEXT, PRIMARY KEY (company_number, made_up_date, sha));
            CREATE TABLE IF NOT EXISTS rate_bucket (name TEXT PRIMARY KEY, tokens REAL, updated REAL);
            """
        )
        self._db.commit()

    # ------------------------------------------------------------------ documents

    def doc_dir(self, sha: str) -> Path:
        if not all(c in "0123456789abcdef" for c in sha) or len(sha) != 64:
            raise ValueError("invalid document hash")
        return self.root / "docs" / sha

    def pdf_path(self, sha: str) -> Path:
        return self.doc_dir(sha) / "original.pdf"

    def store_pdf(self, data: bytes) -> str:
        sha = sha256_bytes(data)
        p = self.pdf_path(sha)
        if not p.exists():
            _atomic_write(p, data)
        return sha

    def has_doc(self, sha: str) -> bool:
        try:
            return self.pdf_path(sha).exists()
        except ValueError:
            return False

    def verify_integrity(self, sha: str) -> bool:
        """Citations are pinned to the hash; a stored PDF whose bytes changed invalidates them all."""
        return sha256_file(self.pdf_path(sha)) == sha

    # ------------------------------------------------------------------ json blobs

    def _path(self, sha: str, name: str) -> Path:
        return self.doc_dir(sha) / name

    def get_json(self, sha: str, name: str) -> Any | None:
        key = f"{sha}/{name}"
        with self._mem_lock:
            if key in self._mem:
                self._mem.move_to_end(key)
                return self._mem[key]
        p = self._path(sha, name)
        if not p.exists():
            return None
        with open(p, "rb") as f:
            val = json.loads(f.read())
        self._remember(key, val)
        return val

    def put_json(self, sha: str, name: str, value: Any) -> None:
        _atomic_write(self._path(sha, name), json.dumps(value, separators=(",", ":"), default=str).encode())
        self._remember(f"{sha}/{name}", value)

    def _remember(self, key: str, val: Any) -> None:
        with self._mem_lock:
            self._mem[key] = val
            self._mem.move_to_end(key)
            while len(self._mem) > 4000:
                self._mem.popitem(last=False)

    def get_bytes(self, sha: str, name: str) -> bytes | None:
        p = self._path(sha, name)
        return p.read_bytes() if p.exists() else None

    def put_bytes(self, sha: str, name: str, data: bytes) -> Path:
        p = self._path(sha, name)
        _atomic_write(p, data)
        return p

    def append_log(self, sha: str, entry: dict) -> None:
        p = self._path(sha, "extraction_log.jsonl")
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    # ------------------------------------------------------------------ index

    def _exec(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._db_lock:
            cur = self._db.execute(sql, args)
            rows = cur.fetchall()
            self._db.commit()
            return rows

    def map_transaction(self, transaction_id: str, sha: str) -> None:
        self._exec("INSERT OR REPLACE INTO tx_map VALUES (?, ?)", (transaction_id, sha))

    def sha_for_transaction(self, transaction_id: str) -> str | None:
        r = self._exec("SELECT sha FROM tx_map WHERE transaction_id = ?", (transaction_id,))
        return r[0][0] if r else None

    def index_citations(self, sha: str, citation_ids: list[str]) -> None:
        with self._db_lock:
            self._db.executemany("INSERT OR REPLACE INTO citation_index VALUES (?, ?)", [(c, sha) for c in citation_ids])
            self._db.commit()

    def sha_for_citation(self, citation_id: str) -> str | None:
        r = self._exec("SELECT sha FROM citation_index WHERE citation_id = ?", (citation_id,))
        return r[0][0] if r else None

    def add_company_doc(self, company_number: str, made_up_date: str | None, sha: str) -> None:
        self._exec("INSERT OR IGNORE INTO company_docs VALUES (?, ?, ?)", (company_number, made_up_date or "", sha))

    def company_docs(self, company_number: str) -> list[tuple[str, str]]:
        return self._exec("SELECT made_up_date, sha FROM company_docs WHERE company_number = ? ORDER BY made_up_date", (company_number,))

    def take_rate_token(self, name: str, capacity: float, refill_per_s: float, now: float) -> float:
        """Shared token bucket (across processes through SQLite). Returns seconds to wait (0 if a token was taken)."""
        with self._db_lock:
            cur = self._db.cursor()
            cur.execute("BEGIN IMMEDIATE")
            row = cur.execute("SELECT tokens, updated FROM rate_bucket WHERE name = ?", (name,)).fetchone()
            tokens, updated = (row if row else (capacity, now))
            tokens = min(capacity, tokens + (now - updated) * refill_per_s)
            if tokens >= 1:
                tokens -= 1
                wait = 0.0
            else:
                wait = (1 - tokens) / refill_per_s
            cur.execute("INSERT OR REPLACE INTO rate_bucket VALUES (?, ?, ?)", (name, tokens, now))
            self._db.commit()
            return wait


_cache: Cache | None = None
_cache_lock = threading.Lock()


def get_cache() -> Cache:
    global _cache
    with _cache_lock:
        if _cache is None:
            from .config import settings

            _cache = Cache(settings().cache_dir)
        return _cache


def reset_cache(root: Path | None = None) -> Cache:
    global _cache
    with _cache_lock:
        from .config import settings

        _cache = Cache(root or settings().cache_dir)
        return _cache
