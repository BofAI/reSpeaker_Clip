"""Small opt-in LAN SNTP endpoint using the Mac/Linux system clock.

This is unauthenticated time, intended for the same controlled test network as
our HTTPS receiver. It never disables the device's TLS certificate checks.
"""
import socketserver
import struct
import time

NTP_EPOCH = 2_208_988_800


def reply(request, unix_time):
    if len(request) != 48 or request[0] & 7 != 3 or (request[0] >> 3) & 7 not in (3, 4):
        return None
    seconds = int(unix_time)
    stamp = struct.pack('!II', (seconds + NTP_EPOCH) & 0xffffffff,
                        int((unix_time - seconds) * (1 << 32)))
    response = bytearray(48)
    response[0] = (request[0] & 0x38) | 4
    response[1:4] = bytes((2, 4, 236))
    response[12:16] = b'LOCL'
    response[16:24] = stamp
    response[24:32] = request[40:48]
    response[32:40] = stamp
    response[40:48] = stamp
    return bytes(response)


class ClockHandler(socketserver.BaseRequestHandler):
    def handle(self):
        request, transport = self.request
        response = reply(request, time.time())
        if response is not None:
            transport.sendto(response, self.client_address)


class ClockServer(socketserver.UDPServer):
    allow_reuse_address = True
