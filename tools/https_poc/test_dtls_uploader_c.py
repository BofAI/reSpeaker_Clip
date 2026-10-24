"""Actual C uploader against the durable Python receiver with a mock TLS BIO.

The shim transports application records over loopback UDP; it deliberately does
not claim to test crypto/certificate handling. It exercises the complete C
sender, cancellation, resume, selective repair and lost COMPLETE handling.
"""
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest

from dtls_upload_protocol import OPEN, DATA, COMPLETE, unpack
from dtls_upload_session import UploadSession
from resume_store import ResumeStore

ROOT = Path(__file__).resolve().parents[2]

SHIM = r'''
#define _POSIX_C_SOURCE 200809L
#define _DARWIN_C_SOURCE
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <stdio.h>
#include <time.h>
#include <unistd.h>
#include <sys/socket.h>
#include <netdb.h>
#include <poll.h>
#include "dtls_upload.h"
#include "upload_window.h"
#define ARG_UNUSED(x) (void)(x)
#define MIN(a,b) ((a)<(b)?(a):(b))
#define zsock_addrinfo addrinfo
#define zsock_pollfd pollfd
#define zsock_getaddrinfo getaddrinfo
#define zsock_freeaddrinfo freeaddrinfo
#define zsock_socket socket
#define zsock_connect connect
#define zsock_send send
#define zsock_recv recv
#define zsock_close close
static int shim_poll(struct pollfd*p,nfds_t n,int timeout){return poll(p,n,timeout);}
#define zsock_poll shim_poll
#define ZSOCK_MSG_DONTWAIT MSG_DONTWAIT
#define ZSOCK_POLLIN POLLIN
#define ZSOCK_POLLOUT POLLOUT
#define ZSOCK_POLLNVAL POLLNVAL
#ifndef CONFIG_CLIP_DTLS_WINDOW_PACKETS
#define CONFIG_CLIP_DTLS_WINDOW_PACKETS 64
#endif
#ifndef CONFIG_CLIP_DTLS_INITIAL_WINDOW_PACKETS
#define CONFIG_CLIP_DTLS_INITIAL_WINDOW_PACKETS 16
#endif
#define k_calloc calloc
#define k_free free
#define PSA_SUCCESS 0
#define MBEDTLS_ERR_SSL_INTERNAL_ERROR -101
#define MBEDTLS_ERR_SSL_WANT_READ -102
#define MBEDTLS_ERR_SSL_WANT_WRITE -103
#define MBEDTLS_ERR_NET_SEND_FAILED -104
#define MBEDTLS_ERR_NET_RECV_FAILED -105
#define MBEDTLS_ERR_SSL_TIMEOUT -106
#define MBEDTLS_ERR_SSL_PEER_CLOSE_NOTIFY -107
#define MBEDTLS_SSL_IS_CLIENT 1
#define MBEDTLS_SSL_TRANSPORT_DATAGRAM 2
#define MBEDTLS_SSL_PRESET_DEFAULT 0
#define MBEDTLS_SSL_VERIFY_REQUIRED 2
struct mbedtls_ssl_context {void*bio;int(*send)(void*,const unsigned char*,size_t);int(*recv)(void*,unsigned char*,size_t);};
typedef struct mbedtls_ssl_context mbedtls_ssl_context;
typedef struct {int unused;} mbedtls_ssl_config;
typedef struct {int unused;} mbedtls_x509_crt;
static int64_t k_uptime_get(void){struct timespec ts;assert(!clock_gettime(CLOCK_MONOTONIC,&ts));return (int64_t)ts.tv_sec*1000+ts.tv_nsec/1000000;}
static void sys_put_be32(uint32_t n,uint8_t*p){p[0]=n>>24;p[1]=n>>16;p[2]=n>>8;p[3]=n;}
static uint32_t sys_get_be32(const uint8_t*p){return (uint32_t)p[0]<<24|(uint32_t)p[1]<<16|(uint32_t)p[2]<<8|p[3];}
static uint16_t sys_get_be16(const uint8_t*p){return (uint16_t)p[0]<<8|p[1];}
static int psa_crypto_init(void){return 0;}
static int psa_generate_random(uint8_t*p,size_t n){static uint32_t seed=1;for(size_t i=0;i<n;i++)p[i]=(uint8_t)(seed+i);seed++;return 0;}
static void mbedtls_ssl_init(mbedtls_ssl_context*s){memset(s,0,sizeof(*s));}
static void mbedtls_ssl_config_init(mbedtls_ssl_config*s){}
static void mbedtls_x509_crt_init(mbedtls_x509_crt*s){}
static void mbedtls_ssl_free(mbedtls_ssl_context*s){}
static void mbedtls_ssl_config_free(mbedtls_ssl_config*s){}
static void mbedtls_x509_crt_free(mbedtls_x509_crt*s){}
static int mbedtls_ssl_close_notify(mbedtls_ssl_context*s){return 0;}
static int mbedtls_x509_crt_parse(mbedtls_x509_crt*s,const unsigned char*p,size_t n){return 0;}
static int mbedtls_ssl_config_defaults(mbedtls_ssl_config*s,int a,int b,int c){return 0;}
static void mbedtls_ssl_conf_authmode(mbedtls_ssl_config*s,int mode){assert(mode==MBEDTLS_SSL_VERIFY_REQUIRED);}
static void mbedtls_ssl_conf_ca_chain(mbedtls_ssl_config*s,mbedtls_x509_crt*c,void*x){}
static void mbedtls_ssl_conf_rng(mbedtls_ssl_config*s,int(*f)(void*,unsigned char*,size_t),void*x){}
static void mbedtls_ssl_conf_handshake_timeout(mbedtls_ssl_config*s,unsigned a,unsigned b){assert(a==1000 && b==8000);}
static int mbedtls_ssl_setup(mbedtls_ssl_context*s,mbedtls_ssl_config*c){return 0;}
static int mbedtls_ssl_set_hostname(mbedtls_ssl_context*s,const char*h){return 0;}
static void mbedtls_ssl_set_mtu(mbedtls_ssl_context*s,unsigned mtu){assert(mtu==1200);}
static void mbedtls_ssl_set_timer_cb(mbedtls_ssl_context*s,void*c,void(*set)(void*,uint32_t,uint32_t),int(*get)(void*)){}
static void mbedtls_ssl_set_bio(mbedtls_ssl_context*s,void*bio,int(*tx)(void*,const unsigned char*,size_t),int(*rx)(void*,unsigned char*,size_t),void*unused){s->bio=bio;s->send=tx;s->recv=rx;}
static int mbedtls_ssl_handshake(mbedtls_ssl_context*s){return 0;}
static unsigned mbedtls_ssl_get_verify_result(mbedtls_ssl_context*s){return 0;}
static int mbedtls_ssl_write(mbedtls_ssl_context*s,const void*p,size_t n){return s->send(s->bio,p,n);}
static int mbedtls_ssl_read(mbedtls_ssl_context*s,void*p,size_t n){return s->recv(s->bio,p,n);}
'''

MAIN = r'''
static bool cancel_requested,second;
static uint8_t data[180*1024+117];
static uint32_t saved;
static int check(void){return cancel_requested?-ECANCELED:0;}
static int read_data(uint32_t offset,uint8_t*out,size_t n){assert(offset+n<=sizeof(data));memcpy(out,data+offset,n);return 0;}
static void notify(uint32_t sent,uint32_t received,uint32_t durable,uint32_t resumed){
 assert(durable<=received && received<=sent && sent<=sizeof(data));
 if(!second && durable>=65536){saved=durable;cancel_requested=true;}
}
int main(int argc,char**argv){
 assert(argc==3);
 for(size_t i=0;i<sizeof(data);i++)data[i]=(uint8_t)(i*17+3);
 struct dtls_upload_identity id={.device="clip",.session="20261020080208",.segment=1,.size=sizeof(data),.credential="tttttttttttttttttttttttttttttttt"};
 for(int i=0;i<32;i++){unsigned byte;assert(sscanf(argv[2]+i*2,"%2x",&byte)==1);id.sha256[i]=byte;}
 struct dtls_upload*c=NULL;struct dtls_upload_stats first,last;
 assert(!dtls_upload_open(&c,"127.0.0.1",argv[1],(const unsigned char*)"CA",3,check));
#ifdef CONFIG_CLIP_DTLS_PROFILE
 assert(dtls_upload_set_window(NULL,16,64)==-EINVAL);
 assert(dtls_upload_set_window(c,15,64)==-EINVAL);
 assert(dtls_upload_set_window(c,128,64)==-EINVAL);
 assert(dtls_upload_set_window(c,16,129)==-EINVAL);
 assert(!dtls_upload_set_window(c,TEST_INITIAL_WINDOW,TEST_CEILING_WINDOW));
#endif
 assert(dtls_upload_file(c,&id,read_data,notify,&first)==-ECANCELED);
 assert(saved>=65536 && saved<sizeof(data));dtls_upload_close(c);
 second=true;cancel_requested=false;
 assert(!dtls_upload_open(&c,"127.0.0.1",argv[1],(const unsigned char*)"CA",3,check));
#ifdef CONFIG_CLIP_DTLS_PROFILE
 assert(!dtls_upload_set_window(c,TEST_INITIAL_WINDOW,TEST_CEILING_WINDOW));
#endif
 assert(!dtls_upload_file(c,&id,read_data,notify,&last));
 assert(last.resume_from==saved && last.durable==sizeof(data) && last.received==sizeof(data));
 assert(first.retransmissions+last.retransmissions>=1);
#ifdef CONFIG_CLIP_DTLS_PROFILE
 assert(first.timing.valid && last.timing.valid);
 assert(first.timing.source_calls && last.timing.source_calls);
 assert(last.timing.tx_calls && last.timing.rx_calls && last.timing.poll_read_us);
 assert(!last.timing.encrypt_calls); /* This fixture deliberately bypasses TLS. */
#endif
 dtls_upload_close(c);
 printf("{\"resume\":%u,\"received\":%u,\"retries\":%u}\n",last.resume_from,last.received,first.retransmissions+last.retransmissions);
 return 0;
}
'''


class ActualUploaderTest(unittest.TestCase):
    def test_actual_c_cancel_resume_repair_and_lost_complete(self):
        self.run_sender(False)

    def test_profiled_c_cancel_resume_repair_and_lost_complete(self):
        self.run_sender(True)

    def test_profiled_128_packet_window_with_loss_and_resume(self):
        self.run_sender(True, 128, 128)

    def test_unprofiled_128_packet_defaults_with_loss_and_resume(self):
        self.run_sender(False, 128, 128)

    def run_sender(self, profiled, initial=16, ceiling=64):
        with tempfile.TemporaryDirectory() as directory, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            root = Path(directory)
            store = ResumeStore(root, threading.Lock())
            sock.bind(('127.0.0.1', 0)); sock.settimeout(.02)
            stopped = threading.Event()
            errors, peers, sessions = [], [], []
            lost = {2048, 90112}
            lost_complete = False

            def receiver():
                nonlocal lost_complete
                current = None
                session = None
                try:
                    while not stopped.is_set():
                        try:
                            packet, peer = sock.recvfrom(2048)
                        except socket.timeout:
                            if session:
                                for response in session.tick():
                                    sock.sendto(response, current)
                            continue
                        kind, offset, cid, _ = unpack(packet)
                        if peer != current:
                            if session:
                                session.close()
                            current = peer
                            peers.append(peer)
                            session = UploadSession(store, {'clip': 't' * 32})
                            sessions.append(session)
                        if kind == DATA and offset in lost:
                            lost.remove(offset)
                            continue
                        for response in session.feed(packet):
                            if unpack(response)[0] == COMPLETE and not lost_complete:
                                lost_complete = True
                                continue
                            sock.sendto(response, peer)
                except Exception as error:
                    errors.append(error)
                finally:
                    if session:
                        session.close()

            thread = threading.Thread(target=receiver, daemon=True)
            text = (ROOT / 'applications/clip/src/dtls_upload.c').read_text()
            source = '\n'.join(line for line in text.splitlines() if not line.startswith('#include'))
            support = ''
            if profiled:
                support = r'''
#define CONFIG_SYS_CLOCK_TICKS_PER_SEC 1000000U
typedef void *atomic_ptr_t;
static void *atomic_ptr_get(atomic_ptr_t *p){return *p;}
static void atomic_ptr_set(atomic_ptr_t *p,void *v){*p=v;}
static void atomic_ptr_clear(atomic_ptr_t *p){*p=NULL;}
static void *k_current_get(void){return (void*)1;}
static uint64_t k_uptime_ticks(void){struct timespec ts;assert(!clock_gettime(CLOCK_MONOTONIC,&ts));return (uint64_t)ts.tv_sec*1000000+ts.tv_nsec/1000;}
static uint64_t k_ticks_to_us_floor64(uint64_t t){return t;}
typedef int psa_status_t;
typedef uint32_t mbedtls_svc_key_id_t;
typedef uint32_t psa_algorithm_t;
#define psa_aead_encrypt(...) 0
#define psa_aead_decrypt(...) 0
'''
                text = (ROOT / 'applications/clip/src/dtls_profile.c').read_text()
                support += '\n'.join(line for line in text.splitlines() if not line.startswith('#include'))
            (root / 'client.c').write_text(SHIM + support + source + MAIN)
            subprocess.run(['clang', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                            '-DCONFIG_CLIP_DTLS_INITIAL_WINDOW_PACKETS='+str(initial),
                            '-DCONFIG_CLIP_DTLS_WINDOW_PACKETS='+str(ceiling),
                            *(['-DCONFIG_CLIP_DTLS_PROFILE=1', '-DTEST_INITIAL_WINDOW='+str(initial),
                               '-DTEST_CEILING_WINDOW='+str(ceiling)] if profiled else []),
                            '-fsanitize=address,undefined', '-I', str(ROOT / 'applications/clip/include'),
                            str(root / 'client.c'), str(ROOT / 'applications/clip/src/upload_window.c'),
                            '-o', str(root / 'client')], check=True)
            content = bytes((i * 17 + 3) % 256 for i in range(180 * 1024 + 117))
            digest = hashlib.sha256(content).hexdigest()
            thread.start()
            try:
                output = subprocess.run([str(root / 'client'), str(sock.getsockname()[1]), digest],
                                        capture_output=True, text=True, timeout=30)
            finally:
                stopped.set(); thread.join(timeout=1)
            self.assertFalse(errors, errors)
            self.assertEqual(output.returncode, 0, output.stderr)
            result = json.loads(output.stdout)
            self.assertGreaterEqual(result['resume'], 65536)
            self.assertTrue(lost_complete)
            self.assertEqual(len(peers), 2)
            self.assertEqual(len(list(root.glob('*.bin'))), 1)
            self.assertEqual(next(root.glob('*.bin')).read_bytes(), content)
            self.assertFalse(list(root.glob('*.partial')))
            self.assertFalse(list(root.glob('*.checkpoint')))
