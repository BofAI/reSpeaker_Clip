"""Bounded tuning combinations, malformed input and server capability checks."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class TuneTest(unittest.TestCase):
    def test_actual_c_parser_and_receiver_limit(self):
        code = r'''
#include "upload_tune.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
int main(void) {
 struct upload_tuning t={16384,2048,8192,1};char text[80];
 const unsigned chunks[]={16384,32768,65536,131072},writes[]={2048,4096},windows[]={4096,5461,8192,10240,12288,16384};
 for(unsigned c=0;c<4;c++)for(unsigned w=0;w<2;w++)for(unsigned s=0;s<6;s++)for(unsigned n=0;n<2;n++) {
  snprintf(text,sizeof(text),"%u,%u,%u,%u",chunks[c],writes[w],windows[s],n);
  assert(!upload_tune_parse(text,&t));
  assert(t.chunk_size==chunks[c] && t.write_size==writes[w] && t.send_window==windows[s] && t.nodelay==n);
 }
 const char *bad[]={"","16384,2048,8192","16384,2048,8192,1,0","16384,2048,8192,1,",
  "16384,2048,8192,",",2048,8192,1","16384,2048,8192,2","98304,2048,8192,1",
  "262144,2048,8192,1","1000000,2048,8192,1",
  "16384,1024,8192,1","16384,2048,24576,1","+16384,2048,8192,1",
  " 16384,2048,8192,1","16384,2048,8192,1 ","16384,2048,8192,-1",
  "999999999999999999999999,2048,8192,1","16384,,8192,1"};
 for(unsigned i=0;i<sizeof(bad)/sizeof(bad[0]);i++) {
  struct upload_tuning before=t;assert(upload_tune_parse(bad[i],&t)==-EINVAL);
  assert(!memcmp(&before,&t,sizeof(t)));
 }
 assert(upload_tune_parse(NULL,&t)==-EINVAL && upload_tune_parse("",NULL)==-EINVAL);
 assert(upload_tune_server_limit(16384,0,false)==16384);
 assert(upload_tune_server_limit(32768,0,false)==-EMSGSIZE);
 assert(upload_tune_server_limit(32768,16384,true)==-EMSGSIZE);
 assert(upload_tune_server_limit(32768,32768,true)==32768);
 assert(upload_tune_server_limit(16384,32768,true)==32768);
 assert(upload_tune_server_limit(16384,0,true)==-EBADMSG);
 assert(upload_tune_server_limit(65536,65536,true)==65536);
 assert(upload_tune_server_limit(131072,131072,true)==131072);
 assert(upload_tune_server_limit(32768,131072,true)==131072);
 assert(upload_tune_server_limit(131072,65536,true)==-EMSGSIZE);
 assert(upload_tune_server_limit(65536,32768,true)==-EMSGSIZE);
 assert(upload_tune_server_limit(131072,0,false)==-EMSGSIZE);
 assert(upload_tune_server_limit(16384,98304,true)==-EBADMSG);
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'tune.c').write_text(code)
            subprocess.run(['clang', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            '-fsanitize=address,undefined',
                            '-I', str(ROOT / 'applications/clip/include'),
                            str(root / 'tune.c'), str(ROOT / 'applications/clip/src/upload_tune.c'),
                            '-o', str(root / 'tune')], check=True)
            subprocess.run([str(root / 'tune')], check=True)
