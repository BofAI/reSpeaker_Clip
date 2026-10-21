"""Authenticated HTTP audio storage API."""

import asyncio
import errno
import hashlib
import json
import os
import re
import secrets
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from store import Store

MIMES = {
    "audio/wav",
    "audio/x-wav",
    "audio/mpeg",
    "audio/mp4",
    "audio/ogg",
    "audio/flac",
    "audio/aac",
}
HEX = re.compile(r"^[0-9a-f]{64}$")


def parse_range(value, size):
    m = re.fullmatch(r"bytes=(\d{0,20})-(\d{0,20})", value or "")
    if not m or not any(m.groups()):
        raise HTTPException(416, "Unsupported range", headers={"Content-Range": f"bytes */{size}"})
    a, b = m.groups()
    start = int(a) if a else max(0, size - int(b))
    end = min(int(b), size - 1) if a and b else size - 1
    if start > end or start >= size or (not a and int(b) == 0):
        raise HTTPException(
            416, "Unsatisfiable range", headers={"Content-Range": f"bytes */{size}"}
        )
    return start, end


def create_app(root, token, chunk_size=5 * 1024 * 1024, max_size=2 * 1024**3):
    if not token:
        raise ValueError("Token required")
    store = Store(root, chunk_size, max_size)

    @asynccontextmanager
    async def lifespan(app):
        async def sweep():
            while True:
                await asyncio.sleep(60)
                await asyncio.to_thread(store.cleanup)

        task = asyncio.create_task(sweep())
        yield
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    app = FastAPI(
        version="1.0.1-beta.1", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.store = store

    @app.middleware("http")
    async def authorize(request, call_next):
        if not secrets.compare_digest(
            request.headers.get("authorization", "").encode(),
            f"Bearer {token}".encode(),
        ):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        try:
            response = await call_next(request)
        except OSError as exc:
            if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                return JSONResponse({"detail": "Insufficient storage"}, status_code=507)
            raise
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.post("/api/audio/uploads", status_code=201)
    async def init(request: Request):
        body = bytearray()
        async for block in request.stream():
            if len(body) + len(block) > 8192:
                raise HTTPException(413, "Metadata too large")
            body.extend(block)
        try:
            data = json.loads(body)
            name, size, sha, mime = (data[k] for k in ("filename", "size", "sha256", "mime"))
            valid = (
                isinstance(name, str)
                and 0 < len(name.encode()) <= 240
                and "/" not in name
                and "\\" not in name
                and not any(ord(c) < 32 or ord(c) == 127 for c in name)
                and type(size) is int
                and 0 < size <= store.max_size
                and isinstance(sha, str)
                and HEX.fullmatch(sha)
                and mime in MIMES
            )
            if not valid:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise HTTPException(422, "Invalid audio metadata") from None
        return await asyncio.to_thread(store.create, data)

    @app.get("/api/audio/uploads/{uid}")
    def status(uid: str):
        lock = store.lock(uid)
        if not lock.acquire(blocking=False):
            return store.status(uid)
        try:
            store.check_live(uid)
            return store.status(uid)
        finally:
            lock.release()

    @app.put("/api/audio/uploads/{uid}/parts/{index}")
    async def part(uid: str, index: int, request: Request):
        lock = store.lock(uid)
        if not lock.acquire(blocking=False):
            raise HTTPException(409, "Upload busy; retry")
        tmp = None
        try:
            row = store.check_live(uid)
            if row["state"] not in ("uploading", "failed"):
                raise HTTPException(409, "Upload not writable")
            expected = min(row["chunk_size"], row["size"] - index * row["chunk_size"])
            sha = request.headers.get("x-content-sha256", "")
            if index < 0 or expected <= 0 or not HEX.fullmatch(sha):
                raise HTTPException(422, "Invalid part metadata")
            tmp = store.parts / uid / f"{index}.tmp"
            hasher, count = hashlib.sha256(), 0
            with tmp.open("wb") as out:
                async for block in request.stream():
                    count += len(block)
                    if count > expected:
                        raise HTTPException(413, "Part too large")
                    hasher.update(block)
                    out.write(block)
                if count != expected or hasher.hexdigest() != sha:
                    raise HTTPException(422, "Part size or checksum mismatch")
                out.flush()
                os.fsync(out.fileno())
            with store.db() as db:
                existing = db.execute(
                    "SELECT sha256 FROM parts WHERE upload_id=? AND idx=?", (uid, index)
                ).fetchone()
                if existing and existing["sha256"] != sha:
                    raise HTTPException(409, "Part already has different content")
                os.replace(tmp, store.parts / uid / str(index))
                db.execute(
                    "INSERT OR REPLACE INTO parts VALUES (?,?,?,?)",
                    (uid, index, sha, count),
                )
                db.execute("UPDATE uploads SET state='uploading',error=NULL WHERE id=?", (uid,))
            return {"index": index, "sha256": sha, "size": count}
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
            lock.release()

    @app.post("/api/audio/uploads/{uid}/complete")
    def complete(uid: str, tasks: BackgroundTasks):
        lock = store.lock(uid)
        if not lock.acquire(blocking=False):
            raise HTTPException(409, "Upload busy")
        try:
            start = store.reserve_completion(uid)
            result = store.get(uid)
            if start:
                tasks.add_task(store.finalize, uid)
            return JSONResponse(result, status_code=200 if result["state"] == "complete" else 202)
        finally:
            lock.release()

    @app.delete("/api/audio/uploads/{uid}", status_code=204)
    def cancel(uid: str):
        lock = store.lock(uid)
        if not lock.acquire(blocking=False):
            raise HTTPException(409, "Upload busy")
        try:
            if store.get(uid)["state"] in ("complete", "finalizing"):
                raise HTTPException(409, "Cannot cancel committed upload")
            store.remove(uid)
        finally:
            lock.release()

    @app.get("/api/audio/recordings")
    def listing(offset: int = 0, limit: int = 50):
        if offset < 0 or not 1 <= limit <= 100:
            raise HTTPException(422, "Invalid pagination")
        with store.db() as db:
            return {
                "items": [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM uploads WHERE state='complete' ORDER BY created DESC,id DESC LIMIT ? OFFSET ?",
                        (limit, offset),
                    )
                ]
            }

    def recording(uid):
        row = store.get(uid)
        if row["state"] != "complete":
            raise HTTPException(404, "Audio not ready")
        return row

    @app.get("/api/audio/recordings/{uid}")
    def metadata(uid: str):
        return recording(uid)

    @app.api_route("/api/audio/recordings/{uid}/content", methods=["GET", "HEAD"])
    def content(uid: str, request: Request):
        row = recording(uid)
        size, etag = row["size"], f'"{row["sha256"]}"'
        start, end, code = 0, size - 1, 200
        if request.headers.get("range") and request.headers.get("if-range", etag) == etag:
            start, end = parse_range(request.headers["range"], size)
            code = 206
        headers = {
            "Content-Length": str(end - start + 1),
            "Accept-Ranges": "bytes",
            "ETag": etag,
            "Content-Type": row["mime"],
        }
        if code == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        if request.method == "HEAD":
            return Response(status_code=code, headers=headers)

        def chunks():
            with (store.files / uid).open("rb") as src:
                src.seek(start)
                left = end - start + 1
                while left:
                    block = src.read(min(left, 256 * 1024))
                    if not block:
                        raise OSError("Stored audio truncated")
                    left -= len(block)
                    yield block

        return StreamingResponse(chunks(), status_code=code, headers=headers)

    return app
