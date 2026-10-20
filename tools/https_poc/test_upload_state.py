"""Run the real two-slot C journal against POSIX files and injected I/O faults."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class UploadStateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        code = r'''
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
#include <sys/types.h>
#include "upload_state.h"
#define BUILD_ASSERT(c,m) _Static_assert(c,m)
#define STORAGE_PATH_MAX 128
#define FS_O_READ 1
#define FS_O_WRITE 2
#define FS_O_CREATE 4
#define FS_O_TRUNC 8
struct fs_file_t {int fd;};
static bool locked;
static int open_files, short_write, bad_sync, bad_close, bad_open;
static int device_state;
static uint8_t current_destination = 0x22;
#define CLIP_STATE_IDLE 0
#define FS_DIR_ENTRY_FILE 1
struct fs_dirent {int type; size_t size;};
static int clip_event_get_state(void) {return device_state;}
static int destination_digest(uint8_t digest[32]) {memset(digest,current_destination,32);return 0;}
static int storage_build_chunk_path(const char *s,uint32_t n,char *p,size_t size) {
 (void)s;(void)n;snprintf(p,size,"/sealed.opus");return 0;
}
static int fs_stat(const char *p,struct fs_dirent *entry) {
 (void)p;entry->type=FS_DIR_ENTRY_FILE;entry->size=60975;return 0;
}
static const char *directory;
static uint32_t crc32_ieee(const uint8_t *p,size_t n) {
 uint32_t crc=~0u;
 while(n--) {crc^=*p++;for(int i=0;i<8;i++)crc=(crc>>1)^((crc&1)?0xedb88320u:0);}
 return ~crc;
}
static int storage_ensure_mounted(void) { assert(!locked);return 0; }
static void storage_session_json_lock(void) { assert(!locked);locked=true; }
static void storage_session_json_unlock(void) { assert(locked);locked=false; }
static int storage_build_session_metadata_path(const char *session,char *path,size_t size) {
 if(strcmp(session,"20261019070446"))return -EINVAL;
 int n=snprintf(path,size,"%s/session.json",directory);return n<0||(size_t)n>=size?-ENAMETOOLONG:0;
}
static void fs_file_t_init(struct fs_file_t *f) {f->fd=-1;}
static int fs_open(struct fs_file_t *f,const char *path,int flags) {
 assert(locked);if(bad_open)return -EACCES;
 int mode=(flags&FS_O_WRITE)?O_WRONLY:O_RDONLY;
 if(flags&FS_O_CREATE)mode|=O_CREAT;if(flags&FS_O_TRUNC)mode|=O_TRUNC;
 f->fd=open(path,mode,0600);if(f->fd<0)return -errno;open_files++;return 0;
}
static ssize_t fs_read(struct fs_file_t *f,void *p,size_t n) {
 assert(locked);ssize_t r=read(f->fd,p,n);return r<0?-errno:r;
}
static ssize_t fs_write(struct fs_file_t *f,const void *p,size_t n) {
 assert(locked);ssize_t r=write(f->fd,p,short_write?n/2:n);return r<0?-errno:r;
}
static int fs_sync(struct fs_file_t *f) {assert(locked);return bad_sync?-EIO:fsync(f->fd);}
static int fs_close(struct fs_file_t *f) {
 assert(locked && f->fd>=0);int r=close(f->fd);f->fd=-1;open_files--;return bad_close?-EIO:r;
}
'''
        source = (ROOT / "applications/clip/src/upload_state.c").read_text()
        code += "\n".join(line for line in source.splitlines() if not line.startswith("#include"))
        poc = (ROOT / "applications/clip/src/https_poc.c").read_text()
        for signature, following in (("static bool valid_session", "static int hash_file_range"),
                                     ("static void digest_text", "static void digest_bytes"),
                                     ("static int state_query", "static void worker")):
            code += signature + poc.split(signature, 1)[1].split(following, 1)[0]
        code += r'''
static void raw_record(const char *suffix,struct upload_file_record *r) {
 char path[128];snprintf(path,sizeof(path),"%s/http-1.%s",directory,suffix);
 r->crc=crc32_ieee((const uint8_t*)r,offsetof(struct upload_file_record,crc));
 int fd=open(path,O_WRONLY|O_CREAT|O_TRUNC,0600);assert(fd>=0);
 assert(write(fd,r,sizeof(*r))==sizeof(*r));assert(close(fd)==0);
}
int main(int argc,char **argv) {
 assert(argc==3);directory=argv[1];const char *test=argv[2];
 assert(crc32_ieee((const uint8_t*)"123456789",9)==0xcbf43926u);
 struct upload_file_record r={.size=60975},loaded;
 memset(r.content_sha256,0x11,32);memset(r.destination_sha256,0x22,32);
 assert(upload_state_load("20261019070446",1,&loaded)==-ENOENT);
 assert(upload_state_save("20261019070446",1,&r)==0);
 assert(upload_state_load("20261019070446",1,&loaded)==0 && loaded.state==UPLOAD_PENDING && loaded.generation==1);
 r.state=UPLOAD_STORED;r.confirmed=r.size;
 if(!strcmp(test,"short-write")) {
  short_write=1;assert(upload_state_save("20261019070446",1,&r)==-EIO);short_write=0;
  assert(upload_state_load("20261019070446",1,&loaded)==0 && loaded.state==UPLOAD_PENDING);
 } else if(!strcmp(test,"io-errors")) {
  bad_open=1;assert(upload_state_save("20261019070446",1,&r)==-EACCES);bad_open=0;
  bad_sync=1;assert(upload_state_save("20261019070446",1,&r)==-EIO);bad_sync=0;
  bad_close=1;assert(upload_state_load("20261019070446",1,&loaded)==-EIO);bad_close=0;
 } else if(!strcmp(test,"invalid-status")) {
  r.confirmed=1;assert(upload_state_save("20261019070446",1,&r)==-EINVAL);
  assert(upload_state_load("20261019070446",1,&loaded)==0 && loaded.state==UPLOAD_PENDING);
  r.confirmed=r.size;r.last_error=-EIO;
  assert(upload_state_save("20261019070446",1,&r)==-EINVAL);
 } else if(!strcmp(test,"generation-wrap")) {
  r.magic=STATE_MAGIC;r.version=STATE_VERSION;r.generation=UINT32_MAX;raw_record("a",&r);
  r.state=UPLOAD_PENDING;r.confirmed=0;r.generation=0;raw_record("b",&r);
  assert(upload_state_load("20261019070446",1,&loaded)==0 && loaded.generation==0 && loaded.state==UPLOAD_PENDING);
  assert(upload_state_save("20261019070446",1,&r)==0);
  assert(upload_state_load("20261019070446",1,&loaded)==0 && loaded.generation==1);
 } else if(!strcmp(test,"query")) {
  assert(upload_state_save("20261019070446",1,&r)==0);
  char response[512];
  assert(state_query("20261019070446:1",response,sizeof(response))>0);
  assert(strstr(response,"\"state\":\"uploaded\"") && strstr(response,"\"destination_match\":true"));
  current_destination=0x33;
  assert(state_query("20261019070446:1",response,sizeof(response))>0);
  assert(strstr(response,"\"state\":\"pending\"") && strstr(response,"\"destination_match\":false") && strstr(response,"\"confirmed\":0"));
  current_destination=0x22;r.size++;r.confirmed=r.size;
  assert(upload_state_save("20261019070446",1,&r)==0);
  assert(state_query("20261019070446:1",response,sizeof(response))>0);
  assert(strstr(response,"\"state\":\"pending\"") && strstr(response,"\"size_match\":false"));
  device_state=1;assert(state_query("20261019070446:1",response,sizeof(response))==-EBUSY);
  device_state=0;assert(state_query("bad:1",response,sizeof(response))==-EINVAL);
  assert(state_query("20261019070446:0",response,sizeof(response))==-EINVAL);
 } else if(!strcmp(test,"torn-latest")) {
  r.state=UPLOAD_PARTIAL;r.confirmed=16384;r.last_error=-ECANCELED;
  assert(upload_state_save("20261019070446",1,&r)==0);
  r.state=UPLOAD_STORED;r.confirmed=r.size;r.last_error=0;
  assert(upload_state_save("20261019070446",1,&r)==0);
  char path[128];snprintf(path,sizeof(path),"%s/http-1.a",directory);
  assert(truncate(path,47)==0);
  assert(upload_state_load("20261019070446",1,&loaded)==0 && loaded.state==UPLOAD_PARTIAL && loaded.confirmed==16384);
  assert(upload_state_save("20261019070446",1,&r)==0);
  assert(upload_state_load("20261019070446",1,&loaded)==0 && loaded.state==UPLOAD_STORED);
 } else if(!strcmp(test,"corrupt-both")) {
  assert(upload_state_save("20261019070446",1,&r)==0);
  char path[128];for(char slot='a';slot<='b';slot++) {
   snprintf(path,sizeof(path),"%s/http-1.%c",directory,slot);int fd=open(path,O_WRONLY);assert(fd>=0);
   assert(write(fd,"BAD!",4)==4);assert(close(fd)==0);
  }
  assert(upload_state_load("20261019070446",1,&loaded)==-EBADMSG);
  r.state=UPLOAD_PENDING;r.confirmed=0;assert(upload_state_save("20261019070446",1,&r)==0);
  assert(upload_state_load("20261019070446",1,&loaded)==0 && loaded.state==UPLOAD_PENDING);
 } else {
  assert(upload_state_save("20261019070446",1,&r)==0);
  assert(upload_state_load("20261019070446",1,&loaded)==0 && loaded.state==UPLOAD_STORED);
  assert(!memcmp(loaded.content_sha256,r.content_sha256,32));
  assert(!memcmp(loaded.destination_sha256,r.destination_sha256,32));
  assert(upload_state_load("../bad",1,&loaded)==-EINVAL);
 }
 assert(!open_files && !locked);
 return 0;
}
'''
        (root / "test.c").write_text(code)
        cls.executable = root / "test"
        try:
            subprocess.run(["clang", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
                            "-fsanitize=address,undefined",
                            "-I", str(ROOT / "applications/clip/include"), str(root / "test.c"),
                            "-o", str(cls.executable)], check=True)
        except BaseException:
            cls.temp.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_case(self, case):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "session.json"
            marker.write_bytes(b'{"synced":7}\n')
            subprocess.run([str(self.executable), directory, case], check=True)
            self.assertEqual(marker.read_bytes(), b'{"synced":7}\n')

    def test_roundtrip_preserves_phone_sync_and_binds_identities(self):
        self.run_case("roundtrip")

    def test_torn_latest_falls_back_without_false_uploaded_state(self):
        self.run_case("torn-latest")

    def test_short_write_preserves_previous_valid_record(self):
        self.run_case("short-write")

    def test_corrupt_slots_are_not_uploaded_and_can_be_replaced(self):
        self.run_case("corrupt-both")

    def test_io_errors_close_files_and_unlock_metadata(self):
        self.run_case("io-errors")

    def test_generation_wrap_selects_newest_record(self):
        self.run_case("generation-wrap")

    def test_inconsistent_uploaded_state_is_rejected(self):
        self.run_case("invalid-status")

    def test_query_binds_destination_and_rejects_recording_or_bad_selector(self):
        self.run_case("query")
