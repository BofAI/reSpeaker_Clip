"""Exercise payload coalescing, cancellation and short SD reads in actual C."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class PayloadTest(unittest.TestCase):
    def test_both_write_sizes_preserve_bytes_and_cancel_before_each_read(self):
        text = (ROOT / 'applications/clip/src/https_poc.c').read_text()
        source = 'static int send_payload' + text.split('static int send_payload', 1)[1].split(
            'static int header_fragment', 1)[0]
        definitions = '\n'.join(line for line in text.splitlines()
                                if line.startswith(('#define FILE_READ_SIZE ', '#define TLS_WRITE_SIZE ')))
        fixture = r'''
#include <assert.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <errno.h>
#include <sys/types.h>
#include <stdlib.h>
#define __aligned(n) __attribute__((aligned(n)))
#define IS_ENABLED(x) (x)
#define MIN(a,b) ((a)<(b)?(a):(b))
#define SENDING 1
#define UP_READ_US 1
#define UP_FS_READS 2
struct https_stream { int unused; };
static int file, stage, bytes_sent, reads, writes, checks;
static int cancel_on, read_error_on, eof_on, write_error_on;
static size_t input_size, position, output_size, read_limit;
static unsigned char input[132000], output[132000];
static struct upload_tuning job_tuning = {.write_size=TLS_WRITE_SIZE};
static void atomic_set(int *p,int v) { *p=v; }
static int check_cancel(void) { return ++checks==cancel_on ? -ECANCELED : 0; }
static uint64_t upload_perf_now(void) { return 0; }
static void upload_perf_add(int k,uint64_t v) { (void)k;(void)v; }
static void progress(void) {}
static ssize_t fs_read(int *f,void *p,size_t length) {
 assert(f==&file && length<=2048);reads++;
 if(reads==read_error_on)return -ENODEV;
 if(reads==eof_on)return 0;
 length=MIN(length,MIN(input_size-position,read_limit));
 memcpy(p,input+position,length);position+=length;return length;
}
static int https_stream_write(struct https_stream *s,const void *p,size_t n) {
 assert(s && n && n<=TLS_WRITE_SIZE);writes++;
 if(writes==write_error_on)return -EPIPE;
 assert(output_size+n<=sizeof(output));memcpy(output+output_size,p,n);output_size+=n;return 0;
}
'''
        main = r'''
int main(void) {
 struct https_stream stream={0};
 buffers=malloc(sizeof(*buffers)+TLS_WRITE_SIZE);assert(buffers);
 assert((uintptr_t)io%8==0);
 for(size_t i=0;i<sizeof(input);i++)input[i]=(unsigned char)(i*17);
 const size_t lengths[]={1,2047,2048,2049,4095,4096,4097,16384,16999,65536,131072};
 for(size_t mode=0;mode<2;mode++)for(size_t i=0;i<sizeof(lengths)/sizeof(lengths[0]);i++) {
  input_size=lengths[i];position=output_size=reads=writes=checks=bytes_sent=0;
  read_limit=mode?733:2048;
  assert(send_payload(&stream,123,input_size)==(int)input_size);
  assert(output_size==input_size && !memcmp(output,input,input_size));
  assert(bytes_sent==123+(int)input_size && checks==reads);
  assert(writes==(int)((input_size+TLS_WRITE_SIZE-1)/TLS_WRITE_SIZE));
  if(!mode)assert(reads==(int)((input_size+2047)/2048));
 }
 input_size=16384;read_limit=2048;
 position=output_size=reads=writes=checks=bytes_sent=0;cancel_on=2;
 assert(send_payload(&stream,123,input_size)==-ECANCELED);
 assert(reads==1 && position==2048);
 assert(output_size==(TLS_WRITE_SIZE==2048?2048:0));
 cancel_on=0;
 position=output_size=reads=writes=checks=bytes_sent=0;read_error_on=2;
 assert(send_payload(&stream,123,input_size)==-ENODEV);read_error_on=0;
 position=output_size=reads=writes=checks=bytes_sent=0;eof_on=2;
 assert(send_payload(&stream,123,input_size)==-EIO);eof_on=0;
 position=output_size=reads=writes=checks=bytes_sent=0;write_error_on=1;
 assert(send_payload(&stream,123,input_size)==-EPIPE);
 assert(output_size==0 && bytes_sent==0);
 free(buffers);return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'payload.c'
            header = '#include "' + str(ROOT / 'applications/clip/include/upload_tune.h') + '"\n'
            buffers = text[text.index('static struct upload_buffers {'):text.index('#define recv_buffer')]
            fixture = fixture.replace('static unsigned char input[132000], output[132000];',
                                      buffers + '\nstatic unsigned char input[132000], output[132000];')
            path.write_text(header + definitions + '\n' + fixture + source + main)
            for enabled in (0, 1):
                executable = root / ('payload-' + str(enabled))
                subprocess.run(['clang', '-std=c11', '-Wall', '-Wextra', '-Werror',
                                '-fsanitize=address,undefined',
                                '-DCONFIG_CLIP_HTTPS_WRITE_4096=' + str(enabled),
                                str(path), '-o', str(executable)], check=True)
                subprocess.run([str(executable)], check=True)


if __name__ == '__main__':
    unittest.main()
