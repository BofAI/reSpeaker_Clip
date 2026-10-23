"""Actual C burst sender: short reads, missing packets and lost summary replies."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class BurstSenderTest(unittest.TestCase):
    def test_actual_c_sender_repairs_without_buffering_whole_file(self):
        text = (ROOT / 'applications/upload_bench/src/dtls_bench.c').read_text()
        function = text[text.index('static int burst_transfer('):text.index('\nint dtls_bench_run(')]
        function = function.split('\nstatic int download_transfer(', 1)[0]
        fixture = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <sys/types.h>
#include "dtls_bench.h"
#define HEADER 16U
#define PAYLOAD 1024U
#define MIN(a,b) ((a)<(b)?(a):(b))
#define DIV_ROUND_UP(a,b) (((a)+(b)-1)/(b))
#define BIT(n) (1U<<(n))
#define FS_SEEK_SET 0
enum {START=1,DATA,QUERY,ACK,FINISH,DONE,SUMMARY,MISSING};
struct fs_file_t {size_t offset;};
struct channel {int (*check)(void);struct dtls_bench_result *result;};
static uint8_t input[70001], batch[65536], packet[1040], digest[32];
static bool seen[69], dropped[69];
static int queries, data_packets, seeks, cancel_at, checks, eof_on, reads;
static bool always_timeout, bad_bitmap;
static uint64_t upload_perf_now(void) {return 0;}
static int64_t k_uptime_get(void) {return 0;}
static int check(void) {return ++checks==cancel_at?-ECANCELED:0;}
static void notify(uint32_t sent,uint32_t confirmed) {assert(sent<=sizeof(input));assert(!confirmed || confirmed==sizeof(input));}
static void sys_put_be32(uint32_t n,uint8_t *p) {p[0]=n>>24;p[1]=n>>16;p[2]=n>>8;p[3]=n;}
static uint32_t sys_get_be32(const uint8_t *p) {return (uint32_t)p[0]<<24|(uint32_t)p[1]<<16|(uint32_t)p[2]<<8|p[3];}
static void header(uint8_t *p,uint8_t kind,uint32_t off,uint32_t n) {memset(p,0,16);memcpy(p,"CDB1",4);p[4]=kind;sys_put_be32(off,p+8);sys_put_be32(n,p+12);}
static ssize_t fs_read(struct fs_file_t *f,void *p,size_t n) {
 reads++;if(reads==eof_on)return 0;
 n=MIN(n,733U);assert(f->offset+n<=sizeof(input));memcpy(p,input+f->offset,n);f->offset+=n;return n;
}
static int fs_seek(struct fs_file_t *f,size_t offset,int whence) {assert(whence==0 && offset<sizeof(input));f->offset=offset;seeks++;return 0;}
static int send_frame(struct channel *s,uint8_t *p,size_t n) {
 int ret=s->check();if(ret)return ret;
 uint32_t off=sys_get_be32(p+8),size=sys_get_be32(p+12);
 assert(n==16+size);
 if(p[4]==SUMMARY){assert(off==sizeof(input) && !size);queries++;return 0;}
 assert(p[4]==DATA && off%1024==0 && off+size<=sizeof(input));
 assert(!memcmp(p+16,input+off,size));data_packets++;
 unsigned index=off/1024;
 if((index==3 || index==68) && !dropped[index])dropped[index]=true;
 else seen[index]=true;
 return 0;
}
static int read_frame(struct channel *s,uint8_t *p,size_t capacity,int64_t end) {
 (void)s;(void)end;assert(capacity==1040);
 if(always_timeout || queries==1)return -ETIMEDOUT;
 bool complete=true;uint8_t bits[9]={0};
 for(unsigned i=0;i<69;i++)if(!seen[i]){bits[i/8]|=1U<<(i%8);complete=false;}
 if(complete){header(p,DONE,sizeof(input),32);memcpy(p+16,digest,32);return 48;}
 if(bad_bitmap)bits[8]|=0x80;
 header(p,MISSING,sizeof(input),9);memcpy(p+16,bits,9);return 25;
}
static void reset(struct fs_file_t *f,struct dtls_bench_result *r) {
 memset(seen,0,sizeof(seen));memset(dropped,0,sizeof(dropped));memset(r,0,sizeof(*r));
 r->size=sizeof(input);f->offset=0;queries=data_packets=seeks=cancel_at=checks=eof_on=reads=0;always_timeout=bad_bitmap=false;
}
'''
        main = r'''
int main(void) {
 for(size_t i=0;i<sizeof(input);i++)input[i]=(uint8_t)(i*23);
 for(size_t i=0;i<32;i++)digest[i]=(uint8_t)i;
 struct fs_file_t file;struct dtls_bench_result r;struct channel s={check,&r};
 reset(&file,&r);assert(burst_transfer(&s,&file,batch,sizeof(batch),packet,digest,notify)==0);
 assert(r.confirmed==sizeof(input) && r.retransmissions==2 && r.packets==71);
 assert(data_packets==71 && seeks==2 && queries==3);
 reset(&file,&r);always_timeout=true;
 assert(burst_transfer(&s,&file,batch,sizeof(batch),packet,digest,notify)==-ETIMEDOUT);
 assert(data_packets==69 && queries==8 && !r.confirmed);
 reset(&file,&r);bad_bitmap=true;
 assert(burst_transfer(&s,&file,batch,sizeof(batch),packet,digest,notify)==-EBADMSG);
 assert(!seeks && !r.confirmed);
 reset(&file,&r);eof_on=2;
 assert(burst_transfer(&s,&file,batch,sizeof(batch),packet,digest,notify)==-EIO);
 assert(!r.confirmed);
 reset(&file,&r);cancel_at=2;
 assert(burst_transfer(&s,&file,batch,sizeof(batch),packet,digest,notify)==-ECANCELED);
 assert(!r.confirmed);return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            (p / 'burst.c').write_text(fixture + function + main)
            subprocess.run(['clang', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            '-fsanitize=address,undefined', '-I', str(ROOT / 'applications/clip/include'),
                            str(p / 'burst.c'), '-o', str(p / 'burst')], check=True)
            subprocess.run([str(p / 'burst')], check=True)
