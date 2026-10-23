"""Real portable sender core: loss, stale ACKs, bounded RAM and wraparound."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class UploadWindowTest(unittest.TestCase):
    def test_selective_ack_retransmission_and_durable_progress(self):
        code = r'''
#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include <string.h>
#include "upload_window.h"
int main(void){
 struct upload_window w;uint32_t off;uint8_t bits[16]={0};
 assert(sizeof(w)<2048);
 assert(!upload_window_init(&w,70001,0,64));
 for(unsigned i=0;i<16;i++){
  assert(upload_window_pick(&w,100,&off)==1 && off==i*1024);
  assert(!upload_window_sent(&w,off,100));
 }
 assert(upload_window_pick(&w,101,&off)==0);
 bits[0]=0xfe;bits[1]=0xff; /* packet0 lost, packets1..15 received */
 assert(upload_window_ack(&w,0,0,bits,false,200)==1);
 assert(w.durable==0 && w.received==0 && w.acknowledged==15);
 assert(upload_window_pick(&w,1099,&off)==1 && off==16384); /* window growth */
 assert(upload_window_pick(&w,1100,&off)==1 && off==0);
 assert(!upload_window_sent(&w,off,1100) && w.retransmissions==1 && w.cwnd<31);
 memset(bits,0,sizeof(bits));
 assert(upload_window_ack(&w,0,16384,bits,false,1200)==1);
 assert(w.received==16384 && w.durable==0); /* receive ACK is not durable */
 assert(upload_window_ack(&w,16384,16384,bits,false,1250)==1);
 assert(w.durable==16384);
 assert(upload_window_ack(&w,0,0,bits,false,1260)==0 && w.durable==16384);
 bits[0]=1;assert(upload_window_ack(&w,0,16384,bits,false,1260)==-EBADMSG);bits[0]=0;
 assert(upload_window_ack(&w,0,70001,bits,true,1260)==-EBADMSG);
 assert(upload_window_ack(&w,0,w.next+1024,bits,false,1260)==-EBADMSG);
 assert(!upload_window_init(&w,70001,16384,128));
 uint32_t now=0xfffffff0U; /* monotonic milliseconds wrapping */
 while(w.next<w.size){
  uint32_t limit=w.next+8192;if(limit>w.size)limit=w.size;
  while(w.next<limit){assert(upload_window_pick(&w,now,&off)==1);assert(!upload_window_sent(&w,off,now));now++;}
  assert(upload_window_ack(&w,16384,w.next,bits,false,now+50)==1);
  now+=51;
 }
 assert(w.next==70001 && !w.complete && w.durable==16384);
 assert(upload_window_ack(&w,70001,70001,bits,true,now+50)==1 && w.complete);
 assert(upload_window_pick(&w,now+100,&off)==0);
 assert(!upload_window_init(&w,1024,0,16));
 assert(!upload_window_sent(&w,0,0));
 assert(upload_window_pick(&w,999,&off)==0);
 for(unsigned i=1;i<8;i++){
  now=i*10000;assert(upload_window_pick(&w,now,&off)==1 && off==0);assert(!upload_window_sent(&w,off,now));
 }
 assert(upload_window_pick(&w,90000,&off)==-ETIMEDOUT);
 assert(upload_window_init(&w,0,0,64)==-EINVAL);
 assert(upload_window_init(&w,10000,1,64)==-EINVAL);
 assert(upload_window_init(&w,10000,0,129)==-EINVAL);
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            (p / 'window.c').write_text(code)
            subprocess.run(['clang', '-std=c11', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
                            '-I', str(ROOT / 'applications/clip/include'), str(p / 'window.c'),
                            str(ROOT / 'applications/clip/src/upload_window.c'), '-o', str(p / 'window')], check=True)
            subprocess.run([str(p / 'window')], check=True)
