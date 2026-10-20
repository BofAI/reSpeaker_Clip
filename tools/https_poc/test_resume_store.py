import hashlib
import json
from pathlib import Path
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from resume_store import MAX_CHUNK, ResumeError, ResumeStore


class ResumeStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.store = ResumeStore(self.directory, threading.Lock())
        self.data = bytes(range(256)) * 150
        self.digest = hashlib.sha256(self.data).hexdigest()
        self.identity = hashlib.sha256(b"fixture/20261019070446/1").hexdigest()

    def status(self):
        return self.store.status(self.identity, len(self.data), self.digest)

    def commit(self, offset, data=None, digest=None):
        data = self.data[offset:offset + MAX_CHUNK] if data is None else data
        return self.store.commit(self.identity, len(self.data), digest or self.digest,
                                 offset, data, hashlib.sha256(data).hexdigest())

    def test_complete_idempotent_and_lost_reply(self):
        self.assertEqual(self.status()["offset"], 0)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.commit(0)  # Pretend its HTTP response was lost.
        self.assertEqual((self.directory / (self.identity + ".partial")).stat().st_mode & 0o077, 0)
        self.assertEqual(self.status()["offset"], MAX_CHUNK)
        self.assertEqual(self.commit(0)[0], 200)
        self.commit(MAX_CHUNK)
        self.assertEqual(self.commit(2 * MAX_CHUNK)[0], 201)
        self.assertEqual(self.status()["status"], "stored")
        self.assertEqual(self.commit(2 * MAX_CHUNK)[0], 200)
        self.assertEqual((self.directory / (self.identity + ".bin")).read_bytes(), self.data)
        self.assertEqual((self.directory / (self.identity + ".bin")).stat().st_mode & 0o077, 0)
        self.assertEqual(len(list(self.directory.iterdir())), 1)

    def test_reopen_partial_and_discard_unacknowledged_tail(self):
        self.commit(0)
        partial = self.directory / (self.identity + ".partial")
        with partial.open("ab") as output:
            output.write(b"uncommitted tail")
        self.store = ResumeStore(self.directory, threading.Lock())
        self.assertEqual(self.status()["offset"], MAX_CHUNK)
        self.assertEqual(partial.read_bytes(), self.data[:MAX_CHUNK])
        self.commit(MAX_CHUNK)
        self.commit(2 * MAX_CHUNK)
        self.assertEqual(self.status()["status"], "stored")

    def test_failure_between_data_and_checkpoint_does_not_ack_tail(self):
        self.commit(0)
        original = self.store._save

        def fail_checkpoint(path, state):
            if state["offset"] > MAX_CHUNK:
                raise OSError("simulated checkpoint publication failure")
            original(path, state)

        with patch.object(self.store, "_save", side_effect=fail_checkpoint):
            with self.assertRaises(OSError):
                self.commit(MAX_CHUNK)
        self.assertEqual(self.status()["offset"], MAX_CHUNK)
        self.assertEqual((self.directory / (self.identity + ".partial")).stat().st_size, MAX_CHUNK)
        self.commit(MAX_CHUNK)
        self.commit(2 * MAX_CHUNK)
        self.assertEqual(self.status()["status"], "stored")

    def test_restart_finalizes_checkpointed_full_file(self):
        self.commit(0)
        self.commit(MAX_CHUNK)
        with patch.object(self.store, "_finish", side_effect=OSError("before rename")):
            with self.assertRaises(OSError):
                self.commit(2 * MAX_CHUNK)
        self.store = ResumeStore(self.directory, threading.Lock())
        self.assertEqual(self.status()["status"], "stored")
        self.assertEqual((self.directory / (self.identity + ".bin")).read_bytes(), self.data)

    def test_status_syncs_checkpoint_rename_after_failed_directory_sync(self):
        self.commit(0)
        original = self.store._save

        def fail_after_checkpoint_rename(path, state):
            with patch.object(self.store, "_sync_directory", side_effect=OSError("directory fsync")):
                original(path, state)

        with patch.object(self.store, "_save", side_effect=fail_after_checkpoint_rename):
            with self.assertRaises(OSError):
                self.commit(MAX_CHUNK)
        with patch.object(self.store, "_sync_directory", wraps=self.store._sync_directory) as sync:
            self.assertEqual(self.status()["offset"], 2 * MAX_CHUNK)
            sync.assert_called_once()

    def test_restart_after_final_rename_cleans_stale_checkpoint(self):
        self.commit(0)
        self.commit(MAX_CHUNK)
        original = self.store._sync_directory

        def fail_after_rename():
            if (self.directory / (self.identity + ".bin")).exists():
                raise OSError("after rename")
            original()

        with patch.object(self.store, "_sync_directory", side_effect=fail_after_rename):
            with self.assertRaises(OSError):
                self.commit(2 * MAX_CHUNK)
        self.assertEqual(self.status()["status"], "stored")
        self.assertEqual(len(list(self.directory.iterdir())), 1)

    def test_wrong_offset_hash_or_identity_never_advances_checkpoint(self):
        self.commit(0)
        for offset, data, digest, expected in (
                (MAX_CHUNK + 1, b"gap", self.digest, 409),
                (0, b"overwrite", self.digest, 409),
                (MAX_CHUNK, b"anything", "0" * 64, 409)):
            with self.subTest(offset=offset, expected=expected):
                with self.assertRaises(ResumeError) as result:
                    self.commit(offset, data, digest)
                self.assertEqual(result.exception.code, expected)
                self.assertEqual(self.status()["offset"], MAX_CHUNK)
        with self.assertRaises(ResumeError) as result:
            self.store.commit(self.identity, len(self.data), self.digest, MAX_CHUNK, b"bad", "0" * 64)
        self.assertEqual(result.exception.code, 422)
        self.assertEqual(self.status()["offset"], MAX_CHUNK)

    def test_corrupted_prefix_is_never_published_and_can_restart(self):
        self.commit(0)
        path = self.directory / (self.identity + ".partial")
        with path.open("r+b") as output:
            output.write(b"x")
        self.commit(MAX_CHUNK)
        with self.assertRaises(ResumeError) as result:
            self.commit(2 * MAX_CHUNK)
        self.assertEqual(result.exception.code, 422)
        self.assertFalse((self.directory / (self.identity + ".bin")).exists())
        self.assertEqual(self.status()["offset"], 0)
        self.commit(0)
        self.commit(MAX_CHUNK)
        self.commit(2 * MAX_CHUNK)
        self.assertEqual(self.status()["status"], "stored")

    def test_missing_data_or_bad_checkpoint_fails_without_guessing_offset(self):
        self.commit(0)
        partial = self.directory / (self.identity + ".partial")
        partial.write_bytes(b"short")
        with self.assertRaises(ResumeError) as result:
            self.status()
        self.assertEqual(result.exception.code, 500)
        checkpoint = self.directory / (self.identity + ".checkpoint")
        checkpoint.write_text(json.dumps({"version": 1, "size": len(self.data), "offset": True}))
        with self.assertRaises(ResumeError) as result:
            self.status()
        self.assertEqual(result.exception.code, 500)

    def test_concurrent_duplicate_chunk_is_committed_once(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: self.commit(0), range(2)))
        self.assertTrue(all(reply[1]["offset"] == MAX_CHUNK for reply in replies))
        self.assertEqual((self.directory / (self.identity + ".partial")).read_bytes(), self.data[:MAX_CHUNK])


if __name__ == "__main__":
    unittest.main()
