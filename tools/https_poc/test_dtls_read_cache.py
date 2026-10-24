"""Actual C read-ahead and lifetime code, exercised with adversarial file reads."""
from pathlib import Path
import subprocess,tempfile,unittest
ROOT=Path(__file__).resolve().parents[2]
class ReadCacheTest(unittest.TestCase):
    def test_actual_cache_and_lifetime(self):
        source=(ROOT/'applications/clip/src/https_poc.c').read_text()
        read='static int datagram_read'+source.split('static int datagram_read',1)[1].split('static void datagram_progress',1)[0]
        send='static int send_datagram_file'+source.split('static int send_datagram_file',1)[1].split('\n#endif\n\n/* Upload one file;',1)[0]
        parser='static int configure_datagram_cache'+source.split('static int configure_datagram_cache',1)[1].split('\n#endif',1)[0]
        prefix=r'''
#define CONFIG_CLIP_DTLS_PROFILE 1
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
#include <sys/types.h>
#include "dtls_upload.h"
#include "upload_state.h"
#define MIN(a,b) ((a)<(b)?(a):(b))
#define K_FOREVER 0
#define FS_SEEK_SET 0
static uint8_t io[4096],content[574179];
static size_t file_length=sizeof(content),max_read;
static uint32_t cache_offset,cache_length;
static uint16_t datagram_cache_bytes=4096;
#ifdef CONFIG_CLIP_DTLS_PROFILE
static uint16_t datagram_initial_window=128,datagram_window_ceiling=128;
#endif
static uint8_t *datagram_cache;
static const char *device_id="clip",*session="20261020080208",*token="tttttttttttttttttttttttttttttttt";
static uint32_t segment=1;
static struct {size_t pos;} file;
static int cancel_error,read_error,seek_error,client_error,control_lock;
static bool allocation_failure;
static unsigned reads,allocations,frees,client_calls;
static struct dtls_upload_stats last_datagram;
static struct dtls_upload *datagram=(void*)1;
static int check_cancel(void){return cancel_error;}
#ifdef CONFIG_CLIP_DTLS_PROFILE
uint64_t dtls_profile_now(void){return 0;}
void dtls_profile_add(enum dtls_profile_metric m,uint64_t t){(void)m;(void)t;}
#endif
static int fs_seek(void*f,uint32_t off,int where){assert(f==&file && where==FS_SEEK_SET);if(seek_error)return seek_error;file.pos=off;return 0;}
static ssize_t fs_read(void*f,uint8_t*out,size_t n){assert(f==&file);reads++;if(read_error)return read_error;assert(file.pos+n<=file_length);if(max_read && n>max_read)n=max_read;memcpy(out,content+file.pos,n);file.pos+=n;return n;}
static void *k_malloc(size_t n){if(allocation_failure)return NULL;allocations++;return malloc(n);}
static void k_free(void*p){if(p){frees++;free(p);}}
static void k_mutex_lock(int*m,int wait){assert(!*m);*m=1;(void)wait;}
static void k_mutex_unlock(int*m){assert(*m);*m=0;}
static void datagram_progress(uint32_t s,uint32_t r,uint32_t d,uint32_t o){(void)s;(void)r;(void)d;(void)o;}
int dtls_upload_set_window(struct dtls_upload*s,unsigned a,unsigned b){assert(s==datagram && a==128 && b==128);return 0;}
int dtls_upload_file(struct dtls_upload*s,const struct dtls_upload_identity*i,dtls_upload_read_fn read,dtls_upload_progress_fn progress,struct dtls_upload_stats*stats){
 assert(s==datagram && i->size==file_length && progress==datagram_progress);client_calls++;
 assert((datagram_cache_bytes>4096)==(datagram_cache!=NULL));memset(stats,0,sizeof(*stats));
 if(client_error)return client_error;
 uint8_t out[1024];
 for(uint32_t off=0;off<file_length;off+=sizeof(out)){
  size_t n=MIN(sizeof(out),file_length-off);assert(!read(off,out,n));assert(!memcmp(out,content+off,n));
 }
 return 0;
}
'''
        main=r'''
int main(void){
 for(size_t i=0;i<file_length;i++)content[i]=(uint8_t)(i*17+3);
 struct upload_file_record record={0};char response[128];uint8_t out[1024];
 assert(configure_datagram_cache("cache",response,sizeof(response))>0 && strstr(response,"\"cache_kib\":4"));
 const char*bad[]={NULL,"","cache,0","cache,32","cache,-4","cache,+4","cache,8,16","cache,8,","cache,8 ","cache,8192"};
 for(unsigned j=0;j<sizeof(bad)/sizeof(bad[0]);j++){assert(configure_datagram_cache(bad[j],response,sizeof(response))==-EINVAL);assert(datagram_cache_bytes==4096);}
 for(unsigned kib=4;kib<=16;kib*=2){
  char command[24];snprintf(command,sizeof(command),"cache,%u",kib);assert(configure_datagram_cache(command,response,sizeof(response))>0);
  unsigned before_alloc=allocations,before_free=frees;reads=0;cache_length=0;
  assert(!send_datagram_file(&record));assert(!datagram_cache);
  assert(reads==(file_length+kib*1024-1)/(kib*1024));
  assert(allocations-before_alloc==(kib>4));assert(frees-before_free==(kib>4));
  client_error=-ECANCELED;assert(send_datagram_file(&record)==-ECANCELED && !datagram_cache);client_error=0;
  if(kib>4){allocation_failure=true;unsigned calls=client_calls;assert(send_datagram_file(&record)==-ENOMEM && !datagram_cache && calls==client_calls);allocation_failure=false;}
  datagram_cache=kib>4?k_malloc(kib*1024):NULL;cache_length=0;max_read=333;
  assert(!datagram_read(0,out,sizeof(out)) && !memcmp(out,content,sizeof(out)));
  assert(!datagram_read(32768,out,sizeof(out)) && !memcmp(out,content+32768,sizeof(out)));
  assert(!datagram_read(1024,out,sizeof(out)) && !memcmp(out,content+1024,sizeof(out)));
  assert(!datagram_read(file_length-739,out,739) && !memcmp(out,content+file_length-739,739));
  assert(datagram_read(file_length,out,1)==-EINVAL);
  cache_length=0;read_error=-EIO;assert(datagram_read(0,out,sizeof(out))==-EIO);read_error=0;
  cache_length=0;seek_error=-EIO;assert(datagram_read(0,out,sizeof(out))==-EIO);seek_error=0;
  cancel_error=-ECANCELED;assert(datagram_read(0,out,sizeof(out))==-ECANCELED);cancel_error=0;
  k_free(datagram_cache);datagram_cache=NULL;max_read=0;
 }
 assert(allocations==frees);
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            for profiled in (True,False):
                code=prefix+read+send+parser+main
                if not profiled:code=code.replace('#define CONFIG_CLIP_DTLS_PROFILE 1','')
                (p/'read.c').write_text(code)
                subprocess.run(['clang','-std=c11','-Wall','-Wextra','-Werror','-fsanitize=address,undefined','-I'+str(ROOT/'applications/clip/include'),str(p/'read.c'),'-o',str(p/'read')],check=True)
                subprocess.run([str(p/'read')],check=True)
if __name__=='__main__': unittest.main()
