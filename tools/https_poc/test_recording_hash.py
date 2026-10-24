"""Real recording write/close/hash/cache code with POSIX I/O and fault injection."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SHA = r'''
#include <CommonCrypto/CommonDigest.h>
typedef CC_SHA256_CTX mbedtls_sha256_context;
static int hash_failure;
static void mbedtls_sha256_init(mbedtls_sha256_context*c){memset(c,0,sizeof(*c));}
static int mbedtls_sha256_starts(mbedtls_sha256_context*c,int is224){assert(!is224);return CC_SHA256_Init(c)?0:-EIO;}
static int mbedtls_sha256_update(mbedtls_sha256_context*c,const uint8_t*p,size_t n){return hash_failure?-EIO:CC_SHA256_Update(c,p,(CC_LONG)n)?0:-EIO;}
static int mbedtls_sha256_finish(mbedtls_sha256_context*c,uint8_t out[32]){return CC_SHA256_Final(out,c)?0:-EIO;}
static void mbedtls_sha256_free(mbedtls_sha256_context*c){memset(c,0,sizeof(*c));}
'''
FIXTURE = r'''
#define CONFIG_CLIP_RECORDING_HASH 1
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/stat.h>
#include "recording_hash.h"
#define BUILD_ASSERT(c,m) _Static_assert(c,m)
#define STORAGE_PATH_MAX 256
#define CONFIG_CLIP_STORAGE_CHUNK_SIZE 128
#define FS_O_READ 1
#define FS_O_WRITE 2
#define FS_O_CREATE 4
#define FS_O_TRUNC 8
#define FS_DIR_ENTRY_FILE 1
#define LOG_ERR(...) ((void)0)
#define LOG_WRN(...) ((void)0)
#define LOG_DBG(...) ((void)0)
#define LOG_INF(...) ((void)0)
#define K_MSEC(x) (x)
struct fs_file_t {int fd;bool metadata;};
struct fs_dirent {int type;size_t size;};
struct storage_file {char filename[32],session_id[15];uint32_t chunk_index,bytes_written,frames_written;bool is_open;struct fs_file_t internal_file;};
static const char *directory;
static const char *SID="20261024180000";
static bool locked,sd_mounted=true,sd_full;
static int handles,short_write,bad_sync,bad_close,bad_sidecar_sync;
static uint8_t write_buffer[CONFIG_CLIP_STORAGE_CHUNK_SIZE];
static uint32_t buffer_pos;
static uint64_t total_bytes;
static struct fs_file_t *current_file_ptr;
static int file_closed_sem;
static char writing_session[15],writing_filename[32];
static void storage_set_writing_file(const char*s,const char*f){snprintf(writing_session,sizeof(writing_session),"%s",s?s:"");snprintf(writing_filename,sizeof(writing_filename),"%s",f?f:"");}
static bool storage_file_is_writing(const char*s,const char*f){return !strcmp(s,writing_session)&&!strcmp(f,writing_filename);}
static int validate_session_id(const char*s){return !s||strlen(s)!=14?-EINVAL:0;}
static int ensure_time_bucket_chunk_directory(const char*s,uint32_t n){(void)s;(void)n;return 0;}
static int storage_build_chunk_path(const char*s,uint32_t n,char*p,size_t size){int k=snprintf(p,size,"%s/%s-%u.opus",directory,s,n);return k<0||(size_t)k>=size?-ENAMETOOLONG:0;}
static int storage_build_session_metadata_path(const char*s,char*p,size_t size){(void)s;int k=snprintf(p,size,"%s/session.json",directory);return k<0||(size_t)k>=size?-ENAMETOOLONG:0;}
static void storage_session_json_lock(void){assert(!locked);locked=true;}
static void storage_session_json_unlock(void){assert(locked);locked=false;}
static void k_sleep(int n){(void)n;}
static void k_sem_give(int*p){(*p)++;}
static uint32_t crc32_ieee(const uint8_t*p,size_t n){uint32_t c=~0u;while(n--){c^=*p++;for(int i=0;i<8;i++)c=(c>>1)^((c&1)?0xedb88320u:0);}return ~c;}
static void fs_file_t_init(struct fs_file_t*f){f->fd=-1;f->metadata=false;}
static int fs_open(struct fs_file_t*f,const char*p,int flags){
 f->metadata=strstr(p,"/sha-")!=NULL;assert(!f->metadata||locked);
 int m=(flags&FS_O_WRITE)?O_WRONLY:O_RDONLY;if(flags&FS_O_CREATE)m|=O_CREAT;if(flags&FS_O_TRUNC)m|=O_TRUNC;
 f->fd=open(p,m,0600);if(f->fd<0)return -errno;handles++;return 0;
}
static ssize_t fs_read(struct fs_file_t*f,void*p,size_t n){ssize_t k=read(f->fd,p,n);return k<0?-errno:k;}
static ssize_t fs_write(struct fs_file_t*f,const void*p,size_t n){
 size_t count=!f->metadata&&short_write?n/2:n;ssize_t k=write(f->fd,p,count);return k<0?-errno:k;
}
static int fs_sync(struct fs_file_t*f){if((!f->metadata&&bad_sync)||(f->metadata&&bad_sidecar_sync))return -EIO;return fsync(f->fd);}
static int fs_close(struct fs_file_t*f){assert(f->fd>=0);int k=close(f->fd);f->fd=-1;handles--;return !f->metadata&&bad_close?-EIO:k;}
static int fs_stat(const char*p,struct fs_dirent*e){struct stat s;if(stat(p,&s))return -errno;e->type=S_ISREG(s.st_mode)?1:2;e->size=s.st_size;return 0;}
'''
MAIN = r'''
static void print_digest(uint32_t size){
 uint8_t digest[32];int ret=recording_hash_load(SID,1,size,digest);if(ret){fprintf(stderr,"load failed: %d\n",ret);exit(4);}char hex[65];
 for(unsigned i=0;i<32;i++)snprintf(hex+i*2,3,"%02x",digest[i]);printf("{\"sha256\":\"%s\",\"size\":%u}\n",hex,size);
}
int main(int argc,char**argv){
 assert(argc==3);directory=argv[1];const char*mode=argv[2];
 uint8_t data[521];for(unsigned i=0;i<sizeof(data);i++)data[i]=(uint8_t)(i*17+3);
 if(!strcmp(mode,"load")){print_digest(803);return 0;}
 struct storage_file f={0};assert(!storage_create_file(&f,SID,1));
 if(!strcmp(mode,"short"))short_write=1;
 if(!strcmp(mode,"hash-error"))hash_failure=1;
 int written=storage_write_frame(&f,data,521);
 if(!strcmp(mode,"short")){assert(written==-EIO);short_write=0;assert(!storage_close_file(&f));}
 else {
  assert(!written);assert(!storage_write_frame(&f,data,1));assert(!storage_write_frame(&f,data,275));
  assert(f.bytes_written==803);uint8_t d[32];assert(recording_hash_load(SID,1,803,d)==-EBUSY);
  if(!strcmp(mode,"crash"))return 0; /* No close, therefore no sidecar. */
  if(!strcmp(mode,"sync-error"))bad_sync=1;
  if(!strcmp(mode,"close-error"))bad_close=1;
  if(!strcmp(mode,"cache-sync-error"))bad_sidecar_sync=1;
  int closed=storage_close_file(&f);
  assert(closed==((bad_sync||bad_close)?-EIO:0));
 }
 assert(!handles&&!f.is_open&&!current_file_ptr&&!buffer_pos&&!locked);
 uint8_t d[32];int loaded=recording_hash_load(SID,1,f.bytes_written?f.bytes_written:1,d);
 if(!strcmp(mode,"short")||!strcmp(mode,"hash-error")||bad_sync||bad_close){assert(loaded==-ENOENT);return 0;}
 assert(!loaded); /* A reported cache sync failure may still leave valid bytes. */
 assert(recording_hash_load(SID,1,804,d)==-EBADMSG);
 assert(recording_hash_load("20261024180001",1,803,d)==-EBADMSG);
 assert(recording_hash_load("../bad",1,803,d)==-EINVAL);
 struct storage_file duplicate={0};assert(storage_create_file(&duplicate,SID,1)==-EEXIST);
 print_digest(803);
 /* Segment rotation and pause/resume use a new writer, never old bytes. */
 struct storage_file second={0};assert(!storage_create_file(&second,SID,2));
 assert(!storage_write_frame(&second,data,19));assert(!storage_close_file(&second));
 assert(!recording_hash_load(SID,2,21,d));assert(!handles);
 return 0;
}
'''


@unittest.skipUnless(sys.platform == 'darwin', 'Host SHA adapter uses macOS CommonCrypto')
class RecordingHashTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        module = (ROOT/'applications/clip/src/recording_hash.c').read_text()
        module = '\n'.join(line for line in module.splitlines() if not line.startswith('#include'))
        storage = (ROOT/'applications/clip/src/storage.c').read_text()
        functions = storage[storage.index('static int flush_write_buffer(void)\n{'):storage.index('\nint storage_read_chunk(')]
        (root/'test.c').write_text(FIXTURE+SHA+module+functions+MAIN)
        cls.binary=root/'test'
        subprocess.run(['clang','-std=gnu11','-Wall','-Wextra','-Werror','-Wno-deprecated-declarations',
                        '-Wno-sign-compare','-fsanitize=address,undefined',
                        '-I'+str(ROOT/'applications/clip/include'),str(root/'test.c'),'-o',str(cls.binary)],check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_actual_framed_bytes_rotation_persistence_and_faults(self):
        for mode in ('ok','short','hash-error','sync-error','close-error','cache-sync-error','crash'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                p=Path(tmp)
                result=subprocess.run([str(self.binary),tmp,mode],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)
                if result.stdout:
                    metadata=json.loads(result.stdout)
                    actual=(p/'20261024180000-1.opus').read_bytes()
                    self.assertEqual(len(actual),803)
                    self.assertEqual(metadata['sha256'],hashlib.sha256(actual).hexdigest())
                    second=(p/'20261024180000-2.opus').read_bytes()
                    self.assertEqual((p/'sha-2').read_bytes()[32:64],hashlib.sha256(second).digest())
                    fresh=json.loads(subprocess.check_output([str(self.binary),tmp,'load'],text=True))
                    self.assertEqual(fresh,metadata)
                elif mode=='crash':
                    self.assertFalse((p/'sha-1').exists())

    def test_corrupt_truncated_and_oversized_cache_rejected(self):
        for modification in ('flip','truncate','append'):
            with self.subTest(modification=modification), tempfile.TemporaryDirectory() as tmp:
                subprocess.run([str(self.binary),tmp,'ok'],check=True,capture_output=True)
                path=Path(tmp)/'sha-1'
                data=bytearray(path.read_bytes())
                if modification=='flip':data[35]^=0x40
                elif modification=='truncate':data=data[:35]
                else:data+=b'extra'
                path.write_bytes(data)
                result=subprocess.run([str(self.binary),tmp,'load'],capture_output=True)
                self.assertNotEqual(result.returncode,0)


if __name__=='__main__':unittest.main()
