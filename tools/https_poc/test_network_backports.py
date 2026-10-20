"""Run patched, pinned SDK C functions against state/queue fixtures.

Set CLIP_TEST_SDK to a read-only copy of the pinned SDK (two source files
suffice). Never download dependencies implicitly in a unit test.
"""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import backport_network
import instrument_network


def function(source, signature):
    start = source.index(signature)
    return source[start:source.index("\n}\n", start) + 3]


class NetworkBackportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = os.environ.get("CLIP_TEST_SDK")
        if not root:
            raise unittest.SkipTest("Set CLIP_TEST_SDK to the pinned SDK source fixture")
        cls.root = Path(root)
        cls.original = {}
        for name, (relative, expected) in backport_network.SOURCES.items():
            raw = (cls.root / relative).read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError(f"Incorrect SDK test fixture: {relative}")
            cls.original[name] = raw.decode()

    def c_test(self, source):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test.c").write_text(source)
            subprocess.run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror",
                            "-fsanitize=address,undefined", str(root / "test.c"),
                            "-o", str(root / "test")], check=True)
            subprocess.run([str(root / "test")], check=True)

    def test_reboot_nak_accepts_new_server_requesting_still_filters(self):
        source = backport_network.dhcp(self.original["dhcpv4.c"])
        fixture = r'''
#include <assert.h>
#include <string.h>
#include <stdint.h>
#define NET_DBG(...) ((void)0)
#define LOG_DBG(...) ((void)0)
enum {NET_DHCPV4_DISABLED,NET_DHCPV4_INIT,NET_DHCPV4_INIT_REBOOT,
 NET_DHCPV4_SELECTING,NET_DHCPV4_REQUESTING,NET_DHCPV4_BOUND,
 NET_DHCPV4_DECLINE,NET_DHCPV4_RENEWING,NET_DHCPV4_REBINDING};
struct address { uint32_t addr; };
struct lease { int state; struct address request_server_addr,response_src_addr,requested_ip; };
struct net_if { struct { struct lease dhcpv4; } config; };
static int restarted,immediate,removed;
static void dhcpv4_enter_selecting(struct net_if *i) { restarted++; i->config.dhcpv4.state=NET_DHCPV4_SELECTING; }
static void dhcpv4_immediate_timeout(struct lease *d) { (void)d; immediate++; }
static int net_if_ipv4_addr_rm(struct net_if *i,struct address *a) { (void)i;(void)a;removed++;return 1; }
'''
        checks = r'''
int main(void) {
 struct net_if i={0};
 i.config.dhcpv4.state=NET_DHCPV4_INIT_REBOOT;
 i.config.dhcpv4.request_server_addr.addr=2;i.config.dhcpv4.response_src_addr.addr=71;
 dhcpv4_handle_msg_nak(&i);assert(restarted==1 && immediate==1);
 i.config.dhcpv4.state=NET_DHCPV4_REQUESTING;
 dhcpv4_handle_msg_nak(&i);assert(restarted==1 && immediate==1);
 i.config.dhcpv4.response_src_addr.addr=2;
 dhcpv4_handle_msg_nak(&i);assert(restarted==2 && immediate==1);
 i.config.dhcpv4.state=NET_DHCPV4_BOUND;
 dhcpv4_handle_msg_nak(&i);assert(restarted==2 && removed==0);
 i.config.dhcpv4.state=NET_DHCPV4_RENEWING;
 dhcpv4_handle_msg_nak(&i);assert(restarted==3 && removed==1);
 return 0;
}
'''
        self.c_test(fixture + function(source, "static void dhcpv4_handle_msg_nak(") + checks)

    def test_unanswered_reboot_falls_back_after_one_request(self):
        source = backport_network.dhcp(self.original["dhcpv4.c"])
        timers = function(source, "static uint32_t dhcpv4_manage_timers(")
        start = timers.index("\tcase NET_DHCPV4_INIT_REBOOT:")
        branch = timers[start:timers.index("\tcase NET_DHCPV4_REQUESTING:", start)]
        fixture = r'''
#include <assert.h>
#define CONFIG_CLIP_DHCP_REBOOT_ATTEMPTS 1
enum {NET_DHCPV4_INIT_REBOOT,NET_DHCPV4_SELECTING};
struct net_if { struct { struct { int attempts,state; } dhcpv4; } config; };
static int requests,discovers;
static void dhcpv4_enter_selecting(struct net_if *i) { i->config.dhcpv4.state=NET_DHCPV4_SELECTING; }
static int dhcpv4_send_request(struct net_if *i) { i->config.dhcpv4.attempts++;requests++;return 10; }
static int dhcpv4_send_discover(struct net_if *i) { (void)i;discovers++;return 20; }
static int timer(struct net_if *iface) { switch(iface->config.dhcpv4.state) {
'''
        checks = r'''
default:return -1;} }
int main(void) {
 struct net_if i={0};i.config.dhcpv4.state=NET_DHCPV4_INIT_REBOOT;
 assert(timer(&i)==10 && requests==1 && discovers==0);
 assert(timer(&i)==20 && requests==1 && discovers==1);
 assert(i.config.dhcpv4.state==NET_DHCPV4_SELECTING);return 0;
}
'''
        self.c_test(fixture + branch + checks)

    def test_cleared_arp_entry_is_locked_and_immediately_reusable(self):
        source = backport_network.arp(self.original["arp.c"])
        fixture = r'''
#include <assert.h>
#include <stdbool.h>
#include <errno.h>
#include <stddef.h>
#define K_FOREVER 0
struct net_if { int unused; };struct net_in_addr { int unused; };
struct arp_entry { int node; };static struct arp_entry entry;
static int arp_mutex,arp_free_entries,depth,free_count;static bool pending=true;
static void k_mutex_lock(int *m,int wait) { assert(m==&arp_mutex && wait==0 && depth==0);depth++; }
static void k_mutex_unlock(int *m) { assert(m==&arp_mutex && depth==1);depth--; }
static struct arp_entry *arp_entry_get_pending(struct net_if *i,struct net_in_addr *d) {
 (void)i;(void)d;assert(depth==1);if (!pending)return NULL;pending=false;return &entry;
}
static void arp_entry_cleanup(struct arp_entry *e,bool release) { assert(e==&entry && release && depth==1 && !pending); }
static void sys_slist_prepend(int *list,int *node) { assert(list==&arp_free_entries && node==&entry.node && depth==1);free_count++; }
'''
        checks = r'''
int main(void) {
 struct net_if i;struct net_in_addr a;
 assert(net_arp_clear_pending(&i,&a)==0 && free_count==1 && depth==0);
 assert(net_arp_clear_pending(&i,&a)==-ENOENT && free_count==1 && depth==0);
 return 0;
}
'''
        self.c_test(fixture + function(source, "int net_arp_clear_pending(") + checks)

    def test_pending_packet_destination_is_arp_entry_not_ip_bytes(self):
        source = backport_network.arp(self.original["arp.c"])
        start = source.index("/* Set the dst in the pending packet */")
        statement = source[start:source.index("NET_DBG(", start)]
        fixture = r'''
#include <assert.h>
#include <stdint.h>
#include <string.h>
struct net_eth_addr { uint8_t addr[6]; };
struct arp_entry { struct net_eth_addr eth; };
struct net_pkt { uint8_t ip[20],dst[6]; };
static uint8_t *net_pkt_lladdr_dst(struct net_pkt *p) { return p->dst; }
static int net_linkaddr_set(uint8_t *dst,const uint8_t *src,unsigned int n) { assert(n==6);memcpy(dst,src,n);return 0; }
static void update(struct arp_entry *entry,struct net_pkt *pkt) {
'''
        checks = r'''
}
int main(void) {
 struct arp_entry e={{{2,3,4,5,6,7}}};struct net_pkt p={{0x45,0,0,40,1,2},{0}};
 update(&e,&p);assert(memcmp(p.dst,e.eth.addr,6)==0);return 0;
}
'''
        self.c_test(fixture + statement + checks)

    def test_generation_is_build_local_and_rejects_sdk_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            backport_network.generate(self.root, out / "patched", True, True)
            instrument_network.transform("arp.c", (out / "patched/arp.c").read_text())
            for name, (relative, _) in backport_network.SOURCES.items():
                self.assertEqual((self.root / relative).read_text(), self.original[name])
            fake = out / "changed"
            for name, (relative, _) in backport_network.SOURCES.items():
                p = fake / relative
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(self.original[name] + "/* changed */")
            with self.assertRaisesRegex(ValueError, "Pinned SDK source changed"):
                backport_network.generate(fake, out / "rejected", True, True)
            self.assertFalse((out / "rejected").exists())
