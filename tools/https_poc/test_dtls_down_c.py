"""Actual receiver C: reordered/lost data, partial writes and disk errors."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class DownReceiverTest(unittest.TestCase):
    def test_actual_c_download_verifies_saved_bytes_before_completion(self):
        text = (ROOT / 'applications/upload_bench/src/dtls_bench.c').read_text()
        function = text[text.index('static int download_transfer('):text.index('/* Receive ceiling diagnostic:')]
        fixture = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/types.h>
#include "dtls_bench.h"
#define HEADER 16U
#define PAYLOAD 1024U
#define MIN(a,b) ((a)<(b)?(a):(b))
#define DIV_ROUND_UP(a,b) (((a)+(b)-1)/(b))
#define BIT(n) (1U<<(n))
#define FS_SEEK_SET 0
#define FS_O_CREATE 1
#define FS_O_READ 2
#define FS_O_WRITE 4
enum {START=1,DATA,QUERY,ACK,FINISH,DONE,SUMMARY,MISSING,PULL};
struct fs_file_t {size_t position;};struct fs_dirent {int unused;};
struct channel {int (*check)(void);struct dtls_bench_result *result;};
typedef struct {unsigned sum;} mbedtls_sha256_context;
static uint8_t input[2500], saved[2500], packet[1040], batch[65536], expected[32];
static int reads,write_calls,closes,renames,done_calls,missing_calls,pulls,phase;
static bool synced,disk_error,invalid_packet;
static size_t file_length;
static uint64_t upload_perf_now(void){return 0;}
static int64_t k_uptime_get(void){return 0;}
static int check(void){return 0;}
static void notify(uint32_t n,uint32_t confirmed){assert(n<=2500 && (!confirmed || (synced && renames==1)));}
static void sys_put_be32(uint32_t n,uint8_t *p){p[0]=n>>24;p[1]=n>>16;p[2]=n>>8;p[3]=n;}
static uint32_t sys_get_be32(const uint8_t *p){return (uint32_t)p[0]<<24|(uint32_t)p[1]<<16|(uint32_t)p[2]<<8|p[3];}
static void header(uint8_t *p,uint8_t kind,uint32_t off,uint32_t n){memset(p,0,16);memcpy(p,"CDB1",4);p[4]=kind;sys_put_be32(off,p+8);sys_put_be32(n,p+12);}
static int fs_mkdir(const char *p){assert(!strcmp(p,"/SD:/BENCH"));return 0;}
static int fs_stat(const char *p,struct fs_dirent *e){(void)e;assert(strstr(p,"/SD:/BENCH/"));return -ENOENT;}
static void fs_file_t_init(struct fs_file_t *f){f->position=0;}
static int fs_open(struct fs_file_t *f,const char *p,int flags){(void)f;assert(strstr(p,".part") && flags==7);return 0;}
static int fs_seek(struct fs_file_t *f,size_t n,int mode){assert(mode==0 && n<=2500);if(n>file_length)return -EINVAL;f->position=n;return 0;}
static ssize_t fs_write(struct fs_file_t *f,const void *p,size_t n){
 write_calls++;if(disk_error)return -ENOSPC;n=MIN(n,377U);assert(f->position+n<=2500);memcpy(saved+f->position,p,n);f->position+=n;if(f->position>file_length)file_length=f->position;return n;
}
static ssize_t fs_read(struct fs_file_t *f,void *p,size_t n){assert(synced);reads++;n=MIN(n,733U);assert(f->position+n<=2500);memcpy(p,saved+f->position,n);f->position+=n;return n;}
static int fs_sync(struct fs_file_t *f){(void)f;synced=true;return 0;}
static int fs_close(struct fs_file_t *f){(void)f;closes++;return 0;}
static int fs_rename(const char *a,const char *b){assert(synced && reads && strstr(a,".part") && strstr(b,".bin"));renames++;return 0;}
static void mbedtls_sha256_init(mbedtls_sha256_context *h){h->sum=0;}
static int mbedtls_sha256_starts(mbedtls_sha256_context *h,int mode){(void)h;assert(!mode);return 0;}
static int mbedtls_sha256_update(mbedtls_sha256_context *h,const void *p,size_t n){const uint8_t *b=p;for(size_t i=0;i<n;i++)h->sum=h->sum*33+b[i];return 0;}
static int mbedtls_sha256_finish(mbedtls_sha256_context *h,uint8_t *p){memset(p,0,32);memcpy(p,&h->sum,4);return 0;}
static void mbedtls_sha256_free(mbedtls_sha256_context *h){(void)h;}
static int send_frame(struct channel *s,uint8_t *p,size_t n){(void)s;(void)n;
 if(p[4]==PULL){pulls++;return 0;}
 assert(p[4]==MISSING && sys_get_be32(p+12)==1 && p[16]==2);missing_calls++;return 0;
}
static int read_frame(struct channel *s,uint8_t *p,size_t cap,int64_t end){(void)s;(void)end;assert(cap==1040);
 if(phase==2 || phase==4){header(p,SUMMARY,2500,8);memset(p+16,0,8);phase++;return 24;}
 unsigned offsets[]={2048,0,0,1024,0};unsigned off=offsets[phase++];
 if(invalid_packet)off=2500;
 unsigned n=off<2500?MIN(1024U,2500-off):1;
 header(p,DATA,off,n);if(off<2500)memcpy(p+16,input+off,n);return 16+n;
}
static int control(struct channel *s,uint8_t *p,size_t n,uint8_t type,uint32_t off,uint8_t *reply,size_t cap){
 (void)s;(void)n;(void)cap;assert(synced && renames==1 && p[4]==DONE && type==ACK && off==2500);
 assert(!memcmp(saved,input,2500) && !memcmp(p+16,expected,32));done_calls++;
 header(reply,ACK,2500,32);memcpy(reply+16,expected,32);return 48;
}
static void reset(struct dtls_bench_result *r){memset(r,0,sizeof(*r));r->size=2500;strcpy(r->run,"1234567812345678");reads=write_calls=closes=renames=done_calls=missing_calls=pulls=phase=0;synced=disk_error=invalid_packet=false;file_length=0;memset(saved,0,2500);}
'''
        main = r'''
int main(void){
 for(size_t i=0;i<2500;i++)input[i]=(uint8_t)(i*17);
 mbedtls_sha256_context h;mbedtls_sha256_init(&h);mbedtls_sha256_update(&h,input,2500);mbedtls_sha256_finish(&h,expected);
 struct dtls_bench_result r;struct channel s={check,&r};
 reset(&r);assert(!download_transfer(&s,batch,sizeof(batch),packet,expected,notify));
 assert(r.confirmed==2500 && r.received_bytes==2500 && missing_calls==1 && done_calls==1 && closes==1);
 assert(!memcmp(saved,input,2500));
 reset(&r);disk_error=true;assert(download_transfer(&s,batch,sizeof(batch),packet,expected,notify)==-ENOSPC);
 assert(!renames && !done_calls && closes==1 && !r.confirmed);
 reset(&r);invalid_packet=true;assert(download_transfer(&s,batch,sizeof(batch),packet,expected,notify)==-EBADMSG);
 assert(write_calls && !renames && closes==1);return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            (p / 'down.c').write_text(fixture + function + main)
            subprocess.run(['clang', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            '-fsanitize=address,undefined', '-I', str(ROOT / 'applications/clip/include'),
                            str(p / 'down.c'), '-o', str(p / 'down')], check=True)
            subprocess.run([str(p / 'down')], check=True)
