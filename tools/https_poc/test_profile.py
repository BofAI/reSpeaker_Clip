"""Validate timing publication, bounded history, and unchanged durable receipts."""
from concurrent.futures import ThreadPoolExecutor
import errno
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from profile_timing import FileSink, identify, measure, request
from resume_store import ResumeStore

ROOT = Path(__file__).resolve().parents[2]


class ProfileTest(unittest.TestCase):
    def test_server_measurements_are_request_local_and_do_not_change_storage(self):
        records = []
        with tempfile.TemporaryDirectory() as directory:
            store = ResumeStore(directory, threading.Lock())

            def run(number):
                body = bytes([number]) * 20000
                digest = hashlib.sha256(body).hexdigest()
                identity = hashlib.sha256(str(number).encode()).hexdigest()
                with request(records.append, 'PUT'):
                    identify(object_id=identity, offset=0, length=len(body))
                    with measure('body_receive_us'):
                        pass
                    for offset in (0, 16384):
                        block = body[offset:offset + 16384]
                        code, receipt = store.commit(identity, len(body), digest, offset,
                                                     block, hashlib.sha256(block).hexdigest())
                    self.assertEqual(code, 201)
                    self.assertEqual(receipt['offset'], len(body))
                    self.assertEqual((Path(directory) / (identity + '.bin')).read_bytes(), body)

            with ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(run, (1, 2, 3)))
        self.assertEqual(len(records), 3)
        self.assertEqual(len({r['object_id'] for r in records}), 3)
        for record in records:
            for name in ('elapsed_us', 'body_receive_us', 'lock_wait_us', 'chunk_hash_us',
                         'load_us', 'data_write_us', 'data_fsync_us', 'checkpoint_us',
                         'checkpoint_fsync_us', 'directory_fsync_us', 'whole_hash_us'):
                self.assertGreaterEqual(record[name], 0)
            self.assertNotIn('Authorization', json.dumps(record))

    def test_off_mode_sink_failure_and_bounded_private_output(self):
        def failing_sink(record):
            raise OSError('test sink failure')
        with request(None, 'PUT'):
            identify(unused='not retained')
            with measure('unused'):
                pass
        with request(failing_sink, 'PUT'):
            identify(offset=0)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'profile.jsonl'
            sink = FileSink(target, limit=2)
            try:
                for number in range(4):
                    sink({'number': number})
                with self.assertRaises(FileExistsError):
                    FileSink(target)
            finally:
                sink.close()
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual([json.loads(x)['number'] for x in target.read_text().splitlines()], [0, 1])

    def test_real_c_ring_cursor_overwrite_busy_oom_and_disable(self):
        source = (ROOT / 'applications/clip/src/upload_perf.c').read_text()
        source = '\n'.join(line for line in source.splitlines() if not line.startswith('#include'))
        fixture = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include "upload_perf.h"
struct k_spinlock {int unused;};
typedef int k_spinlock_key_t;
static int k_spin_lock(struct k_spinlock *p){(void)p;return 0;}
static void k_spin_unlock(struct k_spinlock *p,int key){(void)p;(void)key;}
static uint64_t clock_us;
static uint64_t k_uptime_ticks(void){return clock_us;}
static uint64_t k_ticks_to_us_floor64(uint64_t value){return value;}
static bool fail_alloc;
static void *k_calloc(size_t n,size_t s){return fail_alloc?NULL:calloc(n,s);}
static void k_free(void *p){free(p);}
static int limited_snprintf(char *out,size_t size,const char *format,...){
 assert(!strstr(format,"llu")&&!strstr(format,"lld"));
 va_list args;va_start(args,format);int result=vsnprintf(out,size,format,args);va_end(args);return result;
}
#define snprintf limited_snprintf
'''
        fixture += source
        fixture += r'''
int main(void){
 char out[2048];
 assert(upload_perf_command("on",false,out,sizeof(out))>0);puts(out);
 assert(upload_perf_command("off",true,out,sizeof(out))==-EBUSY);
 assert(upload_perf_command("on",true,out,sizeof(out))==-EBUSY);
 assert(upload_perf_command("",false,out,sizeof(out))==-EINVAL);
 assert(upload_perf_command("-1",false,out,sizeof(out))==-EINVAL);
 assert(upload_perf_command("4294967296",false,out,sizeof(out))==-EINVAL);
 clock_us=100;upload_perf_begin(UP_PUT,"20261021000001",3,16384,16384);
 upload_perf_add(UP_WAIT_WRITE_US,75);upload_perf_add(UP_WANT_WRITE,2);
 assert(upload_perf_command("0",true,out,sizeof(out))>0);puts(out);
 clock_us=300;upload_perf_end(-ECANCELED);
 assert(upload_perf_command("0",true,out,sizeof(out))>0);puts(out);
 assert(upload_perf_command("0",true,out,8)==-ENOSPC);
 for(int i=0;i<20;i++){upload_perf_begin(UP_GET,NULL,0,i,0);clock_us+=10;upload_perf_end(0);}
 assert(upload_perf_command("0",true,out,sizeof(out))>0);puts(out);
 assert(upload_perf_command("21",true,out,sizeof(out))>0);puts(out);
 fail_alloc=true;assert(upload_perf_command("on",false,out,sizeof(out))==-ENOMEM);
 assert(upload_perf_command("21",false,out,sizeof(out))>0);puts(out);
 assert(upload_perf_command("off",false,out,sizeof(out))>0);
 upload_perf_begin(UP_GET,NULL,0,0,0);upload_perf_add(UP_READ_US,10);upload_perf_end(0);
 assert(upload_perf_command("0",true,out,sizeof(out))>0);puts(out);
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'test.c').write_text(fixture)
            subprocess.run(['clang', '-std=gnu11', '-DCONFIG_CLIP_HTTPS_PROFILE=1',
                            '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
                            '-I' + str(ROOT / 'applications/clip/include'), str(root / 'test.c'),
                            '-o', str(root / 'test')], check=True)
            output = subprocess.check_output([str(root / 'test')], text=True)
        records = [json.loads(line)['data'] for line in output.splitlines() if line]
        self.assertTrue(records[0]['enabled'])
        self.assertIsNone(records[1]['event'])
        event = records[2]['event']
        self.assertEqual((event['session'], event['segment'], event['offset']), ('20261021000001', 3, 16384))
        self.assertEqual(event['result'], -errno.ECANCELED)
        self.assertEqual(event['elapsed_us'], 200)
        self.assertEqual(event['started_us'], 100)
        self.assertEqual(event['values'][7], 75)
        self.assertEqual(event['values'][11], 2)
        self.assertTrue(records[3]['lost'])
        self.assertEqual(records[3]['event']['seq'], 6)
        self.assertEqual(records[4]['latest'], 21)
        self.assertIsNone(records[4]['event'])
        self.assertEqual(records[5]['latest'], 21)
        self.assertFalse(records[6]['enabled'])
