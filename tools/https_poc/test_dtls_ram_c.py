"""Compile the real RAM receive loop; reject corrupt data and count duplicates."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class RamReceiveTest(unittest.TestCase):
    def test_actual_loop_validates_reordered_payload_and_completion(self):
        text = (ROOT / 'applications/upload_bench/src/dtls_bench.c').read_text()
        function = text[text.index('static int download_ram('):text.index('\nint dtls_bench_run(')]
        fixture = r'''
#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include "dtls_bench.h"
#define HEADER 16U
#define PAYLOAD 1024U
#define DIV_ROUND_UP(a,b) (((a)+(b)-1)/(b))
#define BIT(n) (1U<<(n))
enum{START=1,DATA,QUERY,ACK,FINISH,DONE,SUMMARY,MISSING,PULL,CREDIT};
struct channel{struct dtls_bench_result *result;bool raw;uint8_t cookie[32];};
static unsigned phase,credits,done;static bool corrupt;
static uint64_t upload_perf_now(void){return 0;}
static int64_t k_uptime_get(void){return 0;}
static uint32_t sys_get_be32(const uint8_t*p){return (uint32_t)p[0]<<24|(uint32_t)p[1]<<16|(uint32_t)p[2]<<8|p[3];}
static void put(uint8_t*p,uint32_t n){p[0]=n>>24;p[1]=n>>16;p[2]=n>>8;p[3]=n;}
static void header(uint8_t*p,int k,uint32_t off,uint32_t n){memset(p,0,16);p[4]=k;put(p+8,off);put(p+12,n);}
static void notify(uint32_t n,uint32_t confirmed){assert(n<=2048 && confirmed<=2048);}
static int send_frame(struct channel*s,uint8_t*p,size_t n){(void)s;
 if(p[4]==PULL){assert(n==(s->raw?48U:16U));if(s->raw)assert(!memcmp(p+16,s->cookie,32));return 0;}
 assert(p[4]==CREDIT && sys_get_be32(p+8)==2048 && n==17 && p[16]==3);credits++;return 0;
}
static int read_frame(struct channel*s,uint8_t*p,size_t n,int64_t end){(void)s;(void)end;assert(n==1040);
 if(phase++==0){header(p,ACK,UINT32_MAX,s->raw?36:4);put(p+16,0);if(s->raw)memcpy(p+20,s->cookie,32);return s->raw?52:20;}
 assert(phase<=4);unsigned offsets[]={1024,1024,0};unsigned off=offsets[phase-2];
 header(p,DATA,off,1024);for(unsigned i=0;i<1024;i++)p[16+i]=(uint8_t)(off+i);
 if(corrupt)p[30]^=1;return 1040;
}
static int control(struct channel*s,uint8_t*p,size_t n,uint8_t type,uint32_t off,uint8_t*reply,size_t capacity){
 (void)s;assert(n==48 && type==ACK && off==2048 && capacity==65536 && p[4]==DONE && credits==1);
 done++;header(reply,ACK,2048,32);memcpy(reply+16,p+16,32);return 48;
}
'''
        main = r'''
int main(void){
 uint8_t bitmap[65536],packet[1040],digest[32]={1,2};
 struct dtls_bench_result r={.size=2048};struct channel s={.result=&r};
 assert(!download_ram(&s,bitmap,sizeof(bitmap),packet,digest,notify));
 assert(r.received_bytes==2048 && r.confirmed==2048 && r.packets==3 && r.duplicates==1 && done==1);
 assert(!r.fs_read_us && !r.fs_write_us && !r.sync_us && !r.actual_sha256[0]);
 memset(&r,0,sizeof(r));r.size=2048;phase=credits=done=0;s.raw=true;memset(s.cookie,42,32);
 assert(!download_ram(&s,bitmap,sizeof(bitmap),packet,digest,notify));
 assert(r.received_bytes==2048 && r.confirmed==2048 && done==1);
 memset(&r,0,sizeof(r));r.size=2048;phase=credits=done=0;corrupt=true;
 assert(download_ram(&s,bitmap,sizeof(bitmap),packet,digest,notify)==-EBADMSG);
 assert(!r.received_bytes && !r.confirmed && !done);return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            (p / 'ram.c').write_text(fixture + function + main)
            subprocess.run(['clang', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            '-fsanitize=address,undefined', '-I', str(ROOT / 'applications/clip/include'),
                            str(p / 'ram.c'), '-o', str(p / 'ram')], check=True)
            subprocess.run([str(p / 'ram')], check=True)
