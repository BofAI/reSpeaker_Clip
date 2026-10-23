"""Run the actual bounded-cache C path with short reads and cancellation."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class UploadCacheTest(unittest.TestCase):
    def test_hash_retains_exact_bytes_and_send_does_not_reread_sd(self):
        text = (ROOT / 'applications/clip/src/https_poc.c').read_text()
        hashing = text[text.index('static int hash_file_range('):text.index('static int hash_file(void)')]
        sending = text[text.index('static int send_cached_payload('):text.index('\n#endif\n\nstatic int send_payload')]
        fixture = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
#include <sys/types.h>
#define MIN(a,b) ((a)<(b)?(a):(b))
#define FILE_READ_SIZE 2048U
#define FS_SEEK_SET 0
#define UP_READ_US 1
#define UP_FS_READS 2
#define UP_HASH_US 3
#define SENDING 1
struct https_stream { int unused; };
typedef struct { size_t count; unsigned int sum; } mbedtls_sha256_context;
static unsigned char input[70000], io[4096], bench_chunk[65536], output[65536];
static size_t position, produced, bench_cached_offset, bench_cached_length;
static struct { size_t write_size; } job_tuning;
static int file, stage, bytes_sent, reads, writes, checks, cancel_on, error_on, eof_on;
static int seek_error_on, seeks;
static void atomic_set(int *p,int n) { *p=n; }
static int check_cancel(void) { return ++checks==cancel_on?-ECANCELED:0; }
static void progress(void) {}
static uint64_t upload_perf_now(void) { return 0; }
static void upload_perf_add(int m,uint64_t n) {(void)m;(void)n;}
static int fs_seek(int *f,size_t offset,int whence) {
 assert(f==&file && whence==FS_SEEK_SET);seeks++;
 if(seeks==seek_error_on)return -EIO;
 assert(offset<=sizeof(input));position=offset;return 0;
}
static ssize_t fs_read(int *f,void *to,size_t n) {
 assert(f==&file && n<=2048);reads++;
 if(reads==error_on)return -ENODEV;
 if(reads==eof_on)return 0;
 n=MIN(n,733);assert(position+n<=sizeof(input));
 memcpy(to,input+position,n);position+=n;return n;
}
static void mbedtls_sha256_init(mbedtls_sha256_context *c) {memset(c,0,sizeof(*c));}
static int mbedtls_sha256_starts(mbedtls_sha256_context *c,int sha224) {(void)c;assert(!sha224);return 0;}
static int mbedtls_sha256_update(mbedtls_sha256_context *c,const void *p,size_t n) {
 const unsigned char *bytes=p;
 for(size_t i=0;i<n;i++)c->sum=c->sum*33+bytes[i];
 c->count+=n;return 0;
}
static int mbedtls_sha256_finish(mbedtls_sha256_context *c,unsigned char d[32]) {
 memset(d,0,32);memcpy(d,&c->sum,sizeof(c->sum));return 0;
}
static void mbedtls_sha256_free(mbedtls_sha256_context *c) {(void)c;}
static int https_stream_write(struct https_stream *s,const void *p,size_t n) {
 assert(s && n && n<=job_tuning.write_size);writes++;
 assert(produced+n<=sizeof(output));memcpy(output+produced,p,n);produced+=n;return 0;
}
static void reset(void) {
 position=produced=reads=writes=checks=bytes_sent=seeks=0;
 cancel_on=error_on=eof_on=seek_error_on=0;
}
'''
        main = r'''
int main(void) {
 struct https_stream stream={0};char cached[65], uncached[65];
 for(size_t i=0;i<sizeof(input);i++)input[i]=(unsigned char)(i*19);
 const size_t lengths[]={1,2047,2048,2049,4096,65535,65536};
 for(size_t w=2048;w<=4096;w*=2)for(size_t i=0;i<sizeof(lengths)/sizeof(lengths[0]);i++) {
  reset();job_tuning.write_size=w;bench_cached_offset=123;bench_cached_length=lengths[i];
  assert(hash_file_range(123,lengths[i],cached,bench_chunk)==0);
  assert(position==123 && !memcmp(bench_chunk,input+123,lengths[i]));
  int read_count=reads;assert(send_cached_payload(&stream,123,lengths[i])==(int)lengths[i]);
  assert(reads==read_count && produced==lengths[i] && bytes_sent==123+(int)lengths[i]);
  assert(!memcmp(output,input+123,lengths[i]));
  reset();assert(hash_file_range(123,lengths[i],uncached,NULL)==0);
  assert(!strcmp(cached,uncached));
 }
 reset();cancel_on=2;assert(hash_file_range(0,4096,cached,bench_chunk)==-ECANCELED);assert(reads==1);
 reset();error_on=2;assert(hash_file_range(0,4096,cached,bench_chunk)==-ENODEV);
 reset();eof_on=2;assert(hash_file_range(0,4096,cached,bench_chunk)==-EIO);
 reset();seek_error_on=2;assert(hash_file_range(0,4096,cached,bench_chunk)==-EIO);
 reset();job_tuning.write_size=2048;bench_cached_offset=123;bench_cached_length=4096;cancel_on=2;
 assert(send_cached_payload(&stream,123,4096)==-ECANCELED);
 assert(reads==0 && writes==1 && produced==2048 && bytes_sent==123+2048);
 reset();bench_cached_length=65537;assert(send_cached_payload(&stream,123,65537)==-EINVAL);
 reset();bench_cached_length=4096;assert(send_cached_payload(&stream,124,4096)==-EINVAL);
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'cache.c'
            executable = Path(directory) / 'cache'
            source.write_text(fixture + hashing + sending + main)
            subprocess.run(['clang', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            '-fsanitize=address,undefined', str(source), '-o', str(executable)], check=True)
            subprocess.run([str(executable)], check=True)


if __name__ == '__main__':
    unittest.main()
