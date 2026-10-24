"""Run the real per-file/batch worker with deterministic transport and storage."""
from pathlib import Path
import unittest
from test_auto_upload import compile_run

ROOT = Path(__file__).resolve().parents[2]


class BatchWorkerTest(unittest.TestCase):
    def worker_fixture(self):
        source = (ROOT/'applications/clip/src/https_poc.c').read_text()
        worker = source[source.index('static int upload_file('):source.index('/* Full ECDSA handshakes')]
        code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
#include <setjmp.h>
#include "upload_state.h"
#include "recording_hash.h"
#define ARG_UNUSED(x) (void)(x)
#define K_SECONDS(x) (x)
#define K_MSEC(x) (x)
#define MAX_FILE (8U*1024U*1024U)
#define STORAGE_PATH_MAX 128
#define FS_DIR_ENTRY_FILE 1
#define FS_O_READ 1
#define CLIP_EVENT_UPLOAD_END 1
enum {TLS_SETUP_HTTP,TLS_SETUP_IDLE};
enum {PREPARING,CONNECTING,TLS,SUCCEEDED,FAILED,CANCELLED};
struct https_stream {int live;};
struct fs_file_t {int open;};
struct fs_dirent {int type;uint32_t size;};
struct k_work_sync {int unused;};
struct clip_event_result_info {int unused;};
static int stage,tls_step,bytes_sent,confirmed,resume_from,total_bytes,state_error;
static int cleanup_error,cancelled,stalled,last_error,stall_work,start_job;
static int active,attempt,connections,disconnects,uploads,wifi_starts,saves,stored,partial;
static int mode,last_scheduled,ended,auto_job,target=3;
static int64_t now,deadline;
static bool radio,response_keep_alive;
static size_t file_length;
static char session[32]="20261021000000",ssid[]="fixture",password[]="fixture-pass";
static char expected_hash[65]="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
static uint32_t segment;
static struct fs_file_t file;
static struct upload_buffers {char dummy;} *buffers;
static struct https_stream transport;
static jmp_buf done;
enum {REUSE,LEGACY,DROPPED,PROTOCOL,CANCEL_BETWEEN,CANCEL_PARTIAL,METADATA,CLOSE_ERROR,RADIO_ERROR,ALLOC_ERROR,ROLLING,SCAN_ERROR,SCAN_CANCEL};
static void atomic_set(int *p,int n){*p=n;}
static void atomic_clear(int *p){*p=0;}
static int atomic_get(int *p){return *p;}
static int64_t k_uptime_get(void){return now;}
static int check_cancel(void){return cancelled?-ECANCELED:stalled?-ETIMEDOUT:now>=deadline?-ETIMEDOUT:0;}
static int k_sem_take(int *p,int t){(void)p;(void)t;if(ended)longjmp(done,1);return 1;}
static int begin_auto(void){auto_job=active=1;attempt=1;segment=1;return 1;}
static bool next_auto_file(void){if(check_cancel()||attempt>=target)return false;segment=++attempt;return true;}
static int refill_auto_batch(void){
 if(mode==ROLLING&&attempt==3){target=5;return 1;}
 if(mode==SCAN_ERROR)return -EIO;
 if(mode==SCAN_CANCEL){cancelled=1;return -ECANCELED;}
 return 0;
}
static void *k_malloc(size_t n){return mode==ALLOC_ERROR?NULL:malloc(n);}
static void k_free(void *p){free(p);}
static void k_work_cancel_delayable_sync(int *w,struct k_work_sync *s){(void)w;(void)s;}
static void k_sleep(int t){(void)t;assert(0);}
static int storage_ensure_mounted(void){return 0;}
static int storage_build_chunk_path(const char *id,uint32_t n,char *p,size_t l){(void)id;snprintf(p,l,"%u",n);return 0;}
static int fs_stat(const char *p,struct fs_dirent *e){assert(atoi(p)==attempt);e->type=FS_DIR_ENTRY_FILE;e->size=20000+attempt;return 0;}
static void fs_file_t_init(struct fs_file_t *f){assert(!f->open);}
static int fs_open(struct fs_file_t *f,const char *p,int flag){(void)p;(void)flag;assert(active&&!f->open);f->open=1;return 0;}
static int fs_close(struct fs_file_t *f){assert(f->open);f->open=0;return mode==CLOSE_ERROR?-EIO:0;}
static int hash_file(void){assert(file.open);return 0;}
static void digest_bytes(const char *h,uint8_t *p){(void)h;memset(p,0xaa,32);}
static int destination_digest(uint8_t *p){memset(p,0xbb,32);return 0;}
int upload_state_save(const char *id,uint32_t n,const struct upload_file_record *r){
 (void)id;assert(n==segment&&r->size==file_length);saves++;
 if(mode==METADATA&&attempt==2)return -EIO;
 if(r->state==UPLOAD_STORED){assert(!file.open&&r->confirmed==r->size);stored++;}
 if(r->state==UPLOAD_PARTIAL){assert(!file.open&&r->confirmed==4096);partial++;}
 return 0;
}
static bool wifi_is_sta_connected(void){return radio;}
static int wifi_station_connect(const char *s,const char *p,int *c){(void)s;(void)p;assert(!*c&&!radio);radio=true;wifi_starts++;return 0;}
static int sync_upload_time(void){assert(radio);return check_cancel();}
static int tls_connect(struct https_stream **s){assert(radio&&!transport.live);transport.live=1;connections++;*s=&transport;return 0;}
static void https_stream_close(struct https_stream *s){if(s){assert(s->live);s->live=0;disconnects++;}}
static int upload(struct https_stream *s){
 assert(s&&s->live&&file.open&&active);uploads++;
 if(mode==DROPPED&&attempt==2&&uploads==2){confirmed=4096;return -ECONNRESET;}
 if(mode==PROTOCOL&&attempt==2)return -EBADMSG;
 if(mode==CANCEL_PARTIAL){cancelled=1;confirmed=4096;return -ECANCELED;}
 if(mode==DROPPED&&attempt==2){assert(connections==2);resume_from=4096;}
 confirmed=bytes_sent=file_length;response_keep_alive=mode!=LEGACY;
 if(mode==CANCEL_BETWEEN)cancelled=1;
 return 0;
}
static int wifi_off(void){assert(!transport.live&&!file.open);radio=false;return mode==RADIO_ERROR?-EIO:0;}
static void schedule_after_job(int r){last_scheduled=r;}
static int clip_post_event_sync(int event,struct clip_event_result_info *out){
 (void)out;assert(event==CLIP_EVENT_UPLOAD_END&&!radio&&!transport.live&&!file.open&&!buffers);active=0;ended=1;return 0;
}
'''
        code += worker
        code += r'''
int main(int argc,char **argv){
 assert(argc==2);mode=atoi(argv[1]);
 if(!setjmp(done))worker(NULL,NULL,NULL);
 assert(!active&&ended&&!file.open&&!transport.live&&!radio&&!buffers);
 if(mode==REUSE||mode==LEGACY||mode==DROPPED||mode==ROLLING){
  assert(stored==(mode==ROLLING?5:3)&&wifi_starts==1&&stage==SUCCEEDED&&!last_scheduled);
  assert(connections==((mode==REUSE||mode==ROLLING)?1:mode==LEGACY?3:2));
  assert(uploads==(mode==DROPPED?4:mode==ROLLING?5:3));
 }else if(mode==CANCEL_BETWEEN||mode==CANCEL_PARTIAL){
  assert(attempt==1&&uploads==1&&stage==CANCELLED&&last_scheduled==-ECANCELED);
  assert(mode==CANCEL_BETWEEN?stored==1:partial==1);
 }else if(mode==PROTOCOL){
  assert(attempt==2&&uploads==2&&stored==1&&connections==1&&stage==FAILED&&last_scheduled==-EBADMSG);
 }else if(mode==METADATA){
  assert(attempt==2&&uploads==1&&stored==1&&cleanup_error==-EIO&&stage==FAILED);
 }else if(mode==CLOSE_ERROR){
  assert(attempt==1&&!stored&&cleanup_error==-EIO&&stage==FAILED);
 }else if(mode==RADIO_ERROR){
  assert(stored==3&&cleanup_error==-EIO&&stage==FAILED&&last_scheduled==-EIO);
 }else if(mode==ALLOC_ERROR){assert(!uploads&&!wifi_starts&&last_scheduled==-ENOMEM);}
 else if(mode==SCAN_ERROR){assert(stored==3&&cleanup_error==-EIO&&stage==FAILED&&last_scheduled==-EIO);}
 else if(mode==SCAN_CANCEL){assert(stored==3&&!cleanup_error&&stage==CANCELLED&&last_scheduled==-ECANCELED);}
 assert(connections==disconnects);return 0;
}
'''
        return code

    def test_batch_reuse_legacy_close_reconnect_cancel_and_cleanup_failures(self):
        code = self.worker_fixture()
        for mode in range(13):
            with self.subTest(mode=mode):
                compile_run(code, [str(mode)])

    def test_dtls_reuses_connection_and_releases_cpu_and_handles_before_recording(self):
        code = self.worker_fixture()
        point = code.index('static int upload_file(')
        adapter = r"""
#define dtls_upload https_stream
static struct https_stream *datagram;
static int datagram_received;
static const char host[]="receiver",port[]="9527";
static const unsigned char https_ca[]="CA";
int upload_state_load(const char*id,uint32_t n,struct upload_file_record*r){(void)id;(void)n;(void)r;return -ENOENT;}
static void digest_text(const uint8_t*d,char*out){for(int i=0;i<32;i++)snprintf(out+2*i,3,"%02x",d[i]);}
static int dtls_upload_open(struct https_stream**s,const char*h,const char*p,const unsigned char*ca,size_t n,int(*check)(void)){
 (void)h;(void)p;(void)ca;(void)n;assert(!check());return tls_connect(s);
}
static void dtls_upload_close(struct https_stream*s){https_stream_close(s);}
static int send_datagram_file(const struct upload_file_record*stamp){assert(stamp->size==file_length);return upload(datagram);}
static void clip_cpu_boost_acquire(void){assert(!boosts);boosts++;}
static void clip_cpu_boost_release(void){assert(boosts==1);boosts--;}
"""
        code = '#define CONFIG_CLIP_DTLS_UPLOAD 1\n#define TLS_SETUP_HANDSHAKE 2\nstatic int boosts;\n' + code[:point] + adapter + code[point:]
        code = code.replace('active=0;ended=1;return 0;', 'assert(!boosts);active=0;ended=1;return 0;')
        # Legacy HTTP Connection:close has no DTLS equivalent. All other
        # failure, cancellation, rolling-window and cleanup scenarios apply.
        for mode in [0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]:
            with self.subTest(mode=mode):
                compile_run(code, [str(mode)])
        # Exercise the actual upload decision: recording cache hit avoids data
        # reads; missing/corrupt/I/O-failed cache falls back, save failure is optional.
        for error in (0, '-ENOENT', '-EBADMSG', '-EIO'):
            adapter = r'''
static int record_reads,record_saves;
int recording_hash_load(const char*s,uint32_t n,uint32_t z,uint8_t d[32]){
 assert(!strcmp(s,session)&&n==segment&&z==file_length);record_reads++;memset(d,0xaa,32);return CACHE_ERROR;
}
int recording_hash_save(const char*s,uint32_t n,uint32_t z,const uint8_t d[32]){
 assert(!strcmp(s,session)&&n==segment&&z==file_length&&d[0]==0xaa);record_saves++;return -EIO;
}
'''
            cached = code.replace('static int upload_file(', adapter+'\nstatic int upload_file(', 1)
            cached = cached.replace('static int hash_file(void){assert(file.open);return 0;}',
                                    'static int hash_file(void){assert(file.open);hash_calls++;return 0;}')
            cached = cached.replace('assert(connections==disconnects);return 0;',
                                    'assert(connections==disconnects);assert(record_reads==3);'
                                    'assert(hash_calls==(CACHE_ERROR?3:0));assert(record_saves==(CACHE_ERROR?3:0));return 0;')
            cached = '#define CONFIG_CLIP_RECORDING_HASH 1\n#define CACHE_ERROR '+str(error)+'\nstatic int hash_calls;\n'+cached
            with self.subTest(cache_error=error):
                compile_run(cached, ['0'])
