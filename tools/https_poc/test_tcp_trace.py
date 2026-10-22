"""Exercise the real C trace ring, including overflow and control safety."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from instrument_tcp import generate, transform

ROOT = Path(__file__).resolve().parents[2]


class TcpTraceTest(unittest.TestCase):
    def test_changed_sdk_fails_without_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'subsys/net/ip/tcp.c'
            source.parent.mkdir(parents=True)
            source.write_text('different SDK')
            output = root / 'generated.c'
            with self.assertRaises(ValueError):
                generate(root, output)
            self.assertFalse(output.exists())
            with self.assertRaises(ValueError):
                transform('missing insertion points')

    def test_real_ring_memory_safety_and_cursor(self):
        source = (ROOT / 'applications/clip/src/tcp_trace.c').read_text()
        source = '\n'.join(line for line in source.splitlines() if not line.startswith('#include'))
        fixture = r'''
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "tcp_trace.h"
struct k_spinlock { int unused; };
typedef int k_spinlock_key_t;
static int k_spin_lock(struct k_spinlock *p) { (void)p; return 0; }
static void k_spin_unlock(struct k_spinlock *p,int k) { (void)p; (void)k; }
static uint32_t now;
static uint32_t k_uptime_get_32(void) { return now; }
static bool fail_alloc;
static void *k_calloc(size_t n,size_t s) { return fail_alloc ? NULL : calloc(n,s); }
static void k_free(void *p) { free(p); }
'''
        fixture += source
        fixture += r'''
int main(void) {
 char out[1024];
 struct clip_tcp_event e = {.kind=CLIP_TCP_RTO,.seq=4000000000U,.rto=249,.value=-5};
 clip_tcp_trace(&e);
 assert(clip_tcp_trace_command("0",false,out,sizeof(out))>0); puts(out);
 assert(clip_tcp_trace_command("on",false,out,sizeof(out))>0); puts(out);
 assert(clip_tcp_trace_command("off",true,out,sizeof(out))==-EBUSY);
 assert(clip_tcp_trace_command("on",true,out,sizeof(out))==-EBUSY);
 assert(clip_tcp_trace_command("",false,out,sizeof(out))==-EINVAL);
 assert(clip_tcp_trace_command("-1",false,out,sizeof(out))==-EINVAL);
 assert(clip_tcp_trace_command("4294967296",false,out,sizeof(out))==-EINVAL);
 now=UINT32_MAX; clip_tcp_trace(&e);
 assert(clip_tcp_trace_command("0",true,out,sizeof(out))>0); puts(out);
 assert(clip_tcp_trace_command("0",true,out,10)==-ENOSPC);
 for(int i=0;i<130;i++) { now++; clip_tcp_trace(&e); }
 assert(clip_tcp_trace_command("0",true,out,sizeof(out))>0); puts(out);
 assert(clip_tcp_trace_command("131",true,out,sizeof(out))>0); puts(out);
 fail_alloc=true;
 assert(clip_tcp_trace_command("on",false,out,sizeof(out))==-ENOMEM);
 assert(clip_tcp_trace_command("130",true,out,sizeof(out))>0); puts(out);
 assert(clip_tcp_trace_command("off",false,out,sizeof(out))>0);
 clip_tcp_trace(&e);
 assert(clip_tcp_trace_command("0",true,out,sizeof(out))>0); puts(out);
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'test.c').write_text(fixture)
            subprocess.run(['clang', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                            '-fsanitize=address,undefined', '-I' + str(ROOT / 'applications/clip/include'),
                            str(root / 'test.c'), '-o', str(root / 'test')], check=True)
            output = subprocess.check_output([str(root / 'test')], text=True)
        rows = [json.loads(line)['data'] for line in output.splitlines() if line]
        self.assertFalse(rows[0]['enabled'])
        self.assertEqual(rows[1]['bytes'], 8196)
        self.assertEqual(rows[2]['event']['snd_una'], 4000000000)
        self.assertEqual(rows[2]['event']['value'], -5)
        self.assertEqual(rows[2]['event']['ms'], 4294967295)
        self.assertTrue(rows[3]['lost'])
        self.assertEqual(rows[3]['event']['seq'], 4)
        self.assertEqual(rows[3]['oldest'], 4)
        self.assertIsNone(rows[4]['event'])
        self.assertEqual(rows[5]['event']['seq'], 131)
        self.assertFalse(rows[6]['enabled'])
