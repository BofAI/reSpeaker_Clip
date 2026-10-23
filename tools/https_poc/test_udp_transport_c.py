"""Actual C send/read wrappers: UDP bypasses TLS and excludes stale sessions."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class UDPTransportTest(unittest.TestCase):
    def test_actual_c_udp_transport(self):
        text = (ROOT / 'applications/upload_bench/src/dtls_bench.c').read_text()
        functions = text[text.index('static int send_frame('):text.index('static int control(')]
        fixture = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include "dtls_bench.h"
#define HEADER 16U
#define ZSOCK_MSG_DONTWAIT 1
#define ZSOCK_MSG_TRUNC 2
#define MBEDTLS_ERR_SSL_WANT_READ -101
#define MBEDTLS_ERR_SSL_WANT_WRITE -102
#define MBEDTLS_ERR_NET_RECV_FAILED -103
struct channel{int fd;bool raw;uint8_t tag[7];int ssl;int(*check)(void);struct dtls_bench_result*result;};
static uint8_t queue[3][1041],wire[1040];static unsigned sizes[3],index_rx,n_rx;static int64_t now;
static uint64_t upload_perf_now(void){return 0;}
static int64_t k_uptime_get(void){return now;}
static int check(void){return 0;}
static uint32_t sys_get_be32(const uint8_t*p){return (uint32_t)p[0]<<24|(uint32_t)p[1]<<16|(uint32_t)p[2]<<8|p[3];}
static int wait_slice(struct channel*s,int want,int64_t end){(void)s;(void)want;now=end;return -ETIMEDOUT;}
static int transmit(void*s,const unsigned char*p,size_t n){(void)s;assert(n<=1040);memcpy(wire,p,n);return n;}
static int zsock_recv(int fd,void*p,size_t n,int flags){(void)fd;assert(flags==3);now++;
 if(index_rx>=n_rx){errno=EAGAIN;return -1;}
 unsigned size=sizes[index_rx];memcpy(p,queue[index_rx],size<n?size:n);index_rx++;return size;
}
static int mbedtls_ssl_write(void*s,const void*p,size_t n){(void)s;(void)p;(void)n;assert(!"TLS called in plain UDP mode");return -1;}
static int mbedtls_ssl_read(void*s,void*p,size_t n){(void)s;(void)p;(void)n;assert(!"TLS called in plain UDP mode");return -1;}
'''
        main = r'''
int main(void){
 struct dtls_bench_result r={0};struct channel s={.raw=true,.check=check,.result=&r};memcpy(s.tag,"1234567",7);
 uint8_t p[1040]={0};memcpy(p,"CDB1",4);p[4]=2;p[15]=4;memcpy(p+16,"data",4);
 assert(!send_frame(&s,p,20));assert(!memcmp(wire,"1234",4) && !memcmp(wire+5,"567",3) && !memcmp(wire+16,"data",4));
 memcpy(queue[0],wire,20);queue[0][0]^=1;sizes[0]=20;
 memcpy(queue[1],wire,20);sizes[1]=1041;
 memcpy(queue[2],wire,20);sizes[2]=20;n_rx=3;
 assert(read_frame(&s,p,sizeof(p),100)==20);assert(!memcmp(p,"CDB1",4) && !memcmp(p+5,"\0\0\0",3) && !memcmp(p+16,"data",4));
 assert(index_rx==3);assert(read_frame(&s,p,sizeof(p),100)==-ETIMEDOUT);
 index_rx=0;n_rx=1;now=0;memcpy(queue[0],wire,20);queue[0][15]=5;sizes[0]=20;
 assert(read_frame(&s,p,sizeof(p),100)==-EBADMSG);return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            (p / 'udp.c').write_text(fixture + functions + main)
            subprocess.run(['clang', '-std=c11', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
                            '-I', str(ROOT / 'applications/clip/include'), str(p / 'udp.c'), '-o', str(p / 'udp')], check=True)
            subprocess.run([str(p / 'udp')], check=True)
