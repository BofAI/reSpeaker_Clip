import hashlib
import http.client
from email.message import Message
import json
from pathlib import Path
import ssl
import socket
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import unittest

from make_credentials import generate
from server import Handler, Receiver, MAX_CHUNK, MAX_FILE


class ReceiptWriteTest(unittest.TestCase):
    def test_complete_response_is_one_write_with_correct_framing(self):
        class Writer:
            def __init__(self):
                self.writes = []

            def write(self, data):
                self.writes.append(data)
                return len(data)

        for code, batch, status, keep in [(200, False, 'partial', True),
                                          (201, True, 'stored', True),
                                          (201, False, 'stored', False),
                                          (409, True, 'conflict', False)]:
            with self.subTest(code=code, batch=batch):
                handler = object.__new__(Handler)
                handler.request_version = 'HTTP/1.1'
                handler.requestline = 'PUT /v2/test HTTP/1.1'
                handler.command = 'PUT'
                handler.path = '/v2/test'
                handler.resume_connection = True
                handler.headers = Message()
                if batch:
                    handler.headers['X-Clip-Batch'] = '1'
                handler.wfile = Writer()
                payload = {'status': status, 'offset': 16384}
                handler.reply(code, payload)
                self.assertEqual(len(handler.wfile.writes), 1)
                head, body = handler.wfile.writes[0].split(b'\r\n\r\n', 1)
                self.assertEqual(json.loads(body), payload)
                self.assertIn(f'Content-Length: {len(body)}'.encode(), head)
                self.assertIn(b'Connection: ' + (b'keep-alive' if keep else b'close'), head)
                self.assertEqual(handler.close_connection, not keep)
                self.assertEqual(handler._headers_buffer, [])


class ReceiverTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        cert, key = generate(root / "credentials")
        cls.token = (root / "credentials/token").read_text().strip()
        cls.context = ssl.create_default_context(cafile=str(cert))
        # Match the firmware protocol rather than silently testing TLS 1.3.
        cls.context.maximum_version = ssl.TLSVersion.TLSv1_2
        cls.server = Receiver(("127.0.0.1", 0), root / "recordings", "clip-test", cls.token)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(cert, key)
        cls.server_context = tls
        cls.server.socket = tls.wrap_socket(cls.server.socket, server_side=True,
                                            do_handshake_on_connect=False)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def request(self, segment, body=b"test opus bytes", overrides=None, path=None, port=None):
        headers = {"Authorization": "Bearer " + self.token,
                   "X-Content-SHA256": hashlib.sha256(body).hexdigest(),
                   "Content-Length": str(len(body))}
        headers.update(overrides or {})
        conn = http.client.HTTPSConnection("localhost", port or self.server.server_port,
                                           context=self.context, timeout=3)
        try:
            conn.request("PUT", path or f"/v1/devices/clip-test/recordings/session/{segment}", body, headers)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_store_and_retry(self):
        self.assertEqual(self.request(1)[0], 201)
        self.assertEqual(self.request(1)[0], 200)
        self.assertEqual(self.request(1, b"different")[0], 409)

    def test_idle_tcp_client_does_not_block_upload(self):
        # A USB test client or network probe can open TCP without starting TLS.
        # It must not block every other client in the server's accept loop.
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3):
            self.assertEqual(self.request(10)[0], 201)

    def test_saved_bytes_and_receipt(self):
        body = bytes(range(256)) * 31
        status, response = self.request(11, body)
        self.assertEqual(status, 201)
        receipt = json.loads(response)
        self.assertEqual(receipt["status"], "stored")
        self.assertEqual(receipt["size"], len(body))
        self.assertEqual(receipt["sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual((self.server.directory / (receipt["object_id"] + ".bin")).read_bytes(), body)

    def test_bad_hash_leaves_no_object_and_retry_works(self):
        identity = hashlib.sha256(b"clip-test/session/12").hexdigest()
        target = self.server.directory / (identity + ".bin")
        self.assertEqual(self.request(12, overrides={"X-Content-SHA256": "0" * 64})[0], 422)
        self.assertFalse(target.exists())
        self.assertEqual(self.request(12)[0], 201)

    def test_concurrent_conflicting_content(self):
        bodies = [b"first recording", b"different recording"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda body: self.request(13, body)[0], bodies))
        self.assertEqual(sorted(results), [201, 409])
        winner = bodies[results.index(201)]
        self.assertEqual(self.request(13, winner)[0], 200)

    def test_twenty_fresh_tls_uploads(self):
        for segment in range(100, 120):
            body = f"recording-{segment}".encode()
            status, response = self.request(segment, body)
            self.assertEqual(status, 201)
            receipt = json.loads(response)
            self.assertEqual(receipt["sha256"], hashlib.sha256(body).hexdigest())
            self.assertEqual((self.server.directory / (receipt["object_id"] + ".bin")).read_bytes(), body)

    def test_auth_and_identity(self):
        self.assertEqual(self.request(2, overrides={"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(self.request(2, path="/v1/devices/other/recordings/session/2")[0], 403)

    def test_integrity_and_limits(self):
        self.assertEqual(self.request(3, overrides={"X-Content-SHA256": "0" * 64})[0], 422)
        self.assertEqual(self.request(3, overrides={"Content-Length": str(MAX_FILE + 1)})[0], 413)
        self.assertEqual(self.request(3, overrides={"Transfer-Encoding": "chunked"})[0], 400)

    def test_traversal_and_empty(self):
        self.assertEqual(self.request(4, path="/v1/devices/clip-test/recordings/../4")[0], 403)
        self.assertEqual(self.request(4, b"")[0], 413)

    def test_large_stream(self):
        body = bytes(range(256)) * (MAX_FILE // 256)
        self.assertEqual(self.request(5, body)[0], 201)
        self.assertEqual(self.request(5, body)[0], 200)
        self.assertEqual(list(self.server.directory.glob(".incoming-*")), [])

    def test_untrusted_ca(self):
        conn = http.client.HTTPSConnection("localhost", self.server.server_port)
        try:
            with self.assertRaises(ssl.SSLCertVerificationError):
                conn.request("GET", "/healthz")
        finally:
            conn.close()

    def test_wrong_hostname(self):
        conn = http.client.HTTPSConnection("127.0.0.1", self.server.server_port, context=self.context)
        try:
            with self.assertRaises(ssl.SSLCertVerificationError):
                conn.request("GET", "/healthz")
        finally:
            conn.close()

    def test_concurrent_retry(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.request(6)[0], range(2)))
        self.assertEqual(sorted(results), [200, 201])

    def test_reopened_receiver(self):
        self.assertEqual(self.request(7)[0], 201)
        # A fresh receiver has no in-memory knowledge of the previous upload.
        other = Receiver(("127.0.0.1", 0), self.server.directory, "clip-test", self.token)
        other.socket = self.server_context.wrap_socket(other.socket, server_side=True,
                                                       do_handshake_on_connect=False)
        thread = threading.Thread(target=other.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertEqual(self.request(7, port=other.server_port)[0], 200)
        finally:
            other.shutdown()
            other.server_close()
            thread.join()

    def test_duplicate_content_length(self):
        conn = http.client.HTTPSConnection("localhost", self.server.server_port, context=self.context)
        try:
            conn.putrequest("PUT", "/v1/devices/clip-test/recordings/session/8")
            conn.putheader("Authorization", "Bearer " + self.token)
            conn.putheader("Content-Length", "1")
            conn.putheader("Content-Length", "2")
            conn.putheader("X-Content-SHA256", "0" * 64)
            conn.endheaders()
            response = conn.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
        finally:
            conn.close()

    def test_interrupted_upload(self):
        conn = http.client.HTTPSConnection("localhost", self.server.server_port, context=self.context)
        conn.connect()
        headers = ("PUT /v1/devices/clip-test/recordings/session/9 HTTP/1.1\r\n"
                   "Host: localhost\r\nAuthorization: Bearer " + self.token +
                   "\r\nContent-Length: 10000\r\nX-Content-SHA256: " + "0" * 64 + "\r\n\r\n")
        conn.sock.sendall(headers.encode() + b"partial")
        conn.close()
        # A successful new request proves the receiver can continue serving.
        self.assertEqual(self.request(9)[0], 201)
        end = time.monotonic() + 2
        while list(self.server.directory.glob(".incoming-*")) and time.monotonic() < end:
            time.sleep(0.01)
        self.assertEqual(list(self.server.directory.glob(".incoming-*")), [])

    def resume_request(self, method, segment, recording, offset=0, body=b"", overrides=None, conn=None):
        headers = {"Authorization": "Bearer " + self.token,
                   "X-Recording-Size": str(len(recording)),
                   "X-Content-SHA256": hashlib.sha256(recording).hexdigest(),
                   "Upload-Offset": str(offset), "Content-Length": str(len(body)),
                   "X-Chunk-SHA256": hashlib.sha256(body).hexdigest()}
        headers.update(overrides or {})
        own_connection = conn is None
        conn = conn or http.client.HTTPSConnection("localhost", self.server.server_port,
                                                   context=self.context, timeout=3)
        try:
            conn.request(method, f"/v2/devices/clip-test/recordings/20261020000000/{segment}",
                         body, headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            if own_connection:
                conn.close()

    def test_receiver_opt_in_enforces_limit_and_resumes(self):
        for limit in (32768, 65536, 131072):
            with self.subTest(limit=limit):
                recording = b'x' * (limit + 123)
                self.assertEqual(self.resume_request('PUT', 550, recording, 0, recording[:limit])[0], 400)
                with tempfile.TemporaryDirectory() as directory:
                    server = Receiver(('127.0.0.1', 0), directory, 'clip-test', self.token,
                                      max_chunk=limit)
                    server.socket = self.server_context.wrap_socket(server.socket, server_side=True,
                                                                    do_handshake_on_connect=False)
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    conn = http.client.HTTPSConnection('localhost', server.server_port,
                                                        context=self.context, timeout=3)
                    try:
                        self.assertEqual(self.resume_request('PUT', 550, recording, 0,
                                                             recording[:(limit + 1)], conn=conn)[0], 400)
                        self.assertEqual(self.resume_request('GET', 550, recording, conn=conn)[1]['offset'], 0)
                        self.assertEqual(self.resume_request('PUT', 550, recording, 0,
                                                             recording[:limit], conn=conn)[1]['offset'], limit)
                        self.assertEqual(self.resume_request('PUT', 550, recording, 0,
                                                             recording[:limit], conn=conn)[0], 200)
                        self.assertEqual(self.resume_request('GET', 550, recording, conn=conn)[1]['offset'], limit)
                        result = self.resume_request('PUT', 550, recording, limit,
                                                     recording[limit:], conn=conn)
                        self.assertEqual((result[0], result[1]['status']), (201, 'stored'))
                        identity = hashlib.sha256(b'clip-test/20261020000000/550').hexdigest()
                        self.assertEqual((Path(directory) / (identity + '.bin')).read_bytes(), recording)
                    finally:
                        conn.close()
                        server.shutdown()
                        server.server_close()
                        thread.join()

    def test_resume_reuses_tls_and_checks_every_durable_offset(self):
        recording = bytes(range(256)) * 180
        conn = http.client.HTTPSConnection("localhost", self.server.server_port,
                                           context=self.context, timeout=3)
        try:
            status, reply = self.resume_request("GET", 501, recording, conn=conn)
            self.assertEqual((status, reply["offset"], reply["status"]), (200, 0, "partial"))
            transport = conn.sock
            for offset in range(0, len(recording), MAX_CHUNK):
                chunk = recording[offset:offset + MAX_CHUNK]
                status, reply = self.resume_request("PUT", 501, recording, offset, chunk, conn=conn)
                self.assertIn(status, (200, 201))
                self.assertEqual(reply["offset"], offset + len(chunk))
                if reply["status"] == "partial":
                    self.assertIs(conn.sock, transport)
                else:
                    self.assertIsNone(conn.sock)
            self.assertEqual(reply["status"], "stored")
            identity = hashlib.sha256(b"clip-test/20261020000000/501").hexdigest()
            self.assertEqual((self.server.directory / (identity + ".bin")).read_bytes(), recording)
            status, reply = self.resume_request("GET", 501, recording, conn=conn)
            self.assertEqual((status, reply["offset"], reply["status"]), (200, len(recording), "stored"))
            self.assertEqual(self.resume_request("GET", 501, b"conflicting identity")[0], 409)
        finally:
            conn.close()

    def test_profile_receipts_match_wire_and_exclude_credentials(self):
        records = []
        self.server.profile_sink = records.append
        try:
            recording = b'profile-payload' * 1200
            self.assertEqual(self.resume_request('GET', 599, recording)[0], 200)
            for offset in range(0, len(recording), MAX_CHUNK):
                block = recording[offset:offset + MAX_CHUNK]
                code, receipt = self.resume_request('PUT', 599, recording, offset, block)
                self.assertIn(code, (200, 201))
            end = time.monotonic() + 2
            while len(records) < 3 and time.monotonic() < end:
                time.sleep(.01)
            self.assertEqual(len(records), 3)
            self.assertEqual([r['status'] for r in records], [200, 200, 201])
            self.assertEqual(records[1]['offset'], 0)
            self.assertEqual(records[2]['offset'], MAX_CHUNK)
            self.assertIn('data_fsync_us', records[1])
            self.assertIn('body_receive_us', records[1])
            self.assertIn('response_write_us', records[1])
            self.assertNotIn(self.token, json.dumps(records))
            self.assertNotIn('profile-payload', json.dumps(records))
            self.assertEqual(receipt['sha256'], hashlib.sha256(recording).hexdigest())
        finally:
            self.server.profile_sink = None

    def test_partial_resume_survives_new_receiver(self):
        recording = b"payload" * 5000
        self.assertEqual(self.resume_request("PUT", 502, recording, 0, recording[:MAX_CHUNK])[0], 200)
        other = Receiver(("127.0.0.1", 0), self.server.directory, "clip-test", self.token)
        other.socket = self.server_context.wrap_socket(other.socket, server_side=True,
                                                       do_handshake_on_connect=False)
        worker = threading.Thread(target=other.serve_forever, daemon=True)
        worker.start()
        conn = http.client.HTTPSConnection("localhost", other.server_port, context=self.context, timeout=3)
        try:
            self.assertEqual(self.resume_request("GET", 502, recording, conn=conn)[1]["offset"], MAX_CHUNK)
            for offset in range(MAX_CHUNK, len(recording), MAX_CHUNK):
                status, reply = self.resume_request("PUT", 502, recording, offset,
                                                    recording[offset:offset + MAX_CHUNK], conn=conn)
                self.assertIn(status, (200, 201))
            self.assertEqual(reply["status"], "stored")
        finally:
            conn.close()
            other.shutdown()
            other.server_close()
            worker.join()

    def test_batch_keeps_same_tls_across_complete_files_and_stored_queries(self):
        conn = http.client.HTTPSConnection("localhost", self.server.server_port,
                                           context=self.context, timeout=3)
        try:
            conn.connect()
            transport = conn.sock
            for segment in (601, 602, 603):
                recording = bytes([segment % 256]) * (MAX_CHUNK + 19)
                headers = {"X-Clip-Batch": "1"}
                self.assertEqual(self.resume_request("GET", segment, recording,
                                                     overrides=headers, conn=conn)[1]["offset"], 0)
                for offset in range(0, len(recording), MAX_CHUNK):
                    code, receipt = self.resume_request("PUT", segment, recording, offset,
                                                       recording[offset:offset+MAX_CHUNK],
                                                       overrides=headers, conn=conn)
                    self.assertIn(code, (200, 201))
                    self.assertIs(conn.sock, transport)
                self.assertEqual(receipt["status"], "stored")
                code, receipt = self.resume_request("GET", segment, recording,
                                                   overrides=headers, conn=conn)
                self.assertEqual((code, receipt["offset"]), (200, len(recording)))
                self.assertIs(conn.sock, transport)
            # Errors retain the old close behavior, even for batch clients.
            code, _ = self.resume_request("GET", 603, b"conflicting",
                                          overrides={"X-Clip-Batch": "1"}, conn=conn)
            self.assertEqual(code, 409)
            self.assertIsNone(conn.sock)
        finally:
            conn.close()

    def test_resume_rejects_bad_chunks_without_advancing_checkpoint(self):
        recording = b"a" * (MAX_CHUNK + 100)
        self.assertEqual(self.resume_request("GET", 503, recording,
                                          overrides={"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(self.resume_request("PUT", 503, recording, 1, b"a")[0], 409)
        self.assertEqual(self.resume_request("PUT", 503, recording, 0, b"a",
                                          overrides={"X-Chunk-SHA256": "0" * 64})[0], 422)
        self.assertEqual(self.resume_request("PUT", 503, recording, 0, b"a",
                                          overrides={"Transfer-Encoding": "chunked"})[0], 400)
        self.assertEqual(self.resume_request("GET", 503, recording)[1]["offset"], 0)
        self.assertEqual(self.resume_request("PUT", 503, recording, 0, recording[:MAX_CHUNK])[0], 200)
        self.assertEqual(self.resume_request("GET", 503, recording,
                                          overrides={"X-Content-SHA256": "0" * 64})[0], 409)
        conn = http.client.HTTPSConnection("localhost", self.server.server_port, context=self.context)
        try:
            conn.putrequest("GET", "/v2/devices/clip-test/recordings/20261020000000/503")
            conn.putheader("Authorization", "Bearer " + self.token)
            conn.putheader("X-Recording-Size", str(len(recording)))
            conn.putheader("X-Recording-Size", str(len(recording)))
            conn.putheader("X-Content-SHA256", hashlib.sha256(recording).hexdigest())
            conn.endheaders()
            response = conn.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
        finally:
            conn.close()
        self.assertEqual(self.resume_request("GET", 503, recording)[1]["offset"], MAX_CHUNK)

    def test_interrupted_chunk_preserves_previous_checkpoint(self):
        recording = b"x" * (MAX_CHUNK * 2)
        self.resume_request("PUT", 504, recording, 0, recording[:MAX_CHUNK])
        conn = http.client.HTTPSConnection("localhost", self.server.server_port, context=self.context)
        conn.connect()
        headers = ("PUT /v2/devices/clip-test/recordings/20261020000000/504 HTTP/1.1\r\n"
                   "Host: localhost\r\nAuthorization: Bearer " + self.token +
                   "\r\nContent-Length: " + str(MAX_CHUNK) +
                   "\r\nUpload-Offset: " + str(MAX_CHUNK) +
                   "\r\nX-Recording-Size: " + str(len(recording)) +
                   "\r\nX-Content-SHA256: " + hashlib.sha256(recording).hexdigest() +
                   "\r\nX-Chunk-SHA256: " + hashlib.sha256(recording[MAX_CHUNK:]).hexdigest() + "\r\n\r\n")
        conn.sock.sendall(headers.encode() + b"unfinished")
        conn.close()
        self.assertEqual(self.resume_request("GET", 504, recording)[1]["offset"], MAX_CHUNK)
        self.assertEqual(self.resume_request("PUT", 504, recording, MAX_CHUNK,
                                          recording[MAX_CHUNK:])[1]["status"], "stored")


if __name__ == "__main__":
    unittest.main()
