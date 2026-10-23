"""Exercise the dedicated storage adapter's actual C paths and mount failures."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]

class UploadBenchTest(unittest.TestCase):
    def test_storage_paths_and_mount_failures(self):
        source = (ROOT/'applications/upload_bench/src/storage.c').read_text()
        source = re.sub(r'^#include.*$', '', source, flags=re.M)
        prefix = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
#define STORAGE_PATH_MAX 128
#define CONFIG_CLIP_STORAGE_FILES_PER_GROUP 100
#define K_MUTEX_DEFINE(x) int x
#define K_FOREVER -1
#define FS_FATFS 1
#define DT_NODELABEL(x) 0
#define DEVICE_DT_GET(x) (&rail_device)
struct device { int unused; };
static const struct device rail_device;
typedef int FATFS;
struct fs_mount_t {int type;void *fs_data;const char *mnt_point;};
static int ready=1, rail_error, disk_error, mount_error, mounts;
static void k_mutex_lock(int *m,int t){(void)m;(void)t;}
static void k_mutex_unlock(int *m){(void)m;}
static void k_msleep(int ms){(void)ms;}
static bool device_is_ready(const struct device *d){(void)d;return ready;}
static int regulator_enable(const struct device *d){(void)d;return rail_error;}
static int disk_access_init(const char *name){assert(!strcmp(name,"SD"));return disk_error;}
static int fs_mount(struct fs_mount_t *m){assert(!strcmp(m->mnt_point,"/SD:"));mounts++;return mount_error;}
'''
        main = r'''
int main(void){
 char path[128];
 assert(!storage_build_chunk_path("20261020080208",1,path,sizeof(path)));
 assert(!strcmp(path,"/SD:/REC/20261020/08/02/08/0/0001.opus"));
 assert(!storage_build_chunk_path("20261020080208",100,path,sizeof(path)));
 assert(strstr(path,"/0/0100.opus"));
 assert(!storage_build_chunk_path("20261020080208",101,path,sizeof(path)));
 assert(strstr(path,"/1/0101.opus"));
 assert(!storage_build_session_metadata_path("20261020080208",path,sizeof(path)));
 assert(!strcmp(path,"/SD:/REC/20261020/08/02/08/session.json"));
 assert(storage_build_chunk_path(NULL,1,path,sizeof(path))==-EINVAL);
 assert(storage_build_chunk_path("20261020080208",0,path,sizeof(path))==-EINVAL);
 assert(storage_build_chunk_path("../../etc/pass",1,path,sizeof(path))==-EINVAL);
 assert(storage_build_chunk_path("20261020080208",1,NULL,100)==-EINVAL);
 assert(storage_build_chunk_path("20261020080208",1,path,0)==-EINVAL);
 assert(storage_build_chunk_path("20261020080208",1,path,8)==-ENAMETOOLONG && path[0]==0);
 ready=0;assert(storage_init()==-ENODEV && mounts==0);
 ready=1;rail_error=-EIO;assert(storage_init()==-EIO && mounts==0);
 rail_error=0;disk_error=-EIO;assert(storage_init()==-EIO && mounts==0);
 disk_error=0;mount_error=-EIO;assert(storage_init()==-EIO && mounts==1);
 mount_error=0;assert(!storage_init() && mounts==2);
 assert(!storage_ensure_mounted() && mounts==2);
 storage_session_json_lock();storage_session_json_unlock();
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory);(p/'test.c').write_text(prefix+source+main)
            subprocess.run(['clang','-std=c11','-Wall','-Wextra','-Werror',
                            '-fsanitize=address,undefined',str(p/'test.c'),'-o',str(p/'test')],check=True)
            subprocess.run([str(p/'test')],check=True)

    def test_large_windows_are_bounded_and_require_bench(self):
        code=r'''
#include "upload_tune.h"
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
int main(void){
 const unsigned windows[]={32768,49152,65535};
 struct upload_tuning value={65536,2048,16384,1};char text[80];
 for(unsigned i=0;i<3;i++){
  snprintf(text,sizeof(text),"65536,2048,%u,1",windows[i]);
#ifdef CONFIG_CLIP_UPLOAD_BENCH
  assert(!upload_tune_parse(text,&value) && value.send_window==windows[i]);
#else
  assert(upload_tune_parse(text,&value)==-EINVAL);
#endif
 }
 #ifdef CONFIG_CLIP_UPLOAD_BENCH_WHOLE
 assert(!upload_tune_parse("8388608,2048,16384,1",&value) && value.chunk_size==8388608);
 assert(upload_tune_server_limit(8388608,8388608,true)==8388608);
 assert(upload_tune_server_limit(8388608,131072,true)==-EMSGSIZE);
 #else
 assert(upload_tune_parse("8388608,2048,16384,1",&value)==-EINVAL);
 assert(upload_tune_server_limit(65536,8388608,true)==-EBADMSG);
 #endif
 struct upload_tuning before=value;
 assert(upload_tune_parse("8388609,2048,16384,1",&value)==-EINVAL);
 assert(upload_tune_parse("99999999,2048,16384,1",&value)==-EINVAL);
 assert(upload_tune_parse("65536,2048,65536,1",&value)==-EINVAL);
 assert(upload_tune_parse("65536,2048,98304,1",&value)==-EINVAL);
 assert(!memcmp(&before,&value,sizeof(value)));
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory);(p/'test.c').write_text(code)
            for flags in ([], ['-DCONFIG_CLIP_UPLOAD_BENCH=1'],
                          ['-DCONFIG_CLIP_UPLOAD_BENCH=1', '-DCONFIG_CLIP_UPLOAD_BENCH_WHOLE=1']):
                subprocess.run(['clang','-std=c11','-Wall','-Wextra','-Werror',
                                '-fsanitize=address,undefined',*flags,
                                '-I',str(ROOT/'applications/clip/include'),str(p/'test.c'),
                                str(ROOT/'applications/clip/src/upload_tune.c'),'-o',str(p/'test')],check=True)
                subprocess.run([str(p/'test')],check=True)
