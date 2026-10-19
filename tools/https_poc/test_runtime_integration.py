"""Exercise the firmware command policy and LAN certificate identity."""
from pathlib import Path
import subprocess
import socket
import ssl
import tempfile
import threading
import unittest

from make_credentials import generate
from server import Receiver

ROOT = Path(__file__).resolve().parents[2]


class RuntimeIntegrationTest(unittest.TestCase):
    def test_resources_remain_readable_during_upload(self):
        source = (ROOT / "applications/clip/src/https_poc.c").read_text()
        policy = source.split("bool https_poc_command_allowed", 1)[1].split(
            "bool https_poc_event", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code = '#include <stdbool.h>\n#include <string.h>\n#include <assert.h>\n'
            code += 'static bool busy;\nstatic bool https_poc_active(void) {return busy;}\n'
            code += 'bool https_poc_command_allowed' + policy
            code += '''
int main(void) {
    for (int state=0; state<2; state++) {
        busy=state;
        assert(https_poc_command_allowed("RES"));
        assert(https_poc_command_allowed("GSTAT"));
        assert(!https_poc_command_allowed("PAIR"));
        assert(!https_poc_command_allowed("DELETE"));
        assert(!https_poc_command_allowed("WIFI"));
    }
    assert(!https_poc_command_allowed("NETCFG"));
    assert(!https_poc_command_allowed("START"));
    busy=false;
    assert(https_poc_command_allowed("START"));
    return 0;
}
'''
            (root / "test.c").write_text(code)
            subprocess.run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror",
                            "-fsanitize=address,undefined", str(root / "test.c"),
                            "-o", str(root / "test")], check=True)
            subprocess.run([str(root / "test")], check=True)

    def test_ip_certificate_matches_only_requested_address(self):
        with tempfile.TemporaryDirectory() as directory:
            cert, key = generate(Path(directory) / "credentials", "127.0.0.1")
            server = Receiver(("127.0.0.1", 0), Path(directory) / "recordings", "fixture", "fixture")
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(cert, key)
            server.socket = tls.wrap_socket(server.socket, server_side=True,
                                            do_handshake_on_connect=False)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            trusted = ssl.create_default_context(cafile=str(cert))
            try:
                with socket.create_connection(server.server_address, timeout=3) as connection:
                    with trusted.wrap_socket(connection, server_hostname="127.0.0.1"):
                        pass
                with socket.create_connection(server.server_address, timeout=3) as connection:
                    with self.assertRaises(ssl.SSLCertVerificationError):
                        trusted.wrap_socket(connection, server_hostname="127.0.0.2")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(3)
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
            self.assertEqual((key.parent / "token").stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
