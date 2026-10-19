"""Check passive packet observations and fail-closed SDK instrumentation."""
import hashlib
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import instrument_network

ROOT = Path(__file__).resolve().parents[2]


class NetworkTraceTest(unittest.TestCase):
    def test_sdk_copy_checks_hash_and_never_writes_original(self):
        text = "int net_recv_data(struct net_if *iface, struct net_pkt *pkt)\n{\n\tint ret;\n}\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sdk/net_core.c"
            source.parent.mkdir()
            source.write_text(text)
            spec = {"net_core.c": ("net_core.c", hashlib.sha256(text.encode()).hexdigest())}
            with patch.dict(instrument_network.SOURCES, spec, clear=True):
                instrument_network.generate(source.parent, root / "output")
                self.assertEqual(source.read_text(), text)
                self.assertIn("wifi_trace_rx(iface, pkt);", (root / "output/net_core.c").read_text())
                source.write_text(text + "/* SDK changed */")
                with self.assertRaisesRegex(ValueError, "Pinned SDK source changed"):
                    instrument_network.generate(source.parent, root / "rejected")
                self.assertFalse((root / "rejected").exists())
        with self.assertRaises(ValueError):
            instrument_network.replace_once("needle needle", "needle", "hook")

    def test_header_only_fragmented_packets_and_send_errors(self):
        source = (ROOT / "applications/clip/src/wifi_trace.c").read_text()
        source = re.sub(r"^#include .*\n", "", source, flags=re.MULTILINE)
        fixture = r'''
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <string.h>
#include <assert.h>
#define CONFIG_CLIP_WIFI_DIAG 1
#include "wifi_trace.h"
typedef int32_t atomic_t;
static int32_t atomic_get(atomic_t *v) { return *v; }
static void atomic_set(atomic_t *v, uint32_t n) { *v=(int32_t)n; }
struct k_spinlock { int unused; };
typedef int k_spinlock_key_t;
static int k_spin_lock(struct k_spinlock *l) { (void)l; return 0; }
static void k_spin_unlock(struct k_spinlock *l, int k) { (void)l; (void)k; }
static uint32_t k_uptime_get_32(void) { return 123; }
struct net_linkaddr { uint8_t *addr; uint8_t len; };
struct net_if { struct net_linkaddr link; };
static uint8_t own_mac[6]={2,3,4,5,6,7};
static struct net_if wifi={{own_mac,6}}, other;
static struct net_if *net_if_get_first_wifi(void) { return &wifi; }
static struct net_linkaddr *net_if_get_link_addr(struct net_if *i) { return &i->link; }
struct net_buf { const uint8_t *data; size_t len; struct net_buf *next; };
struct net_pkt { struct net_buf *buffer; int cursor; };
static size_t net_buf_linearize(void *dst, size_t size, const struct net_buf *b,
                               size_t offset, size_t count) {
    uint8_t *out=dst; size_t done=0;
    while (b && done<count && done<size) {
        if (offset>=b->len) { offset-=b->len; b=b->next; continue; }
        size_t n=b->len-offset;
        if (n>count-done) n=count-done;
        if (n>size-done) n=size-done;
        memcpy(out+done,b->data+offset,n); done+=n; offset=0; b=b->next;
    }
    return done;
}
'''
        checks = r'''
int main(void) {
    uint8_t raw[96]={0}, peer[4]={192,168,2,78}, local[4]={192,168,2,100};
    uint32_t p,l; memcpy(&p,peer,4); memcpy(&l,local,4); wifi_trace_peer(p,l);
    memcpy(raw,own_mac,6); raw[12]=8;raw[13]=6;
    raw[15]=1;raw[16]=8;raw[18]=6;raw[19]=4;raw[21]=2;
    memcpy(raw+28,peer,4);memcpy(raw+32,own_mac,6);memcpy(raw+38,local,4);
    struct net_buf tail={raw+17,25,NULL},head={raw,17,&tail};
    struct net_pkt pkt={&head,9}; uint8_t before[96];memcpy(before,raw,96);
    wifi_trace_rx(&wifi,&pkt);wifi_trace_rx(&other,&pkt);
    struct wifi_trace_stats s;wifi_trace_get(&s);
    assert(s.rx_peer==1 && s.rx_valid==1 && s.rx_ms==123);
    assert(pkt.cursor==9 && memcmp(before,raw,96)==0);
    raw[38]^=1;wifi_trace_rx(&wifi,&pkt);raw[38]^=1;
    tail.len=24;wifi_trace_rx(&wifi,&pkt);tail.len=25;
    wifi_trace_get(&s);assert(s.rx_peer==2 && s.rx_valid==1);
    wifi_trace_event(WIFI_TRACE_ARP_INPUT,&wifi,peer,0);
    wifi_trace_event(WIFI_TRACE_ARP_ACCEPT,&wifi,peer,0);
    wifi_trace_event(WIFI_TRACE_ARP_PENDING,&wifi,peer,0);
    wifi_trace_event(WIFI_TRACE_ARP_RELEASE,&wifi,peer,-12);
    wifi_trace_event(WIFI_TRACE_ARP_RELEASE,&other,peer,-12);
    wifi_trace_event(WIFI_TRACE_DRIVER_NOMEM,&wifi,NULL,0);
    wifi_trace_get(&s);
    assert(s.arp_input==1 && s.arp_accept==1 && s.arp_pending==1);
    assert(s.arp_release==1 && s.arp_release_error==1 && s.release_result==-12);
    assert(s.driver_nomem==1);
    memset(raw,0,sizeof(raw));raw[12]=8;raw[14]=0x4f;raw[23]=6;
    memcpy(raw+30,peer,4);raw[14+60+13]=2;tail.len=77;
    int kind=wifi_trace_tx_begin(&wifi,&pkt);assert(kind==2);
    wifi_trace_tx_end(kind,-105);wifi_trace_get(&s);
    assert(s.tx_syn==1 && s.tx_syn_error==1 && s.syn_result==-105 && s.syn_ms==123);
    raw[14+60+13]=0x12;assert(wifi_trace_tx_begin(&wifi,&pkt)==0);
    raw[14+60+13]=2;raw[21]=1;assert(wifi_trace_tx_begin(&wifi,&pkt)==0);
    assert(pkt.cursor==9);
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
