"""Compile the real bounded log reader with an isolated filesystem fixture."""
from pathlib import Path
import subprocess
import tempfile
import unittest


class LogReadTest(unittest.TestCase):
    def test_read_bounds_idle_guards_and_filesystem_errors(self):
        root = Path(__file__).resolve().parents[2]
        source = (root / 'applications/clip/src/at_commands.c').read_text()
        start = source.index('static int log_read_page(')
        source = source[start:source.index('\n#endif\n\n/* LOG Command Handler', start)]
        fixture = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#define CONFIG_CLIP_HTTPS_POC 1
#define CONFIG_LOG_BACKEND_FS_FILES_LIMIT 20
#define CONFIG_LOG_BACKEND_FS_FILE_SIZE 131072
#define CONFIG_LOG_BACKEND_FS_DIR "/SD:/LOG"
#define CONFIG_LOG_BACKEND_FS_FILE_PREFIX "log."
#define STORAGE_PATH_MAX 128
#define CLIP_STATE_IDLE 0
#define FS_DIR_ENTRY_FILE 1
#define FS_O_READ 1
#define FS_SEEK_SET 0
static bool log_fs_active,active;
static int state,mount_error,io_error,calls,position;
static unsigned int cursor;
struct fs_dir_t {int unused;};
struct fs_dirent {char name[128];int type;size_t size;};
struct fs_file_t {int unused;};
static int clip_event_get_state(void){return state;}
static bool https_poc_active(void){return active;}
static int storage_ensure_mounted(void){calls++;return mount_error;}
static void fs_dir_t_init(struct fs_dir_t *d){(void)d;}
static void fs_file_t_init(struct fs_file_t *f){(void)f;}
static int fs_opendir(struct fs_dir_t *d,const char *path)
{(void)d;calls++;assert(!strcmp(path,"/SD:/LOG"));cursor=0;return io_error==1?-EIO:0;}
static int fs_readdir(struct fs_dir_t *d,struct fs_dirent *e)
{(void)d;calls++;if(io_error==2)return -EIO;
 memset(e,0,sizeof(*e));if(cursor++==0){strcpy(e->name,"log.0000");e->type=1;e->size=130;}return 0;}
static int fs_closedir(struct fs_dir_t *d){(void)d;calls++;return io_error==3?-EIO:0;}
static int fs_open(struct fs_file_t *f,const char *p,int flags)
{(void)f;calls++;assert(flags==FS_O_READ && !strcmp(p,"/SD:/LOG/log.0000"));return io_error==4?-EIO:0;}
static int fs_seek(struct fs_file_t *f,unsigned int offset,int mode)
{(void)f;assert(mode==0);calls++;position=offset;return io_error==5?-EIO:0;}
static int fs_read(struct fs_file_t *f,unsigned char *b,size_t n)
{(void)f;calls++;if(io_error==6)return -EIO;
 int count=130-position;if(count>(int)n)count=n;for(int i=0;i<count;i++)b[i]=(unsigned char)(position+i);return count;}
static int fs_close(struct fs_file_t *f){(void)f;calls++;return io_error==7?-EIO:0;}
static int create_json_response(bool ok,const char *error,const char *data,char *out,size_t size)
{if(ok){assert(!error && data);snprintf(out,size,"%s",data);return 0;}
 assert(error && !data);snprintf(out,size,"error:%s",error);return -1;}
'''
        checks = r'''
int main(void){char out[1024];
 const char *bad[]={NULL,"","-1,0","0,-1","+0,0","0, 0","0,0junk","20,0","0,131073","9999999999999999999999999,0","0,9999999999999999999999999","0","0,0,1"};
 for(unsigned int i=0;i<sizeof(bad)/sizeof(bad[0]);i++){calls=0;assert(log_read_page(bad[i],out,sizeof(out))<0);assert(calls==0);}
 log_fs_active=true;assert(log_read_page("0,0",out,sizeof(out))<0);assert(calls==0);log_fs_active=false;
 state=1;assert(log_read_page("0,0",out,sizeof(out))<0);assert(calls==0);state=0;
 active=true;assert(log_read_page("0,0",out,sizeof(out))<0);assert(calls==0);active=false;
 assert(log_read_page("0,0",out,sizeof(out))==0);assert(strstr(out,"\"next\":128") && strstr(out,"\"eof\":false") && strstr(out,"00010203"));
 assert(log_read_page("0,128",out,sizeof(out))==0);assert(strstr(out,"\"hex\":\"8081\"") && strstr(out,"\"eof\":true"));
 assert(log_read_page("0,130",out,sizeof(out))==0);assert(strstr(out,"\"hex\":\"\""));
 assert(log_read_page("0,131",out,sizeof(out))<0);assert(log_read_page("1,0",out,sizeof(out))<0);
 for(io_error=1;io_error<=7;io_error++)assert(log_read_page("0,0",out,sizeof(out))<0);
 io_error=0;mount_error=-EIO;assert(log_read_page("0,0",out,sizeof(out))<0);
 return 0;}
'''
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            c = path / 'reader.c'
            c.write_text(fixture + source + checks)
            binary = path / 'reader'
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            '-fsanitize=address,undefined', str(c), '-o', str(binary)], check=True, capture_output=True)
            subprocess.run([str(binary)], check=True, capture_output=True)


if __name__ == '__main__':
    unittest.main()
