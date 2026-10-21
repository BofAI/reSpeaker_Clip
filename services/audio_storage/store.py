"""Durable single-worker upload storage; CLI holds a process lock."""

import hashlib
import os
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException


class Store:
    def __init__(self, root, chunk_size, max_size):
        self.root = Path(root)
        self.parts, self.files = self.root / "parts", self.root / "files"
        self.chunk_size, self.max_size = chunk_size, max_size
        for p in (self.root, self.parts, self.files):
            p.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.locks = [threading.Lock() for _ in range(64)]
        with self.db() as db:
            db.executescript("""PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY,filename TEXT,size INTEGER,sha256 TEXT,mime TEXT,chunk_size INTEGER,state TEXT,created REAL,expires REAL,error TEXT);
            CREATE TABLE IF NOT EXISTS parts(upload_id TEXT,idx INTEGER,sha256 TEXT,size INTEGER,PRIMARY KEY(upload_id,idx));""")
            db.execute(
                "UPDATE uploads SET state='uploading',error='Interrupted; retry completion' WHERE state='finalizing'"
            )
        for p in list(self.parts.glob("*/*.tmp")) + list(self.files.glob("*.tmp")):
            p.unlink(missing_ok=True)
        self.cleanup()

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.root / "metadata.sqlite3", timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def lock(self, uid):
        return self.locks[int(hashlib.sha256(uid.encode()).hexdigest()[:8], 16) % 64]

    def get(self, uid):
        with self.db() as db:
            row = db.execute("SELECT * FROM uploads WHERE id=?", (uid,)).fetchone()
        if row is None:
            raise HTTPException(404, "Upload not found")
        return dict(row)

    def check_live(self, uid):
        row = self.get(uid)
        if (
            row["state"] not in ("complete", "finalizing")
            and row["expires"] < time.time()
        ):
            self.remove(uid)
            raise HTTPException(410, "Upload expired")
        return row

    def remove(self, uid):
        if (self.parts / uid).exists():
            shutil.rmtree(self.parts / uid)
        for p in (self.files / uid, self.files / f"{uid}.tmp"):
            p.unlink(missing_ok=True)
        with self.db() as db:
            db.execute("DELETE FROM parts WHERE upload_id=?", (uid,))
            db.execute("DELETE FROM uploads WHERE id=?", (uid,))

    def cleanup(self):
        with self.db() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM uploads WHERE expires<? AND state NOT IN ('complete','finalizing')",
                    (time.time(),),
                )
            ]
        for uid in ids:
            lock = self.lock(uid)
            if lock.acquire(blocking=False):
                try:
                    if self.get(uid)["state"] not in ("complete", "finalizing"):
                        self.remove(uid)
                except HTTPException:
                    pass
                finally:
                    lock.release()

    def create(self, data):
        uid, now = uuid.uuid4().hex, time.time()
        (self.parts / uid).mkdir(mode=0o700)
        with self.db() as db:
            db.execute(
                "INSERT INTO uploads VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    uid,
                    data["filename"],
                    data["size"],
                    data["sha256"],
                    data["mime"],
                    self.chunk_size,
                    "uploading",
                    now,
                    now + 86400,
                    None,
                ),
            )
        return self.status(uid)

    def status(self, uid):
        row = self.get(uid)
        with self.db() as db:
            row["parts"] = [
                dict(r)
                for r in db.execute(
                    'SELECT idx AS "index",sha256,size FROM parts WHERE upload_id=? ORDER BY idx',
                    (uid,),
                )
            ]
        return row

    def reserve_completion(self, uid):
        row = self.check_live(uid)
        if row["state"] in ("complete", "finalizing"):
            return False
        count = (row["size"] + row["chunk_size"] - 1) // row["chunk_size"]
        with self.db() as db:
            parts = db.execute(
                "SELECT idx,size FROM parts WHERE upload_id=? ORDER BY idx", (uid,)
            ).fetchall()
            if len(parts) != count or any(
                r["idx"] != i
                or r["size"]
                != min(row["chunk_size"], row["size"] - i * row["chunk_size"])
                for i, r in enumerate(parts)
            ):
                raise HTTPException(409, "Missing or invalid parts")
            db.execute(
                "UPDATE uploads SET state='finalizing',error=NULL WHERE id=?", (uid,)
            )
        return True

    def merge(self, uid):
        row = self.get(uid)
        with self.db() as db:
            parts = db.execute(
                "SELECT * FROM parts WHERE upload_id=? ORDER BY idx", (uid,)
            ).fetchall()
        sha, total = hashlib.sha256(), 0
        tmp = self.files / f"{uid}.tmp"
        with tmp.open("wb") as out:
            for part in parts:
                part_sha = hashlib.sha256()
                with (self.parts / uid / str(part["idx"])).open("rb") as src:
                    while block := src.read(256 * 1024):
                        sha.update(block)
                        part_sha.update(block)
                        total += len(block)
                        out.write(block)
                if part_sha.hexdigest() != part["sha256"]:
                    raise ValueError("Stored part checksum mismatch")
            if total != row["size"] or sha.hexdigest() != row["sha256"]:
                raise ValueError("File checksum mismatch")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, self.files / uid)
        fd = os.open(self.files, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        with self.db() as db:
            db.execute(
                "UPDATE uploads SET state='complete',error=NULL WHERE id=?", (uid,)
            )
        shutil.rmtree(self.parts / uid, ignore_errors=True)

    def finalize(self, uid):
        with self.lock(uid):
            try:
                self.merge(uid)
            except Exception as exc:
                (self.files / f"{uid}.tmp").unlink(missing_ok=True)
                with self.db() as db:
                    db.execute(
                        "UPDATE uploads SET state='failed',error=? WHERE id=? AND state!='complete'",
                        (
                            "Insufficient storage"
                            if isinstance(exc, OSError)
                            else "File verification failed",
                            uid,
                        ),
                    )
