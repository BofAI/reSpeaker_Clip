"""Exercise real config serialization and scheduling with deterministic adapters."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


def compile_run(code, arguments=()):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / 'test.c').write_text('#include "upload_perf.h"\n' + code)
        subprocess.run(['clang', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                        '-fsanitize=address,undefined', '-I'+str(ROOT/'applications/clip/include'),
                        str(root/'test.c'), '-o', str(root/'test')], check=True)
        subprocess.run([str(root/'test'), *arguments], check=True)


def without_includes(path):
    return '\n'.join(line for line in path.read_text().splitlines() if not line.startswith('#include'))


class AutoUploadTest(unittest.TestCase):
    def test_config_roundtrip_corruption_validation_and_save_failures(self):
        code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <errno.h>
#include "upload_config.h"
typedef int (*settings_read_cb)(void*,void*,size_t);
struct settings_handler {const char *name;int (*h_set)(const char*,size_t,settings_read_cb,void*);};
static struct settings_handler *registered;
static unsigned char disk[1024];
static size_t disk_size;
static int write_error, read_error, short_read, watches;
static uint32_t crc32_ieee(const uint8_t *p,size_t n) {
 uint32_t c=~0u;while(n--){c^=*p++;for(int i=0;i<8;i++)c=(c>>1)^((c&1)?0xedb88320u:0);}return ~c;
}
static void settings_load_watchdog_arm(void){assert(!watches);watches++;}
static void settings_load_watchdog_disarm(void){assert(watches);watches--;}
static int settings_register(struct settings_handler *h){registered=h;return 0;}
static int read_value(void *arg,void *value,size_t length){
 (void)arg;if(read_error)return -EIO;memcpy(value,disk,length);return short_read?(int)length-1:(int)length;
}
static int settings_load_subtree(const char *name){
 assert(!strcmp(name,"https"));return disk_size?registered->h_set("config",disk_size,read_value,NULL):0;
}
static int settings_save_one(const char *key,const void *p,size_t n){
 assert(!strcmp(key,"https/config"));if(write_error)return -EIO;assert(n<sizeof(disk));memcpy(disk,p,n);disk_size=n;return 0;
}
static int settings_delete(const char *key){assert(!strcmp(key,"https/config"));if(write_error)return -EIO;disk_size=0;return 0;}
'''
        code += without_includes(ROOT/'applications/clip/src/upload_config.c')
        code += r'''
int main(void){
 struct upload_config c={0},out={0};
 assert(upload_config_init(&out)==0&&!out.enabled&&!out.ssid[0]);
 strcpy(c.ssid,"test");strcpy(c.password,"test-pass");strcpy(c.host,"192.168.2.78");strcpy(c.port,"8443");
 strcpy(c.device,"clip");memset(c.token,'a',32);strcpy(c.clock_host,"clock.example");strcpy(c.clock_port,"123");c.enabled=1;
 assert(upload_config_save(&c)==0);assert(upload_config_init(&out)==0&&!memcmp(&c,&out,sizeof(c)));
 c.enabled=0;assert(upload_config_save(&c)==0);assert(upload_config_init(&out)==0&&!out.enabled);
 struct upload_config invalid=c;memset(invalid.host,'x',sizeof(invalid.host));assert(upload_config_save(&invalid)==-EINVAL);
 invalid=c;strcpy(invalid.port,"-1");assert(upload_config_save(&invalid)==-EINVAL);
 invalid=c;invalid.enabled=2;assert(upload_config_save(&invalid)==-EINVAL);
 invalid=c;invalid.token[2]='!';assert(upload_config_save(&invalid)==-EINVAL);
 invalid=c;invalid.clock_host[0]=0;assert(upload_config_save(&invalid)==-EINVAL);
 disk[12]^=0x55;assert(upload_config_init(&out)==-EBADMSG&&!out.ssid[0]&&!out.enabled);
 assert(upload_config_save(&c)==0);short_read=1;assert(upload_config_init(&out)==-EBADMSG&&!out.enabled);short_read=0;
 read_error=1;assert(upload_config_init(&out)==-EBADMSG&&!out.enabled);read_error=0;
 disk_size--;assert(upload_config_init(&out)==-EBADMSG&&!out.enabled);
 assert(upload_config_save(&c)==0);write_error=1;assert(upload_config_save(&c)==-EIO&&upload_config_clear()==-EIO);write_error=0;
 assert(upload_config_clear()==0);assert(upload_config_init(&out)==0&&!out.enabled);
 assert(!watches);return 0;
}
'''
        compile_run(code)

    def test_round_robin_backoff_busy_cancel_and_uploaded_filter(self):
        source = (ROOT/'applications/clip/src/https_poc.c').read_text()
        choose = source[source.index('#define AUTO_BATCH_FILES'):source.index('/* Two records per page')]
        schedule = 'static void schedule_after_job' + source.split('static void schedule_after_job', 1)[1].split('/* Upload one file;', 1)[0]
        code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include "upload_state.h"
#include "upload_config.h"
#define MAX_FILE (8U*1024U*1024U)
#define MIN(a,b) ((a)<(b)?(a):(b))
#define K_FOREVER 0
#define K_MSEC(x) (x)
typedef int atomic_val_t;
static struct upload_config settings={.enabled=1};
static int initialized=1,auto_paused,activity,observed_activity,auto_job,control_lock,cleanup_error;
static int scan_error,stage,last_error,scan_failure,metadata_failure,busy,cancel,active;
static uint32_t failures,cursor_segment,segment;
static char cursor_session[15],session[32];
static int64_t next_auto,deadline,now;
static int allocation_failure;
static void *k_calloc(size_t n,size_t size){return allocation_failure?NULL:calloc(n,size);}
static void k_free(void *p){free(p);}
enum {CANCELLED,FAILED,IDLE};
enum {CLIP_EVENT_UPLOAD_BEGIN,CLIP_EVENT_UPLOAD_END,CLIP_EVENT_OK};
struct clip_event_result_info {int result;};
static int atomic_get(int *v){return *v;}
static void atomic_set(int *v,int n){*v=n;}
static void atomic_clear(int *v){*v=0;}
static int64_t k_uptime_get(void){return now;}
static void k_mutex_lock(int *m,int t){(void)t;assert(!*m);*m=1;}
static void k_mutex_unlock(int *m){assert(*m);*m=0;}
static void k_sleep(int t){(void)t;assert(0);}
static int upload_config_valid(const struct upload_config *v);
static int destination_digest(uint8_t *out){memset(out,0x22,32);return 0;}
static int check_cancel(void){return cancel?-ECANCELED:0;}
static void wifi_diag_reset(void){}
static int clip_post_event_sync(int event,struct clip_event_result_info *out){
 out->result=busy?1:CLIP_EVENT_OK;
 if(event==CLIP_EVENT_UPLOAD_END)active=0;else if(!busy)active=1;return 0;
}
struct item {const char *id;uint32_t number,size;bool sealed;int state;};
static struct item items[]={
 {"20261019070446",1,60975,true,UPLOAD_PENDING},
 {"20261019070446",2,60975,true,UPLOAD_STORED},
 {"20261019070446",3,60975,true,UPLOAD_FAILED},
 {"20261019070447",1,60975,false,UPLOAD_PENDING},
 {"20261019070448",1,60975,true,UPLOAD_PARTIAL}
};
static unsigned int visited;
static int upload_state_load(const char *id,uint32_t number,struct upload_file_record *out){
 if(metadata_failure)return metadata_failure;
 for(unsigned i=0;i<sizeof(items)/sizeof(items[0]);i++){
  if(!strcmp(items[i].id,id)&&items[i].number==number){
   *out=(struct upload_file_record){.state=items[i].state,.size=items[i].size};memset(out->destination_sha256,0x22,32);return 0;
  }
 }
 return -ENOENT;
}
typedef int (*visit_fn)(const char*,uint32_t,uint32_t,bool,void*);
static int upload_scan(visit_fn visit,void *context,int (*check)(void)){
 assert(!control_lock); /* Status/off commands are not locked out by the scan. */
 if(scan_failure)return scan_failure;
 for(unsigned i=0;i<sizeof(items)/sizeof(items[0]);i++){
  int ret=check();if(ret)return ret;visited++;
  ret=visit(items[i].id,items[i].number,items[i].size,items[i].sealed,context);if(ret)return ret;
 }
 return 0;
}
int upload_config_valid(const struct upload_config *v){return v->enabled<=1;}
'''
        # Header declares non-static API.
        code = code.replace('static int upload_config_valid(', 'int upload_config_valid(')
        code = code.replace('static int upload_state_load(', 'int upload_state_load(')
        code += choose + schedule
        code += r'''
int main(void){
 busy=1;assert(!begin_auto()&&!active&&!visited);busy=0;
 activity++;assert(!begin_auto()&&next_auto==5000);now=5000;
 assert(begin_auto()&&active&&auto_job&&segment==1&&!strcmp(session,items[0].id));
 schedule_after_job(-EIO);active=0;assert(failures==1&&next_auto==now+20000);
 assert(!begin_auto());now=next_auto;
 assert(begin_auto()&&segment==3);schedule_after_job(-EIO);active=0;assert(failures==2&&next_auto==now+40000);
 now=next_auto;assert(begin_auto()&&segment==1&&!strcmp(session,items[4].id));schedule_after_job(0);active=0;assert(!failures&&next_auto==now+5000);
 now=next_auto;assert(begin_auto()&&!strcmp(session,items[0].id)&&segment==1);schedule_after_job(-ECANCELED);active=0;assert(!failures);
 for(int i=0;i<7;i++){auto_job=1;schedule_after_job(-EIO);}assert(failures==5&&next_auto==now+300000);
 auto_paused=1;now=next_auto;assert(!begin_auto());auto_paused=0;
 metadata_failure=-EIO;assert(!begin_auto()&&!active&&scan_error==-EIO);metadata_failure=0;
 now=next_auto;scan_failure=-ECANCELED;assert(!begin_auto()&&!active&&stage==CANCELLED);scan_failure=0;
 for(unsigned i=0;i<sizeof(items)/sizeof(items[0]);i++)items[i].state=UPLOAD_STORED;
 now=next_auto;assert(!begin_auto()&&!active&&next_auto==now+300000);
 struct selection select={0};memset(select.destination,0x33,32);assert(choose_visit(items[0].id,1,60975,true,&select)==0&&select.first[0].session[0]);
 memset(&select,0,sizeof(select));memset(select.destination,0x22,32);assert(choose_visit(items[0].id,1,60974,true,&select)==0&&select.first[0].session[0]);
 memset(&select,0,sizeof(select));
 for(unsigned n=20;n>8;n--)add_candidate(select.first,&select.first_count,items[0].id,n);
 assert(select.first_count==AUTO_BATCH_FILES);
 for(unsigned n=0;n<AUTO_BATCH_FILES;n++)assert(select.first[n].segment==n+9);
 add_candidate(select.first,&select.first_count,items[0].id,9);
 assert(select.first_count==AUTO_BATCH_FILES&&select.first[1].segment==10);
 auto_batch=k_calloc(1,sizeof(*auto_batch));auto_batch->first_count=2;
 add_candidate(auto_batch->first,&auto_batch->after_count,items[0].id,9);
 add_candidate(auto_batch->first,&auto_batch->after_count,items[0].id,10);
 deadline=now+300000;
 assert(next_auto_file()&&segment==9&&auto_batch->index==1);
 cancel=1;assert(!next_auto_file()&&auto_batch->index==1);cancel=0;
 assert(next_auto_file()&&segment==10&&auto_batch->index==2);
 assert(!next_auto_file());
 assert(refill_auto_batch()==0); /* All persisted items are already stored. */
 items[0].state=UPLOAD_PENDING;
 assert(refill_auto_batch()==1&&auto_batch->first_count==1);
 assert(next_auto_file()&&segment==1); /* Wrap discovers earlier/new work. */
 items[0].state=UPLOAD_STORED;assert(refill_auto_batch()==0);
#ifdef CONFIG_CLIP_DTLS_PREPARE_MEASURE
 /* A measurement replays stored files in exactly one session, once only. */
 cursor_session[0]=0;cursor_segment=0;
 strcpy(measurement_session,items[0].id);
 assert(refill_auto_batch()==1&&auto_batch->first_count==3);
 for(unsigned n=1;n<=3;n++)assert(next_auto_file()&&segment==n&&!strcmp(session,measurement_session));
 assert(refill_auto_batch()==0); /* Must not wrap and replay forever. */
 strcpy(measurement_session,items[3].id);cursor_session[0]=0;cursor_segment=0;
 assert(refill_auto_batch()==0); /* Active/unsealed recording stays excluded. */
 auto_job=1;schedule_after_job(0);assert(!measurement_session[0]);
 auto_batch=k_calloc(1,sizeof(*auto_batch));
#endif
 k_free(auto_batch);auto_batch=NULL;
 allocation_failure=1;now=next_auto;assert(!begin_auto()&&!active&&!auto_job&&scan_error==-ENOMEM&&!auto_batch);
 return 0;
}
'''
        compile_run(code)
        diagnostic = code.replace('static char cursor_session[15],session[32];',
                                  'static char cursor_session[15],session[32],measurement_session[15];')
        compile_run('#define CONFIG_CLIP_DTLS_PREPARE_MEASURE 1\n' + diagnostic)

    def test_filesystem_scan_exceeds_session_limit_and_cleans_up_on_cancel(self):
        import datetime
        import json
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = []
            for index in range(205):
                key = (datetime.datetime(2026, 10, 20) + datetime.timedelta(seconds=index)).strftime('%Y%m%d%H%M%S')
                folder = root/'REC'/key[:8]/key[8:10]/key[10:12]/key[12:14]
                (folder/'0').mkdir(parents=True)
                (folder/'session.json').write_text(json.dumps({'recording': index == 204}))
                (folder/'0/0001.opus').write_bytes(b'recording')
                (folder/'0/0000.opus').write_bytes(b'invalid')
                (folder/'0/002x.opus').write_bytes(b'invalid')
                (folder/'0/0002.opus').mkdir()
                sessions.append(folder)
            (sessions[0]/'99').mkdir()
            (sessions[0]/'99/10000.opus').write_bytes(b'large-number')
            (sessions[0]/'0/10000.opus').write_bytes(b'wrong-group')
            code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <stddef.h>
#include <string.h>
#include <ctype.h>
#include <errno.h>
#include <dirent.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/stat.h>
#include "upload_scan.h"
#define STORAGE_PATH_MAX 128
#define FS_O_READ 1
#define FS_DIR_ENTRY_DIR 2
#define FS_DIR_ENTRY_FILE 1
#define JSON_TOK_TRUE 1
#define JSON_OBJ_DESCR_PRIM_NAMED(t,n,m,k) {0}
struct json_obj_descr {int unused;};
/* Adapter for Zephyr's boolean JSON field decoder. */
static int json_obj_parse(char *body,size_t n,const struct json_obj_descr *d,int count,void *out){
 (void)n;(void)d;(void)count;if(strstr(body,"false")){*(bool*)out=false;return 1;}
 if(strstr(body,"true")){*(bool*)out=true;return 1;}return -EINVAL;
}
struct fs_file_t {int fd;};
struct fs_dir_t {DIR *dir;char path[1024];};
struct fs_dirent {int type;uint32_t size;char name[256];};
static const char *root;
static int handles,files,tick,cancel_at,count,sealed_count,large;
static char path_buffer[1024];
static const char *mapped(const char *path){assert(!strncmp(path,"/SD:/",5));snprintf(path_buffer,sizeof(path_buffer),"%s%s",root,path+4);return path_buffer;}
static int storage_ensure_mounted(void){return 0;}
static void *k_calloc(size_t n,size_t size){return calloc(n,size);}
static void k_free(void *p){free(p);}
static void fs_file_t_init(struct fs_file_t *f){f->fd=-1;}
static int fs_open(struct fs_file_t *f,const char *path,int flags){(void)flags;f->fd=open(mapped(path),O_RDONLY);if(f->fd<0)return -errno;files++;return 0;}
static ssize_t fs_read(struct fs_file_t *f,void *p,size_t n){return read(f->fd,p,n);}
static int fs_close(struct fs_file_t *f){files--;return close(f->fd);}
static void fs_dir_t_init(struct fs_dir_t *d){memset(d,0,sizeof(*d));}
static int fs_opendir(struct fs_dir_t *d,const char *p){if(handles>=4)return -ENOMEM;strcpy(d->path,mapped(p));d->dir=opendir(d->path);if(!d->dir)return -errno;handles++;return 0;}
static int fs_readdir(struct fs_dir_t *d,struct fs_dirent *e){
 struct dirent *entry=readdir(d->dir);if(!entry){e->name[0]=0;return 0;}
 strcpy(e->name,entry->d_name);char path[2048];snprintf(path,sizeof(path),"%s/%s",d->path,e->name);
 struct stat st;assert(!stat(path,&st));e->type=S_ISDIR(st.st_mode)?FS_DIR_ENTRY_DIR:FS_DIR_ENTRY_FILE;e->size=st.st_size;return 0;
}
static int fs_closedir(struct fs_dir_t *d){handles--;return closedir(d->dir);}
static int storage_build_session_metadata_path(const char *s,char *p,size_t n){snprintf(p,n,"/SD:/REC/%.8s/%.2s/%.2s/%.2s/session.json",s,s+8,s+10,s+12);return 0;}
static int storage_build_chunk_path(const char *s,uint32_t i,char *p,size_t n){snprintf(p,n,"/SD:/REC/%.8s/%.2s/%.2s/%.2s/%u/%04u.opus",s,s+8,s+10,s+12,(i-1)/100,i);return 0;}
static int check(void){return cancel_at&&++tick>=cancel_at?-ECANCELED:0;}
static int visit(const char *s,uint32_t i,uint32_t size,bool sealed,void *ctx){
 assert(strlen(s)==14&&size&&ctx==&count);count++;if(sealed)sealed_count++;if(i==10000)large++;return 0;
}
'''
            code += without_includes(ROOT/'applications/clip/src/upload_scan.c')
            code += r'''
int main(int argc,char **argv){
 assert(argc==2);root=argv[1];assert(upload_scan(visit,&count,check)==0);
 assert(count==206&&sealed_count==205&&large==1&&!handles&&!files);
 count=sealed_count=large=0;
 assert(upload_scan_after(visit,&count,check,"20261020000320")==0);
 assert(count==5&&sealed_count==4&&!large&&!handles&&!files);
 count=sealed_count=large=0;cancel_at=30;
 assert(upload_scan(visit,&count,check)==-ECANCELED&&!handles&&!files);return 0;
}
'''
            compile_run(code, [directory])

    def test_sntp_cancel_timeout_invalid_time_and_cache(self):
        source = (ROOT/'applications/clip/src/https_poc.c').read_text()
        sync = 'static int sync_upload_time' + source.split('static int sync_upload_time', 1)[1].split('static void schedule_after_job', 1)[0]
        code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <errno.h>
#include <time.h>
#include "upload_config.h"
#define AF_INET 1
#define SOCK_DGRAM 2
struct zsock_addrinfo {int ai_family,ai_socktype;void *ai_addr;unsigned ai_addrlen;};
struct sntp_ctx {int open;};
struct sntp_time {uint64_t seconds;};
static struct upload_config settings;
static int time_ready;
static int64_t time_synced_at,now;
static int atomic_get(int *v){return *v;}
static void atomic_set(int *v,int n){*v=n;}
static void atomic_clear(int *v){*v=0;}
static int sends,receives,closes,allocated,cancel_at,mode;
static int64_t k_uptime_get(void){return now;}
static int check_cancel(void){return cancel_at&&now>=cancel_at?-ECANCELED:0;}
static int zsock_getaddrinfo(const char *h,const char *p,const struct zsock_addrinfo *hint,struct zsock_addrinfo **out){
 (void)h;(void)p;static struct zsock_addrinfo address;assert(hint->ai_family==AF_INET);
 if(mode==3)return -1;allocated++;*out=&address;return 0;
}
static void zsock_freeaddrinfo(struct zsock_addrinfo *a){(void)a;allocated--;}
static int sntp_init(struct sntp_ctx *c,void *a,unsigned length){(void)a;(void)length;c->open=1;return 0;}
static int sntp_query(struct sntp_ctx *c,uint32_t timeout,struct sntp_time *t){(void)t;assert(c->open&&timeout==50);sends++;now+=50;return -ETIMEDOUT;}
static int sntp_recv_response(struct sntp_ctx *c,uint32_t timeout,struct sntp_time *t){
 assert(c->open&&timeout==50);receives++;now+=50;if(mode==1)return -ETIMEDOUT;t->seconds=mode==2?1:1800000000;return 0;
}
static void sntp_close(struct sntp_ctx *c){assert(c->open);c->open=0;closes++;}
static int fake_clock_settime(int clock,const struct timespec *t){assert(clock==CLOCK_REALTIME&&t->tv_sec==1800000000);return 0;}
static int config_set_time(int64_t t){assert(t==1800000000);return 0;}
#define clock_settime fake_clock_settime
'''
        code += sync
        code += r'''
int main(void){
 assert(sync_upload_time()==0&&time_ready&&sends==1&&closes==1&&!allocated);
 assert(sync_upload_time()==0&&sends==1);
 now+=6*60*60*1000;assert(sync_upload_time()==0&&sends==2&&closes==2);
 time_ready=false;mode=1;now=0;cancel_at=100;assert(sync_upload_time()==-ECANCELED&&!time_ready&&closes==3&&!allocated);
 cancel_at=0;mode=1;now=0;assert(sync_upload_time()==-ETIMEDOUT&&now>=5000&&closes==4&&!allocated);
 mode=2;assert(sync_upload_time()==-ERANGE&&!time_ready&&closes==5&&!allocated);
 mode=3;assert(sync_upload_time()==-EHOSTUNREACH&&!allocated);
 return 0;
}
'''
        compile_run(code)

    def test_cursor_pages_select_two_records_in_one_scan_without_duplicates(self):
        source = (ROOT/'applications/clip/src/https_poc.c').read_text()
        compare = 'static int compare_key' + source.split('static int compare_key', 1)[1].split('static void add_candidate', 1)[0]
        valid = 'static bool valid_session' + source.split('static bool valid_session', 1)[1].split('static int hash_file_range', 1)[0]
        listing = source[source.index('struct list_item {'):source.index('/* SNTP supplies wall time')]
        code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include "upload_state.h"
#define MAX_FILE (8U*1024U*1024U)
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define CLIP_STATE_IDLE 0
struct candidate {char session[15];uint32_t segment;};
static char keys[205][15];
static int scans,state,metadata_error;
static int clip_event_get_state(void){return state;}
static int destination_digest(uint8_t *p){memset(p,0x22,32);return 0;}
const char *upload_state_name(uint32_t s){(void)s;return "pending";}
int upload_state_load(const char *s,uint32_t n,struct upload_file_record *r){(void)s;(void)n;(void)r;return metadata_error?metadata_error:-ENOENT;}
typedef int (*visit_fn)(const char*,uint32_t,uint32_t,bool,void*);
static int upload_scan_after(visit_fn visit,void *context,int (*check)(void),const char *after){
 scans++;
 for(int i=204;i>=0;i--){
  assert(check()==0);if(after&&strcmp(keys[i],after)<0)continue;
  int ret=visit(keys[i],1,100,true,context);if(ret)return ret;
 }
 return 0;
}
'''
        code += compare + valid + listing
        code += r'''
int main(void){
 for(unsigned i=0;i<205;i++)snprintf(keys[i],sizeof(keys[i]),"20261020%06u",i);
 char cursor[32]="0",out[1024];
 for(unsigned i=0;i<205;i+=2){
  assert(list_states(cursor,out,sizeof(out))>0);
  assert(strstr(out,keys[i])&&strstr(out,"\"eligible\":true"));
  if(i+1<205)assert(strstr(out,keys[i+1]));
  unsigned last=i+1<205?i+1:i;snprintf(cursor,sizeof(cursor),"%s:1",keys[last]);
  assert(strstr(out,i+2<205?"\"more\":true":"\"more\":false"));
 }
 assert(scans==103);
 assert(list_states(cursor,out,sizeof(out))>0&&strstr(out,"\"items\":[]"));
 assert(list_states("bad",out,sizeof(out))==-EINVAL);
 assert(list_states("20261020000000:0",out,sizeof(out))==-EINVAL);
 assert(list_states("0",out,16)==-ENOSPC);
 state=1;assert(list_states("0",out,sizeof(out))==-EBUSY);state=0;
 metadata_error=-EIO;assert(list_states("0",out,sizeof(out))==-EIO);
 return 0;
}
'''
        compile_run(code)
