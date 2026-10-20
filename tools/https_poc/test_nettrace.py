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

    def test_fmac_generator_expression_preserves_condition(self):
        cmake = (ROOT / "applications/clip/wifi-trace.cmake").read_text()
        start = cmake.index("function(clip_trace_source ")
        function = cmake[start:cmake.index("endfunction()", start) + len("endfunction()")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            zephyr = root / "sdk/zephyr"
            original = root / "sdk/modules/lib/nrf_wifi/fw_if/umac_if/src/system/tx.c"
            generated = root / "generated/fmac_tx.c"
            zephyr.mkdir(parents=True)
            original.parent.mkdir(parents=True)
            generated.parent.mkdir()
            original.write_text("int original(void) { return 1; }\n")
            generated.write_text("int observed(void) { return 1; }\n")
            project = f'''cmake_minimum_required(VERSION 3.20)
project(trace_fixture C)
set(ZEPHYR_BASE "{zephyr}")
set(_trace_dir "{generated.parent}")
set(CONFIG_NRF70_DATA_TX y)
add_library(nrf-wifi-osal STATIC "$<$<BOOL:y>:{original}>")
{function}
clip_trace_source(nrf-wifi-osal ../modules/lib/nrf_wifi/fw_if/umac_if/src/system/tx.c fmac_tx.c)
get_target_property(actual nrf-wifi-osal SOURCES)
file(WRITE "${{CMAKE_BINARY_DIR}}/sources.txt" "${{actual}}")
'''
            (root / "CMakeLists.txt").write_text(project)
            subprocess.run(["cmake", "-S", str(root), "-B", str(root / "build")],
                           check=True, capture_output=True, text=True)
            self.assertEqual((root / "build/sources.txt").read_text(),
                             f"$<$<BOOL:y>:{generated}>")
            self.assertEqual(original.read_text(), "int original(void) { return 1; }\n")

    def test_firmware_event_status_bounds(self):
        fixture = r"""
#include <assert.h>
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#define CONFIG_NRF70_MAX_TX_AGGREGATION 4
struct __attribute__((packed)) nrf_wifi_tx_buff_done {
    struct { uint32_t cmd,len; } umac_head;
    uint8_t tx_desc_num,num_tx_status_code,timestamp_t1[6],timestamp_t4[6];
    uint8_t tx_status_code[];
};
static int available, calls;
static unsigned int seen_count;
void wifi_tx_trace_done(unsigned int desc,unsigned int count,const uint8_t *status,
                        const uint8_t *tx,const uint8_t *ack) {
    assert(desc==2 && tx[0]==3 && ack[5]==4);
    calls++; seen_count=count; available=status!=NULL;
    if (status) { for (unsigned int i=0;i<count;i++) assert(status[i]==i); }
}
"""
        checks = r"""
int main(void) {
    struct nrf_wifi_tx_buff_done *event=calloc(1,sizeof(*event)+4);
    assert(event);event->tx_desc_num=2;event->timestamp_t1[0]=3;event->timestamp_t4[5]=4;
    for (unsigned int i=0;i<4;i++) event->tx_status_code[i]=i;
    event->num_tx_status_code=4;event->umac_head.len=sizeof(*event)+4;
    clip_trace_fmac_done(event);assert(available && calls==1 && seen_count==4);
    event->umac_head.len--;clip_trace_fmac_done(event);assert(!available);
    event->num_tx_status_code=5;event->umac_head.len=sizeof(*event)+5;
    clip_trace_fmac_done(event);assert(!available && seen_count==5);
    free(event);
    event=calloc(1,sizeof(*event));assert(event);event->tx_desc_num=2;
    event->timestamp_t1[0]=3;event->timestamp_t4[5]=4;
    event->num_tx_status_code=1;event->umac_head.len=sizeof(*event);
    clip_trace_fmac_done(event);assert(!available); /* No byte beyond allocation. */
    free(event);return 0;
}
"""
        adapter = instrument_network.FMAC_DONE_ADAPTER.replace('#include "wifi_tx_trace.h"', '')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test.c").write_text(fixture + adapter + checks)
            subprocess.run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror",
                            "-fsanitize=address,undefined", str(root / "test.c"),
                            "-o", str(root / "test")], check=True)
            subprocess.run([str(root / "test")], check=True)

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
#include "wifi_tx_trace.h"
typedef int32_t atomic_t;
static int32_t atomic_get(atomic_t *v) { return *v; }
static void atomic_set(atomic_t *v, uint32_t n) { *v=(int32_t)n; }
struct k_spinlock { int unused; };
typedef int k_spinlock_key_t;
static int k_spin_lock(struct k_spinlock *l) { (void)l; return 0; }
static void k_spin_unlock(struct k_spinlock *l, int k) { (void)l; (void)k; }
static uint32_t now=123;
static uint32_t k_uptime_get_32(void) { return now; }
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
    assert(s.timeline_valid==4 && s.rx_first_ms==123);
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
    /* Connection timeline resets without destroying cumulative counters.
     * Zero and uptime wrap are valid timestamps, not missing samples. */
    wifi_trace_peer(p,l);wifi_trace_get(&s);
    assert(s.timeline_valid==0 && s.rx_first_ms==0 && s.rx_valid==1);
    memset(raw,0,sizeof(raw));raw[12]=8;raw[13]=6;raw[21]=1;
    memcpy(raw+38,peer,4);now=0;
    assert(wifi_trace_tx_begin(&wifi,&pkt)==1);
    wifi_trace_tx_end(1,-12);
    now=3000;assert(wifi_trace_tx_begin(&wifi,&pkt)==1);
    now=6000;assert(wifi_trace_tx_begin(&wifi,&pkt)==1);
    raw[21]=2;assert(wifi_trace_tx_begin(&wifi,&pkt)==0); /* No ARP replies. */
    wifi_trace_get(&s);
    assert(s.timeline_valid==3 && s.arp_first_ms==0 && s.arp_second_ms==3000);
    assert(s.arp_last_ms==6000 && s.tx_arp==3 && s.tx_arp_error==1);
    memcpy(raw,own_mac,6);raw[15]=1;raw[16]=8;raw[18]=6;raw[19]=4;
    memcpy(raw+28,peer,4);memcpy(raw+32,own_mac,6);memcpy(raw+38,local,4);
    now=6100;wifi_trace_rx(&wifi,&pkt);
    now=6200;wifi_trace_rx(&wifi,&pkt);wifi_trace_get(&s);
    assert(s.timeline_valid==7 && s.rx_first_ms==6100 && s.rx_ms==6200);
    memset(raw,0,sizeof(raw));raw[12]=8;raw[14]=0x45;raw[23]=6;
    memcpy(raw+30,peer,4);raw[14+20+13]=2;
    now=UINT32_MAX;assert(wifi_trace_tx_begin(&wifi,&pkt)==2);
    now=9;assert(wifi_trace_tx_begin(&wifi,&pkt)==2);wifi_trace_get(&s);
    assert(s.timeline_valid==15 && s.syn_first_ms==UINT32_MAX && s.syn_ms==9);
    wifi_trace_peer(p,l);wifi_trace_get(&s);
    assert(s.timeline_valid==0 && s.arp_first_ms==0 && s.arp_second_ms==0);
    assert(s.arp_last_ms==0 && s.syn_first_ms==0 && s.tx_arp==3);
    /* Firmware-command records are bounded, keyed by descriptor/frame,
     * and distinguish failed submission, unavailable status and completion. */
    memset(raw,0,sizeof(raw)); raw[12]=8; raw[13]=6;raw[15]=1;
    raw[16]=8;raw[18]=6;raw[19]=4;raw[21]=1;
    memcpy(raw+28,local,4);memcpy(raw+38,peer,4);
    struct wifi_tx_trace tx;uint32_t job,total;
    _Static_assert(sizeof(tx)==32,"Keep the bounded trace RAM budget");
    wifi_tx_trace_reset(9);
    wifi_tx_trace_prepare(2,1,raw,42);
    wifi_tx_trace_get(0,&tx,&job,&total);assert(job==9 && total==0);
    wifi_trace_peer(p,l);
    wifi_tx_trace_prepare(2,1,raw,41); /* Short header. */
    raw[28]^=1;wifi_tx_trace_prepare(2,1,raw,42);raw[28]^=1;
    wifi_tx_trace_prepare(65536,1,raw,42);
    now=0;wifi_tx_trace_prepare(2,1,raw,42);
    uint8_t status[2]={1,0},phy[6]={0,1,2,3,4,5};
    now=1;wifi_tx_trace_submit(99,0,true);
    wifi_tx_trace_get(0,&tx,&job,&total);assert(tx.flags==WIFI_TX_PREPARED);
    wifi_tx_trace_submit(2,0,true);
    now=2;wifi_tx_trace_done(2,2,status,phy,phy);
    now=3;wifi_tx_trace_done(2,2,status,phy,phy);
    wifi_tx_trace_get(0,&tx,&job,&total);
    assert(total==1 && tx.prepared_ms==0 && tx.submit_ms==1 && tx.done_ms==2);
    assert(tx.status==0 && tx.status_count==2 && tx.flags==111);
    assert(!memcmp(tx.phy_tx,phy,6));
    now=10;wifi_tx_trace_prepare(2,0,raw,42); /* Descriptor reuse. */
    wifi_tx_trace_submit(2,1,true);wifi_tx_trace_done(2,2,status,phy,phy);
    wifi_tx_trace_get(1,&tx,&job,&total);
    assert(tx.submit_result==1 && !(tx.flags&WIFI_TX_DONE));
    now=20;wifi_tx_trace_prepare(3,3,raw,42);
    wifi_tx_trace_done(3,2,status,NULL,NULL); /* Frame index absent. */
    wifi_tx_trace_submit(3,0,true); /* Completion can precede return. */
    wifi_tx_trace_get(2,&tx,&job,&total);
    assert((tx.flags&WIFI_TX_DONE) && !(tx.flags&WIFI_TX_STATUS_VALID));
    assert(tx.status==255 && tx.phy_tx[0]==0 && tx.status_count==2);
    wifi_tx_trace_prepare(4,0,raw,42);wifi_tx_trace_get(3,&tx,&job,&total);
    assert(total==4 && tx.flags==0); /* No out-of-bounds fourth slot. */
    wifi_tx_trace_reset(10);wifi_trace_peer(p,l);
    wifi_tx_trace_prepare(5,0,raw,42);wifi_tx_trace_submit(5,1,false);
    wifi_tx_trace_done(5,1,status,phy,phy);wifi_tx_trace_get(0,&tx,&job,&total);
    assert(job==10 && total==1 && tx.flags==19 && tx.submit_result==1);
    wifi_tx_trace_reset(11);wifi_tx_trace_done(5,1,status,phy,phy);
    wifi_tx_trace_get(0,&tx,&job,&total);assert(job==11 && total==0 && tx.flags==0);
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
