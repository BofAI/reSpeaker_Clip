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
    def test_cleanup_before_radio_start_is_safe_and_idempotent(self):
        source = (ROOT / "applications/clip/src/wifi_station.c").read_text()
        cleanup = source.split("int wifi_off(void)", 1)[1].split(
            "bool wifi_is_interface_up", 1)[0]
        code = '''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <errno.h>
#define CONFIG_NRF70_SR_COEX 1
#define NET_REQUEST_WIFI_DISCONNECT 1
struct net_if { bool up; };
static struct net_if radio;
static bool present = true;
static int got_ip, disconnects, dhcp_stops, resets, downs;
static struct net_if *net_if_get_first_wifi(void) { return present ? &radio : NULL; }
static bool net_if_is_admin_up(struct net_if *iface) { return iface->up; }
static void atomic_clear(int *value) { *value = 0; }
static int net_mgmt(int request, struct net_if *iface, void *data, size_t length) {
    assert(request == NET_REQUEST_WIFI_DISCONNECT && iface->up);
    assert(data == NULL && length == 0); disconnects++; return 0;
}
static void net_dhcpv4_stop(struct net_if *iface) { assert(iface->up); dhcp_stops++; }
static void nrf_wifi_coex_hw_reset(void) { assert(radio.up); resets++; }
static int net_if_down(struct net_if *iface) { iface->up = false; downs++; return 0; }
'''
        code += "int wifi_off(void)" + cleanup
        code += '''
int main(void) {
    got_ip = 1;
    assert(wifi_off() == 0 && got_ip == 0);
    assert(disconnects == 0 && dhcp_stops == 0 && resets == 0 && downs == 0);
    radio.up = true; got_ip = 1;
    assert(wifi_off() == 0 && got_ip == 0 && !radio.up);
    assert(disconnects == 1 && dhcp_stops == 1 && resets == 1 && downs == 1);
    assert(wifi_off() == 0);
    assert(disconnects == 1 && dhcp_stops == 1 && resets == 1 && downs == 1);
    present = false;
    assert(wifi_off() == -ENODEV);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test.c").write_text(code)
            subprocess.run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror",
                            "-fsanitize=address,undefined", str(root / "test.c"),
                            "-o", str(root / "test")], check=True)
            subprocess.run([str(root / "test")], check=True)

    def test_resources_remain_readable_during_upload(self):
        source = (ROOT / "applications/clip/src/https_poc.c").read_text()
        policy = source.split("bool https_poc_command_allowed", 1)[1].split(
            "bool https_poc_event", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code = '#include <stdbool.h>\n#include <string.h>\n#include <assert.h>\n'
            code += '#define CONFIG_CLIP_WIFI_DIAG 1\n#define IS_ENABLED(x) (x)\n'
            code += 'static bool busy;\nstatic bool https_poc_active(void) {return busy;}\n'
            code += 'bool https_poc_command_allowed' + policy
            code += '''
int main(void) {
    for (int state=0; state<2; state++) {
        busy=state;
        assert(https_poc_command_allowed("RES"));
        assert(https_poc_command_allowed("NETDIAG"));
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
