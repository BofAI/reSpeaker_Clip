"""Compile the real RAM-only parameter parser: malformed inputs preserve config."""
from pathlib import Path
import subprocess
import tempfile
import unittest
ROOT = Path(__file__).resolve().parents[2]
class WindowConfigTest(unittest.TestCase):
    def test_real_parameter_parser(self):
        source = (ROOT/'applications/clip/src/https_poc.c').read_text()
        split = 'static char *split_field'+source.split('static char *split_field',1)[1].split('static void request_cancel',1)[0]
        helper = 'static int configure_datagram_window'+source.split('static int configure_datagram_window',1)[1].split('\n#endif',1)[0]
        code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
static uint16_t datagram_initial_window=16,datagram_window_ceiling=64;
'''+split+helper+r'''
int main(void){
 char out[128];
 assert(configure_datagram_window("window",out,sizeof(out))>0 && strstr(out,"\"initial_packets\":16"));
 assert(configure_datagram_window("window,128,128",out,sizeof(out))>0 && datagram_initial_window==128 && datagram_window_ceiling==128);
 const char *bad[]={NULL,"","timing","window,","window,16","window,16,64,","window,16,64,1","window,0,64","window,15,64","window,65,64","window,16,129","window,-1,64","window,+16,64","window,16x,64","window,16,64 ","window,99999999999999999999999,64"};
 for(unsigned i=0;i<sizeof(bad)/sizeof(bad[0]);i++){
  assert(configure_datagram_window(bad[i],out,sizeof(out))==-EINVAL);
  assert(datagram_initial_window==128 && datagram_window_ceiling==128);
 }
 assert(configure_datagram_window("window,16,64",out,sizeof(out))>0 && datagram_initial_window==16 && datagram_window_ceiling==64);
 assert(configure_datagram_window("window",out,4)==-ENOSPC);
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);(p/'test.c').write_text(code)
            subprocess.run(['clang','-std=c11','-Wall','-Wextra','-Werror','-fsanitize=address,undefined',str(p/'test.c'),'-o',str(p/'test')],check=True)
            subprocess.run([str(p/'test')],check=True)
if __name__=='__main__': unittest.main()
