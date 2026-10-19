import asyncio
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from collect import Connection


class FakeClient:
    def __init__(self):
        self.connected = True
        self.commands = []
        self.pending = 0
        self.respond = True

    async def write_gatt_char(self, _uuid, payload, *, response):
        assert self.connected and response is False
        assert self.pending == 0, "commands overlapped"
        self.pending += 1
        self.commands.append(payload.decode())
        if self.respond:
            def deliver():
                self.pending -= 1
                self.connection.receive(None, b'{"event":"test"}{"ok":tr')
                self.connection.receive(None, b'ue,"data":{"cpu_bp":42}}\n')
            asyncio.get_running_loop().call_later(0.001, deliver)

    async def disconnect(self):
        self.connected = False


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_fragmented_responses_events_and_serialization(self):
        client = FakeClient()
        connection = Connection(client)
        client.connection = connection
        results = await asyncio.gather(
            connection.request("AT+RES"), connection.request("AT+RES?"))
        self.assertEqual(client.commands, ["AT+RES", "AT+RES?"])
        self.assertEqual(results, [{"ok": True, "data": {"cpu_bp": 42}}] * 2)
        self.assertTrue(connection.queue.empty())

    async def test_timeout_disconnects_to_discard_late_responses(self):
        client = FakeClient()
        client.respond = False
        connection = Connection(client)
        with self.assertRaises(asyncio.TimeoutError):
            await connection.request("AT+RES", timeout=0.001)
        self.assertFalse(client.connected)


if __name__ == "__main__":
    unittest.main()
