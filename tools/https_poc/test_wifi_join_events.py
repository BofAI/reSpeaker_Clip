"""Actual station functions: diagnostics preserve callback codes and control results."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class WifiJoinEventsTest(unittest.TestCase):
    def test_station_callbacks_and_request_returns_are_observed_passively(self):
        source = (ROOT / 'applications/clip/src/wifi_station.c').read_text()
        source = re.sub(r'^#include .*\n', '', source, flags=re.MULTILINE)
        fixture = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <stdio.h>
#include <errno.h>
#define CONFIG_CLIP_WIFI_DIAG 1
#include "wifi_diag.h"
#define AF_INET 2
#define SOCK_DGRAM 2
#define NET_ADDR_PREFERRED 1
#define NET_REQUEST_WIFI_REG_DOMAIN 1
#define NET_REQUEST_WIFI_CONNECT 2
#define NET_REQUEST_WIFI_DISCONNECT 3
#define NET_EVENT_IPV4_DHCP_BOUND 1
#define NET_EVENT_WIFI_CONNECT_RESULT 2
#define NET_EVENT_WIFI_DISCONNECT_RESULT 4
#define WIFI_MGMT_SET 1
#define WIFI_SECURITY_TYPE_PSK 1
#define WIFI_MFP_OPTIONAL 1
#define WIFI_CHANNEL_ANY 0
#define WIFI_FREQ_BAND_2_4_GHZ 1
#define K_MSEC(v) (v)
#define K_SEM_DEFINE(name,a,b) int name
struct k_spinlock {int unused;};
typedef int k_spinlock_key_t;
static int k_spin_lock(struct k_spinlock *l){(void)l;return 0;}
static void k_spin_unlock(struct k_spinlock *l,int key){(void)l;(void)key;}
typedef int atomic_t;
static int atomic_get(int *v){return *v;}
static void atomic_set(int *v,int x){*v=x;}
static void atomic_clear(int *v){*v=0;}
static uint32_t now;
static int64_t k_uptime_get(void){return now;}
static void k_sleep(int n){now+=n;}
static void k_sem_reset(int *s){*s=0;}
static void k_sem_give(int *s){*s=1;}
static int k_sem_take(int *s,int ms){(void)s;now+=ms;return 0;}
struct net_if {bool admin;};
static struct net_if iface,other;
static struct net_if *net_if_get_first_wifi(void){return &iface;}
static int net_if_up(struct net_if *n){n->admin=true;return 0;}
static int net_if_down(struct net_if *n){n->admin=false;return 0;}
static bool net_if_is_admin_up(struct net_if *n){return n->admin;}
struct net_in_addr {int value;};
static struct net_in_addr ip;
static struct net_in_addr *net_if_ipv4_get_global_addr(struct net_if *n,int state)
{(void)n;(void)state;return &ip;}
static struct net_in_addr net_if_ipv4_get_netmask_by_addr(struct net_if *n,struct net_in_addr *a)
{(void)n;return *a;}
static struct net_in_addr net_if_ipv4_get_gw(struct net_if *n){(void)n;return ip;}
static const char *net_addr_ntop(int family,const void *a,char *b,size_t size)
{(void)family;(void)a;snprintf(b,size,"192.0.2.1");return b;}
struct wifi_station_network {char ip[16],netmask[16],gateway[16];};
struct wifi_status {int status;};
struct net_mgmt_event_callback {void *info;size_t info_length;};
static void net_mgmt_init_event_callback(struct net_mgmt_event_callback *cb,
 void (*fn)(struct net_mgmt_event_callback *,uint64_t,struct net_if *),uint64_t events)
{(void)cb;(void)fn;(void)events;}
static void net_mgmt_add_event_callback(struct net_mgmt_event_callback *cb){(void)cb;}
typedef struct {void (*wifi_ready_cb)(bool);} wifi_ready_callback_t;
static int register_wifi_ready_callback(wifi_ready_callback_t cb,struct net_if *n)
{(void)n;cb.wifi_ready_cb(true);return 0;}
struct wifi_reg_domain {int oper;char country_code[2];};
struct wifi_connect_req_params {
 const uint8_t *ssid,*psk;size_t ssid_length,psk_length;int security,mfp,channel,band;
};
static const char *config_get_wifi_reg_domain(void){return "US";}
static void net_dhcpv4_start(struct net_if *n){(void)n;}
static void net_dhcpv4_stop(struct net_if *n){(void)n;}
static int request_result,connect_status,requests,query_calls;
static bool disconnect_payload=true;
static int net_mgmt(int op,struct net_if *n,void *data,size_t length);
struct saved {enum wifi_join_event event;int status;};
static struct saved events[64];
static unsigned int count,begins;
static bool initially_ready;
void wifi_diag_join_begin(bool r){initially_ready=r;begins++;count=0;}
void wifi_diag_join_event(enum wifi_join_event event,int status)
{assert(count<64);events[count++]=(struct saved){event,status};}
void wifi_diag_capture(enum wifi_diag_point point,int result)
{(void)point;(void)result;query_calls++;}
'''
        checks = r'''
static int net_mgmt(int op,struct net_if *n,void *data,size_t length)
{
 assert(n==&iface);
 if(op==NET_REQUEST_WIFI_REG_DOMAIN){assert(length==sizeof(struct wifi_reg_domain));return 0;}
 requests++;
 if(op==NET_REQUEST_WIFI_CONNECT){
  assert(length==sizeof(struct wifi_connect_req_params));
  struct wifi_connect_req_params *p=data;assert(p->ssid_length==7 && p->band==WIFI_FREQ_BAND_2_4_GHZ);
  if(request_result)return request_result;
  struct wifi_status s={connect_status};
  wifi_events.info=&s;wifi_events.info_length=sizeof(s);
  network_event(&wifi_events,NET_EVENT_WIFI_CONNECT_RESULT,n);
  if(!connect_status)network_event(&ipv4_events,NET_EVENT_IPV4_DHCP_BOUND,n);
  return 0;
 }
 assert(op==NET_REQUEST_WIFI_DISCONNECT);
 struct wifi_status s={3};wifi_events.info=disconnect_payload?&s:NULL;
 wifi_events.info_length=disconnect_payload?sizeof(s):0;
 network_event(&wifi_events,NET_EVENT_WIFI_DISCONNECT_RESULT,n);
 return 0;
}
static unsigned int found(enum wifi_join_event kind,int status)
{unsigned int n=0;for(unsigned int i=0;i<count;i++)n+=events[i].event==kind && events[i].status==status;return n;}
int main(void)
{
 atomic_t cancelled=0;
 assert(wifi_init()==0);
 assert(wifi_station_connect("fixture","fixture-pass",&cancelled)==0);
 assert(begins==1 && initially_ready && wifi_is_sta_connected());
 assert(found(WIFI_JOIN_REQUEST,0)==1 && found(WIFI_JOIN_RESULT,0)==1 && found(WIFI_JOIN_DHCP,0)==1);
 assert(found(WIFI_JOIN_REQUEST_RETURN,0)==1);
 assert(!strcmp(wifi_station_last_step(),"connected"));
 assert(wifi_off()==0 && !wifi_is_sta_connected());
 assert(found(WIFI_JOIN_DISCONNECTED,3)==1 && found(WIFI_JOIN_DISCONNECT_RETURN,0)==1 && found(WIFI_JOIN_DOWN,0)==1);
 connect_status=17;
 assert(wifi_station_connect("fixture","fixture-pass",&cancelled)==-ENETUNREACH);
 assert(begins==2 && found(WIFI_JOIN_RESULT,17)==1 && found(WIFI_JOIN_REQUEST_RETURN,0)==1);
 assert(!found(WIFI_JOIN_DHCP,0) && !strcmp(wifi_station_last_step(),"dhcp"));
 assert(wifi_off()==0);
 request_result=-EIO;
 assert(wifi_station_connect("fixture","fixture-pass",&cancelled)==-EIO);
 assert(found(WIFI_JOIN_REQUEST_RETURN,-EIO)==1 && !found(WIFI_JOIN_RESULT,17));
 unsigned int before=count;struct wifi_status s={99};
 struct net_mgmt_event_callback cb={&s,sizeof(s)};
 network_event(&cb,NET_EVENT_WIFI_CONNECT_RESULT,&other);assert(count==before);
 disconnect_payload=false;assert(wifi_off()==0);
 assert(found(WIFI_JOIN_DISCONNECTED_NO_INFO,0)==1);
 /* Request counts match the unchanged station policy: one connect each,
  * one disconnect per cleanup; the diagnostic hooks issue no requests. */
 assert(requests==6 && query_calls==2);
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'test.c').write_text(fixture + source + checks)
            subprocess.run(['clang', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            '-fsanitize=address,undefined', '-I', str(ROOT / 'applications/clip/include'),
                            str(root / 'test.c'), '-o', str(root / 'test')], check=True)
            subprocess.run([str(root / 'test')], check=True)
