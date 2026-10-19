"""Exercise the actual diagnostic C code with a passive network API fixture."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class NetdiagTest(unittest.TestCase):
    def test_cached_queries_reset_bounds_and_transport(self):
        source = (ROOT / "applications/clip/src/wifi_diag.c").read_text()
        source = re.sub(r"^#include .*\n", "", source, flags=re.MULTILINE)
        fixture = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#define CONFIG_CLIP_WIFI_DIAG 1
#include "wifi_diag.h"
#define ARG_UNUSED(x) (void)(x)
#define TRANSPORT_TYPE_USB 2
#define AT_CMD_SET 1
#define AT_ERR_NOMEM 4
#define NET_REQUEST_STATS_GET_ALL 1
typedef int atomic_t;
static int atomic_get(int *v) { return *v; }
static void atomic_inc(int *v) { ++*v; }
static void atomic_set(int *v, int n) { *v=n; }
struct k_spinlock { int unused; };
typedef int k_spinlock_key_t;
static int k_spin_lock(struct k_spinlock *l) { (void)l; return 0; }
static void k_spin_unlock(struct k_spinlock *l, int k) { (void)l; (void)k; }
static uint32_t k_uptime_get_32(void) { return 100; }
struct net_if { int unused; };
struct net_linkaddr { int unused; };
static struct net_if wifi, other;
static struct net_if *net_if_get_first_wifi(void) { return &wifi; }
static bool net_if_is_admin_up(struct net_if *i) { (void)i; return true; }
static bool net_if_is_carrier_ok(struct net_if *i) { (void)i; return true; }
static bool net_if_is_dormant(struct net_if *i) { (void)i; return false; }
static bool net_if_is_up(struct net_if *i) { (void)i; return true; }
struct net_if_link_cb { int unused; };
static void net_if_register_link_cb(struct net_if_link_cb *c,
    void (*fn)(struct net_if *, struct net_linkaddr *, int)) { (void)c; (void)fn; }
struct net_stats {
    struct { uint64_t sent, received; } bytes;
    struct { uint32_t sent, recv, drop; } ipv4, tcp;
    uint32_t processing_error;
};
static int queries;
static int net_mgmt(int request, struct net_if *iface, void *out, size_t size) {
    assert(request == NET_REQUEST_STATS_GET_ALL && iface == &wifi);
    assert(size == sizeof(struct net_stats)); queries++;
    struct net_stats s={.bytes={UINT64_C(0x100000002),3},.tcp={1,0,0}};
    memcpy(out,&s,size); return 0;
}
struct at_cmd_ctx { int transport_type; const char *args; };
struct at_command {
    const char *name; int flags;
    int (*handler)(struct at_cmd_ctx *, char *, size_t);
};
static int at_server_register_cmd(const struct at_command *c) { (void)c; return 0; }
'''
        checks = r'''
int main(void) {
    assert(wifi_diag_register() == 0);
    char out[1024];
    struct at_cmd_ctx ctx={TRANSPORT_TYPE_USB,"3"};
    wifi_diag_reset();
    diagnostic(&ctx,out,sizeof(out));
    assert(strstr(out,"\"valid\":false") && queries == 0);
    link_done(&other,NULL,-1);
    link_done(&wifi,NULL,0); link_done(&wifi,NULL,-5);
    wifi_diag_capture(WIFI_DIAG_CONNECT_END,-116);
    assert(queries == 1);
    diagnostic(&ctx,out,sizeof(out));
    assert(queries == 1); /* Cached reads never call network management. */
    assert(strstr(out,"\"valid\":true") && strstr(out,"\"tx_bytes\":2"));
    assert(strstr(out,"\"result\":-116") && strstr(out,"\"flags\":11"));
    assert(strstr(out,"\"link_ok\":1") && strstr(out,"\"link_error\":1"));
    char small[8];
    assert(diagnostic(&ctx,small,sizeof(small)) == -AT_ERR_NOMEM && small[0] == 0);
    const char *bad[]={"", "4", "-1", "10", "0x0", NULL};
    for (unsigned i=0;i<sizeof(bad)/sizeof(bad[0]);i++) {
        ctx.args=bad[i]; diagnostic(&ctx,out,sizeof(out));
        assert(strstr(out,"\"ok\":false"));
    }
    ctx.args="3"; ctx.transport_type=0;
    diagnostic(&ctx,out,sizeof(out)); assert(strstr(out,"\"ok\":false"));
    wifi_diag_capture((enum wifi_diag_point)-1,0); assert(queries == 1);
    wifi_diag_reset(); ctx.transport_type=TRANSPORT_TYPE_USB;
    diagnostic(&ctx,out,sizeof(out));
    assert(strstr(out,"\"valid\":false") && strstr(out,"\"job\":2"));
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test.c").write_text(fixture + source + checks)
            subprocess.run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror",
                            "-fsanitize=address,undefined", "-I",
                            str(ROOT / "applications/clip/include"), str(root / "test.c"),
                            "-o", str(root / "test")], check=True)
            subprocess.run([str(root / "test")], check=True)


if __name__ == "__main__":
    unittest.main()
