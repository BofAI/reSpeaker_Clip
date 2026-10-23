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
    def test_command_dispatch_preserves_validation_busy_cancel_and_start(self):
        source = (ROOT / "applications/clip/src/https_poc.c").read_text()
        parsing = "static char *split_field" + source.split("static char *split_field", 1)[1].split(
            "static void request_cancel", 1)[0]
        parsing += "static bool valid_id" + source.split("static bool valid_id", 1)[1].split(
            "static int hash_file", 1)[0]
        handlers = "static int configure_network" + source.split("static int configure_network", 1)[1].split(
            "int https_poc_register", 1)[0]
        code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <errno.h>
#define TRANSPORT_TYPE_USB 1
#define AT_CMD_TYPE_SET 1
#define CONFIG_CLIP_HTTPS_TUNING 1
#define CLIP_STATE_IDLE 0
#define AT_ERR_NOMEM 12
#include "upload_tune.h"
static struct upload_tuning tuning={16384,2048,8192,1}, job_tuning;
static uint32_t tune_generation=1, job_generation;
static int job_server_chunk, job_socket_verified, device_state, pending, saves;
static int clip_event_get_state(void) { return device_state; }
#define CLOCK_REALTIME 0
#define CLIP_EVENT_UPLOAD_BEGIN 1
#define CLIP_EVENT_OK 0
struct timespec { long long tv_sec; long tv_nsec; };
struct at_cmd_ctx { int transport_type, type; const char *name, *args; };
struct clip_event_result_info { int result; };
struct wifi_station_network { char ip[16], netmask[16], gateway[16]; };
static int active, cancelled, stalled, last_error, bytes_sent, stage, tls_step, start_job;
static int confirmed, resume_from, total_bytes;
static int state_error, state_queries;
#include "upload_config.h"
static struct upload_config settings = {.clock_host="clock", .clock_port="123"};
#define ssid settings.ssid
#define password settings.password
#define host settings.host
#define port settings.port
#define device_id settings.device
#define token settings.token
static char session[32];
static int auto_job, auto_paused, config_error, scan_error, control_lock, save_error;
static bool config_saved;
static unsigned int failures, cursor_segment;
static char cursor_session[15];
static long long next_auto, time_synced_at;
#define K_FOREVER 0
#define MAX(a,b) ((a)>(b)?(a):(b))
static long long k_uptime_get(void) { return 123; }
static void k_mutex_lock(int *m,int wait) { assert(!*m);*m=1;(void)wait; }
static void k_mutex_unlock(int *m) { assert(*m);*m=0; }
static void atomic_set(int *v,int n) { *v=n; }
static void atomic_clear(int *v) { *v=0; }
int upload_config_save(struct upload_config *c) { (void)c; saves++;return save_error; }
int upload_config_clear(void) { return save_error; }
int upload_config_valid(const struct upload_config *c) { (void)c;return 1; }
static int list_states(const char *a,char *o,size_t n) { (void)a;(void)o;(void)n;return -ENOENT; }
static uint32_t segment;
static int time_ready;
static const char *names[] = {"idle", "preparing"}, *tls_steps[] = {"idle"};
enum { PREPARING = 1 };
static int event_error, event_result, interrupts, resets, starts, clock_error;
static long long wall_time;
static bool https_poc_active(void) { return active; }
static bool https_poc_record_pending(void) { return pending; }
static int atomic_get(int *v) { return *v; }
static void request_cancel(void) { cancelled = 1; interrupts++; }
static int state_query(const char *args, char *out, size_t length) {
    assert(args && out && length); state_queries++; return -ENOENT;
}
static void wifi_diag_reset(void) { resets++; }
static void k_sem_give(int *sem) { (*sem)++; starts++; }
static const char *wifi_station_last_step(void) { return "idle"; }
static void wifi_station_get_network(struct wifi_station_network *n) { memset(n, 0, sizeof(*n)); }
static int clip_post_event_sync(int event, struct clip_event_result_info *result) {
    assert(event == CLIP_EVENT_UPLOAD_BEGIN); result->result = event_result;
    if (!event_error && !event_result) {
        active = true; cancelled = stalled = last_error = bytes_sent = 0; stage = PREPARING;
    }
    return event_error;
}
static int clock_settime(int clock, const struct timespec *ts) {
    assert(clock == CLOCK_REALTIME);
    if (clock_error) { errno = EIO; return -1; }
    wall_time = ts->tv_sec; return 0;
}
'''
        code += parsing + handlers
        code += r'''
static void request(const char *name, const char *args, int transport, int type, int expected) {
    struct at_cmd_ctx ctx = {.name=name, .args=args, .transport_type=transport, .type=type};
    char response[1024], error[32];
    int n = command(&ctx, response, sizeof(response));
    assert(n > 0 && (size_t)n == strlen(response));
    snprintf(error, sizeof(error), "\"error\":%d,", expected);
    assert(strstr(response, error));
    assert(strstr(response, expected ? "\"ok\":false" : "\"ok\":true"));
    assert(!strstr(response, "test-password"));
}
int main(void) {
    struct at_cmd_ctx tune_ctx={1,0,"UPLOADTUNE",NULL};char tune_json[1024];
    assert(command(&tune_ctx,tune_json,sizeof(tune_json))>0);
    assert(strstr(tune_json,"\"persistent\":false") && tune_generation==1);
    request("UPLOADTUNE","32768,4096,8192,2",1,AT_CMD_TYPE_SET,-EINVAL);
    request("UPLOADTUNE","32768,4096,8192,1",0,AT_CMD_TYPE_SET,-EACCES);
    assert(tuning.chunk_size==16384 && tuning.write_size==2048 && tune_generation==1);
    tune_ctx.type=AT_CMD_TYPE_SET;tune_ctx.args="32768,4096,5461,0";
    assert(command(&tune_ctx,tune_json,sizeof(tune_json))>0);
    assert(tuning.chunk_size==32768 && tuning.write_size==4096 && tuning.send_window==5461 && !tuning.nodelay);
    assert(tune_generation==2 && !saves);
    job_tuning=tuning;job_generation=tune_generation;
    active=1;request("UPLOADTUNE","16384,2048,8192,1",1,AT_CMD_TYPE_SET,-EBUSY);active=0;
    pending=1;request("UPLOADTUNE","16384,2048,8192,1",1,AT_CMD_TYPE_SET,-EBUSY);pending=0;
    device_state=1;request("UPLOADTUNE","16384,2048,8192,1",1,AT_CMD_TYPE_SET,-EBUSY);device_state=0;
    settings.enabled=1;request("UPLOADTUNE","16384,2048,8192,1",1,AT_CMD_TYPE_SET,-EBUSY);settings.enabled=0;
    assert(tune_generation==2 && tuning.chunk_size==32768);
    tune_ctx.type=0;tune_ctx.args=NULL;active=1;
    assert(command(&tune_ctx,tune_json,sizeof(tune_json))>0);active=0;
    tune_ctx.type=AT_CMD_TYPE_SET;tune_ctx.args="16384,2048,8192,1";
    assert(command(&tune_ctx,tune_json,sizeof(tune_json))>0);
    assert(tuning.chunk_size==16384 && job_tuning.chunk_size==32768 && job_generation==2 && !saves);
    assert(tune_reply(tune_json,8)<0);
    request("NETCFG", "test,test-password", 0, AT_CMD_TYPE_SET, -EACCES);
    assert(!ssid[0]);
    request("NETCFG", "test,short", 1, AT_CMD_TYPE_SET, -EINVAL);
    request("NETCFG", "test,test-password", 1, AT_CMD_TYPE_SET, 0);
    assert(!strcmp(ssid, "test") && !strcmp(password, "test-password"));
    request("UPLOADCFG", "receiver,8443,fixture", 1, AT_CMD_TYPE_SET, -EINVAL);
    request("UPLOADCFG", "receiver,0,fixture,aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", 1, AT_CMD_TYPE_SET, -EINVAL);
    assert(!host[0]);
    request("UPLOADCFG", "receiver,8443,fixture,aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", 1, AT_CMD_TYPE_SET, 0);
    assert(!strcmp(host, "receiver") && !strcmp(port, "8443"));
    request("UPLOADSTATE", "20261019070446:1", 1, AT_CMD_TYPE_SET, -ENOENT);
    assert(state_queries == 1);
    request("UPTIME", "invalid", 1, AT_CMD_TYPE_SET, -EINVAL);
    clock_error = 1;
    request("UPTIME", "1800000000", 1, AT_CMD_TYPE_SET, -EIO);
    assert(!time_ready);
    clock_error = 0;
    request("UPTIME", "1800000000", 1, AT_CMD_TYPE_SET, 0);
    assert(time_ready && wall_time == 1800000000LL);
    request("UPLOAD", "20261019070446:0", 1, AT_CMD_TYPE_SET, -EINVAL);
    request("UPLOAD", "session:1", 1, AT_CMD_TYPE_SET, -EINVAL);
    request("UPLOAD", "2026101907044x:1", 1, AT_CMD_TYPE_SET, -EINVAL);
    event_error = -EIO;
    request("UPLOAD", "20261019070446:1", 1, AT_CMD_TYPE_SET, -EBUSY);
    event_error = 0; event_result = 1;
    request("UPLOAD", "20261019070446:1", 1, AT_CMD_TYPE_SET, -EBUSY);
    assert(!starts && !resets);
    event_result = 0; cancelled = stalled = last_error = bytes_sent = 1;
    request("UPLOAD", "20261019070446:1", 1, AT_CMD_TYPE_SET, 0);
    assert(active && starts == 1 && resets == 1 && stage == PREPARING);
    request("UPLOADSTATE", "20261019070446:1", 1, AT_CMD_TYPE_SET, -EBUSY);
    assert(state_queries == 1);
    assert(segment == 1 && !strcmp(session, "20261019070446"));
    assert(!cancelled && !stalled && !last_error && !bytes_sent);
    request("UPLOAD", NULL, 1, 0, 0);
    request("NETCFG", "other,test-password", 1, AT_CMD_TYPE_SET, -EBUSY);
    assert(!strcmp(ssid, "test"));
    request("UPLOADCANCEL", NULL, 1, 0, 0);
    assert(cancelled && interrupts == 1 && auto_paused);
    active = 0;
    request("UPLOADAUTO", "on", 1, AT_CMD_TYPE_SET, 0);
    assert(settings.enabled && config_saved && !auto_paused);
    request("NETCFG", "other,test-password", 1, AT_CMD_TYPE_SET, -EBUSY);
    auto_job = active = 1;
    request("UPLOADAUTO", "off", 1, AT_CMD_TYPE_SET, 0);
    assert(!settings.enabled && auto_paused && cancelled);
    active = 0; save_error = -EIO;
    request("UPLOADAUTO", "on", 1, AT_CMD_TYPE_SET, -EIO);
    assert(!settings.enabled && !config_saved);
    save_error = 0;
    request("UPLOADCLEAR", NULL, 1, 0, 0);
    assert(!ssid[0] && !host[0] && !config_saved);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test.c").write_text(code)
            subprocess.run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror",
                            "-fsanitize=address,undefined", "-I"+str(ROOT / "applications/clip/include"), str(root / "test.c"),
                            str(ROOT / 'applications/clip/src/upload_tune.c'),
                            "-o", str(root / "test")], check=True)
            subprocess.run([str(root / "test")], check=True)

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
                            "-fsanitize=address,undefined", "-I"+str(ROOT / "applications/clip/include"), str(root / "test.c"),
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
            code += 'static bool busy, pending;\nstatic bool https_poc_active(void) {return busy;}\n'
            code += 'static bool https_poc_record_pending(void) {return pending;}\n'
            code += 'bool https_poc_command_allowed' + policy
            code += '''
int main(void) {
    for (int state=0; state<2; state++) {
        busy=state;
        assert(https_poc_command_allowed("RES"));
        assert(https_poc_command_allowed("NETDIAG"));
        assert(https_poc_command_allowed("NETTRACE"));
        assert(https_poc_command_allowed("NETTX"));
        assert(https_poc_command_allowed("GSTAT"));
        assert(!https_poc_command_allowed("PAIR"));
        assert(!https_poc_command_allowed("DELETE"));
        assert(!https_poc_command_allowed("WIFI"));
        assert(https_poc_command_allowed("UPLOADSTATE") == !busy);
    }
    assert(!https_poc_command_allowed("NETCFG"));
    assert(!https_poc_command_allowed("UPLOADSTATE"));
    assert(https_poc_command_allowed("START"));
    assert(https_poc_command_allowed("STOP"));
    busy=false;
    pending=true;
    assert(!https_poc_command_allowed("NETCFG"));
    assert(https_poc_command_allowed("START"));
    pending=false;
    assert(https_poc_command_allowed("START"));
    return 0;
}
'''
            (root / "test.c").write_text(code)
            subprocess.run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror",
                            "-fsanitize=address,undefined", "-I"+str(ROOT / "applications/clip/include"), str(root / "test.c"),
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
