"""Real counter/wrapper code: exclusive accounting, thread scope and bounded JSON."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import instrument_dtls_crypto as hooks

ROOT = Path(__file__).resolve().parents[2]

FIXTURE = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
#define CONFIG_CLIP_DTLS_PROFILE 1
#include "dtls_profile.h"
#ifndef CONFIG_SYS_CLOCK_TICKS_PER_SEC
#define CONFIG_SYS_CLOCK_TICKS_PER_SEC 1000000U
#endif
static uint64_t ticks;
static void *thread=(void*)1;
typedef void *atomic_ptr_t;
static void *atomic_ptr_get(atomic_ptr_t *p){return *p;}
static void atomic_ptr_set(atomic_ptr_t *p,void *v){*p=v;}
static void atomic_ptr_clear(atomic_ptr_t *p){*p=NULL;}
static void *k_current_get(void){return thread;}
static uint64_t k_uptime_ticks(void){return ticks;}
static uint64_t k_ticks_to_us_floor64(uint64_t t){return t*1000000U/CONFIG_SYS_CLOCK_TICKS_PER_SEC;}
typedef int psa_status_t;
typedef uint32_t mbedtls_svc_key_id_t;
typedef uint32_t psa_algorithm_t;
static uint8_t nonce[12],ad[3],plain[8],cipher[24],decoded[8];
static int crypto_result;
static int psa_aead_encrypt(mbedtls_svc_key_id_t key,psa_algorithm_t alg,
 const uint8_t*n,size_t nl,const uint8_t*a,size_t al,const uint8_t*p,size_t pl,
 uint8_t*c,size_t capacity,size_t*out){
 assert(key==7 && alg==9 && n==nonce && nl==12 && a==ad && al==3 && p==plain && pl==8 && c==cipher && capacity==24);
 ticks+=7;memcpy(c,p,pl);*out=pl;return crypto_result;}
static int psa_aead_decrypt(mbedtls_svc_key_id_t key,psa_algorithm_t alg,
 const uint8_t*n,size_t nl,const uint8_t*a,size_t al,const uint8_t*c,size_t cl,
 uint8_t*p,size_t capacity,size_t*out){
 assert(key==7 && alg==9 && n==nonce && nl==12 && a==ad && al==3 && c==cipher && cl==8 && p==decoded && capacity==8);
 ticks+=5;memcpy(p,c,cl);*out=cl;return crypto_result;}
'''
MAIN = r'''
static void add(enum dtls_profile_metric m,uint64_t t){ticks+=t;dtls_profile_add(m,t);}
static uint32_t sum(const struct dtls_profile_result *p){return p->source_us+p->encrypt_us+p->decrypt_us+p->udp_send_us+p->udp_recv_us+p->tls_other_us+p->poll_read_us+p->poll_write_us+p->other_us;}
int main(void){
 size_t outlen;struct dtls_profile_result p;
 memset(plain,31,sizeof(plain));
 assert(!clip_profile_aead_encrypt(7,9,nonce,12,ad,3,plain,8,cipher,24,&outlen));
 assert(outlen==8 && !memcmp(cipher,plain,8));
 dtls_profile_end(&p);assert(!p.valid);
 dtls_profile_begin();
 uint64_t source=ticks;add(DP_SD_SEEK,20);add(DP_SD_READ,80);ticks+=10;
 dtls_profile_add(DP_SOURCE,ticks-source);dtls_profile_add(DP_CACHE_HIT,0);
 uint64_t write=ticks;
 assert(!clip_profile_aead_encrypt(7,9,nonce,12,ad,3,plain,8,cipher,24,&outlen));
 add(DP_UDP_SEND,13);ticks+=5;dtls_profile_add(DP_TLS_WRITE,ticks-write);
 uint64_t read=ticks;
 add(DP_UDP_RECV,11);
 assert(!clip_profile_aead_decrypt(7,9,nonce,12,ad,3,cipher,8,decoded,8,&outlen));
 assert(outlen==8 && !memcmp(decoded,plain,8));
 ticks+=7;dtls_profile_add(DP_TLS_READ,ticks-read);
 dtls_profile_window(true);ticks+=200;dtls_profile_poll(false,200);
 dtls_profile_window(false);ticks+=40;dtls_profile_poll(true,40);
 thread=(void*)2;dtls_profile_add(DP_SD_READ,999999);
 crypto_result=-17;
 assert(clip_profile_aead_encrypt(7,9,nonce,12,ad,3,plain,8,cipher,24,&outlen)==-17);
 thread=(void*)1;crypto_result=0;ticks+=9;
 dtls_profile_end(&p);
 assert(p.valid && !p.saturated && p.sd_reads==1 && p.sd_seeks==1 && p.cache_hits==1);
 assert(p.encrypt_calls==1 && p.decrypt_calls==1 && p.source_calls==1 && p.window_stalls==1);
 assert(p.total_us==sum(&p));assert(p.encrypt_us==k_ticks_to_us_floor64(7));
 assert(p.source_us==k_ticks_to_us_floor64(110));
 assert(p.window_wait_us==k_ticks_to_us_floor64(200));
 assert(p.window_wait_us<=p.poll_read_us+p.poll_write_us);
 char text[1024];assert(dtls_profile_format(&p,text,sizeof(text))>0);puts(text);
 struct dtls_profile_result worst;memset(&worst,255,sizeof(worst));worst.valid=true;worst.saturated=true;
 assert(dtls_profile_format(&worst,text,sizeof(text))>0);
 char tiny[8];assert(dtls_profile_format(&p,tiny,sizeof(tiny))==-ENOSPC && !tiny[0]);
 dtls_profile_begin();add(DP_AEAD_ENCRYPT,100);dtls_profile_end(&p);assert(!p.valid);
 dtls_profile_begin();ticks+=(uint64_t)UINT32_MAX*CONFIG_SYS_CLOCK_TICKS_PER_SEC/1000000U+1;
 dtls_profile_end(&p);assert(!p.valid && p.saturated);
 dtls_profile_begin();dtls_profile_add(DP_SOURCE,UINT64_MAX);dtls_profile_add(DP_SOURCE,1);
 dtls_profile_end(&p);assert(!p.valid && p.saturated);
 return 0;
}
'''


class ProfileTest(unittest.TestCase):
    def test_actual_c_accounting_and_crypto_argument_forwarding(self):
        text = (ROOT / 'applications/clip/src/dtls_profile.c').read_text()
        source = '\n'.join(line for line in text.splitlines() if not line.startswith('#include'))
        for hz in (1000000, 32768):
            with self.subTest(tick_hz=hz), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                c = directory / 'profile.c'
                c.write_text(FIXTURE + source + MAIN)
                binary = directory / 'profile'
                result = subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=address,undefined', '-DCONFIG_SYS_CLOCK_TICKS_PER_SEC='+str(hz),
                    '-I'+str(ROOT / 'applications/clip/include'), str(c), '-o', str(binary)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                result = subprocess.run([str(binary)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                value = json.loads(result.stdout)['data']
                self.assertTrue(value['valid'])
                self.assertEqual(value['tick_hz'], hz)

    def test_source_transform_is_narrow_and_fail_closed(self):
        source = '#include "common.h"\na=psa_aead_encrypt(k);\nb=psa_aead_decrypt(k);\n'
        changed = hooks.transform(source)
        self.assertEqual(changed.replace('#include "dtls_profile_crypto.h"\n', '').replace('clip_profile_aead_', 'psa_aead_'), source)
        for text in ('', source+source, source.replace('psa_aead_encrypt(', 'changed(')):
            with self.assertRaises(ValueError):
                hooks.transform(text)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / 'modules/crypto/mbedtls/library/ssl_msg.c'
            original.parent.mkdir(parents=True)
            original.write_text(source)
            (root / 'zephyr').mkdir()
            target = root / 'generated/ssl_msg.c'
            with self.assertRaises(ValueError):
                hooks.generate(root / 'zephyr', target)
            self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
