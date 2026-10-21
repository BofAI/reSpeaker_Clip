import hashlib
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient


def digest(b):
    return hashlib.sha256(b).hexdigest()


@pytest.fixture
def env(tmp_path):
    from app import create_app

    app = create_app(tmp_path, "test-token", chunk_size=8, max_size=128)
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        yield client, app, tmp_path


def init(c, data=b"0123456789abcdef", **kw):
    args = dict(
        filename="test.wav", size=len(data), sha256=digest(data), mime="audio/wav"
    )
    args.update(kw)
    return c.post("/api/audio/uploads", json=args)


def put(c, uid, i, b, sha=None):
    return c.put(
        f"/api/audio/uploads/{uid}/parts/{i}",
        content=b,
        headers={"X-Content-SHA256": sha or digest(b)},
    )


def uploaded(c, data=b"0123456789abcdef"):
    uid = init(c, data).json()["id"]
    for i, off in enumerate(range(0, len(data), 8)):
        assert put(c, uid, i, data[off : off + 8]).status_code == 200
    return uid


def complete(c, uid):
    r = c.post(f"/api/audio/uploads/{uid}/complete")
    assert r.status_code in (200, 202), r.text
    for _ in range(100):
        info = c.get(f"/api/audio/uploads/{uid}").json()
        if info["state"] != "finalizing":
            return info
        time.sleep(0.01)
    pytest.fail("completion timed out")


def test_auth_and_input_limits(env):
    c, app, _ = env
    assert (
        c.get("/api/audio/recordings", headers={"Authorization": ""}).status_code == 401
    )
    assert (
        c.get(
            "/api/audio/recordings", headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )
    for kw in [
        dict(size=129),
        dict(size=0),
        dict(size=True),
        dict(sha256="bad"),
        dict(filename="../x.wav"),
        dict(filename="a\r\nx.wav"),
        dict(mime="text/html"),
    ]:
        assert init(c, **kw).status_code == 422
    assert c.post("/api/audio/uploads", content=b"x" * 9000).status_code == 413


def test_upload_roundtrip_ranges_and_idempotence(env):
    c, _, _ = env
    data = b"0123456789abcdef"
    uid = init(c).json()["id"]
    assert put(c, uid, 1, data[8:]).status_code == 200
    assert c.post(f"/api/audio/uploads/{uid}/complete").status_code == 409
    assert put(c, uid, 0, data[:8]).status_code == 200
    assert put(c, uid, 0, data[:8]).status_code == 200
    assert put(c, uid, 0, b"xxxxxxxx").status_code == 409
    assert complete(c, uid)["state"] == "complete"
    assert complete(c, uid)["state"] == "complete"
    assert len(c.get("/api/audio/recordings").json()["items"]) == 1
    url = f"/api/audio/recordings/{uid}/content"
    assert c.get(url).content == data
    h = c.head(url)
    assert h.content == b"" and h.headers["content-length"] == "16"
    for value, expected in [
        ("bytes=2-5", b"2345"),
        ("bytes=12-", b"cdef"),
        ("bytes=-4", b"cdef"),
    ]:
        r = c.get(url, headers={"Range": value})
        assert r.status_code == 206 and r.content == expected
    for value in ["bytes=16-", "bytes=3-1", "bytes=0-1,3-4", "bad"]:
        assert c.get(url, headers={"Range": value}).status_code == 416
    assert (
        c.get(url, headers={"Range": "bytes=2-", "If-Range": "old"}).status_code == 200
    )
    assert (
        c.get(url, headers={"Range": "bytes=2-", "If-Range": h.headers["etag"]}).content
        == data[2:]
    )
    assert c.delete(f"/api/audio/uploads/{uid}").status_code == 409


def test_corrupt_parts_and_file_never_publish(env):
    c, _, _ = env
    uid = init(c, sha256="0" * 64).json()["id"]
    assert put(c, uid, 0, b"01234567", sha="0" * 64).status_code == 422
    assert put(c, uid, 0, b"012345678").status_code == 413
    assert put(c, uid, 9, b"01234567").status_code == 422
    assert put(c, uid, 0, b"01234567").status_code == 200
    assert put(c, uid, 1, b"89abcdef").status_code == 200
    assert complete(c, uid)["state"] == "failed"
    assert c.get("/api/audio/recordings").json()["items"] == []
    assert c.get(f"/api/audio/recordings/{uid}/content").status_code == 404


def test_restart_resume_and_persistence(env):
    c, _, root = env
    uid = init(c).json()["id"]
    put(c, uid, 0, b"01234567")
    from app import create_app

    with TestClient(
        create_app(root, "test-token", chunk_size=8, max_size=128),
        headers={"Authorization": "Bearer test-token"},
    ) as other:
        assert other.get(f"/api/audio/uploads/{uid}").json()["parts"][0]["index"] == 0
        assert put(other, uid, 1, b"89abcdef").status_code == 200
        assert complete(other, uid)["state"] == "complete"
    with TestClient(
        create_app(root, "test-token", chunk_size=8, max_size=128),
        headers={"Authorization": "Bearer test-token"},
    ) as other:
        assert (
            other.get(f"/api/audio/recordings/{uid}/content").content
            == b"0123456789abcdef"
        )


def test_cancel_and_expire(env):
    c, app, _ = env
    uid = uploaded(c)
    assert c.delete(f"/api/audio/uploads/{uid}").status_code == 204
    assert c.get(f"/api/audio/uploads/{uid}").status_code == 404
    uid = uploaded(c)
    with app.state.store.db() as db:
        db.execute("UPDATE uploads SET expires=0 WHERE id=?", (uid,))
    assert c.get(f"/api/audio/uploads/{uid}").status_code == 410
    assert not (app.state.store.parts / uid).exists()


def test_disk_failure_does_not_publish(env, monkeypatch):
    import errno

    c, app, _ = env
    uid = uploaded(c)

    def no_space(*args):
        raise OSError(errno.ENOSPC, "test disk full")

    monkeypatch.setattr(app.state.store, "merge", no_space)
    assert complete(c, uid)["state"] == "failed"
    assert c.get("/api/audio/recordings").json()["items"] == []


def test_short_body_and_bad_pagination(env):
    c, _, _ = env
    uid = init(c).json()["id"]
    assert put(c, uid, 0, b"123").status_code == 422
    assert c.get("/api/audio/recordings?limit=0").status_code == 422
    assert c.get("/api/audio/recordings?offset=-1").status_code == 422
    assert c.get("/api/audio/uploads/unknown").status_code == 404


def test_cancel_cannot_race_finalization(env):
    import threading

    c, app, _ = env
    uid = uploaded(c)
    store = app.state.store
    original = store.merge
    started, proceed = threading.Event(), threading.Event()

    def slow_merge(value):
        started.set()
        assert proceed.wait(3)
        original(value)

    store.merge = slow_merge
    with ThreadPoolExecutor() as pool:
        future = pool.submit(lambda: c.post(f"/api/audio/uploads/{uid}/complete"))
        assert started.wait(2)
        assert c.delete(f"/api/audio/uploads/{uid}").status_code == 409
        assert put(c, uid, 0, b"01234567").status_code == 409
        proceed.set()
        assert future.result().status_code == 202
    assert c.get(f"/api/audio/recordings/{uid}/content").status_code == 200


def test_restart_recovers_interrupted_finalization(env):
    c, app, root = env
    uid = uploaded(c)
    app.state.store.reserve_completion(uid)
    (app.state.store.files / f"{uid}.tmp").write_bytes(b"incomplete")
    from app import create_app

    with TestClient(
        create_app(root, "test-token", chunk_size=8, max_size=128),
        headers={"Authorization": "Bearer test-token"},
    ) as after:
        assert after.get(f"/api/audio/uploads/{uid}").json()["state"] == "uploading"
        assert not (app.state.store.files / f"{uid}.tmp").exists()
        assert complete(after, uid)["state"] == "complete"


def test_stored_part_corruption_prevents_publication(env):
    c, app, _ = env
    uid = uploaded(c)
    (app.state.store.parts / uid / "0").write_bytes(b"xxxxxxxx")
    assert complete(c, uid)["state"] == "failed"
    assert c.get(f"/api/audio/recordings/{uid}/content").status_code == 404


def test_upload_disk_full_returns_507(env, monkeypatch):
    import errno
    from pathlib import Path

    c, _, _ = env
    uid = init(c).json()["id"]
    original = Path.open

    def disk_full(path, *args, **kwargs):
        if str(path).endswith(".tmp"):
            raise OSError(errno.ENOSPC, "test full")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", disk_full)
    assert put(c, uid, 0, b"01234567").status_code == 507
    assert c.get(f"/api/audio/uploads/{uid}").json()["parts"] == []


def test_status_remains_responsive_during_merge(env):
    import threading

    c, app, _ = env
    uid = uploaded(c)
    store = app.state.store
    original = store.merge
    entered, release = threading.Event(), threading.Event()

    def slow(value):
        entered.set()
        assert release.wait(3)
        original(value)

    store.merge = slow
    with ThreadPoolExecutor() as pool:
        merge = pool.submit(lambda: c.post(f"/api/audio/uploads/{uid}/complete"))
        assert entered.wait(1)
        status = pool.submit(lambda: c.get(f"/api/audio/uploads/{uid}"))
        try:
            result = status.result(timeout=0.5)
            assert result.json()["state"] == "finalizing"
        finally:
            release.set()
            merge.result()
