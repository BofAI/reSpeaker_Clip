import struct
import unittest
from clock_server import NTP_EPOCH, reply


class ClockServerTest(unittest.TestCase):
    def test_response_preserves_request_timestamp_and_system_time(self):
        request = bytearray(48)
        request[0] = 0x23
        request[40:48] = bytes(range(8))
        response = reply(request, 1800000000.5)
        self.assertEqual(len(response), 48)
        self.assertEqual(response[0] & 7, 4)
        self.assertEqual(response[24:32], request[40:48])
        self.assertEqual(struct.unpack('!II', response[40:48]),
                         (1800000000 + NTP_EPOCH, 1 << 31))

    def test_rejects_server_packets_and_incorrect_lengths(self):
        for request in (b'', bytes(48), b'\x24' + bytes(47), b'\x23' + bytes(48)):
            self.assertIsNone(reply(request, 1800000000))
