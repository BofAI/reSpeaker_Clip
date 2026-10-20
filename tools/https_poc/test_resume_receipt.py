"""Compile the actual device receipt gate: remote offsets never bypass identity."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class ReceiptTest(unittest.TestCase):
    def test_offsets_identity_and_terminal_state(self):
        source = (ROOT / "applications/clip/src/https_poc.c").read_text()
        gate = "static int accept_receipt" + source.split("static int accept_receipt", 1)[1].split(
            "static int receive_receipt", 1)[0]
        fixture = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <string.h>
#include <errno.h>
static size_t file_length = 40000;
static char expected_hash[] = "expected";
static int confirmed;
static void atomic_set(int *p, int value) { *p = value; }
'''
        fixture += gate
        fixture += r'''
int main(void) {
 assert(accept_receipt("partial",40000,"expected",16384,-1)==0 && confirmed==16384);
 assert(accept_receipt("partial",40000,"expected",32768,32768)==0 && confirmed==32768);
 struct invalid {const char *status; int size; const char *hash; int offset,expected;} cases[]={
  {"partial",40000,"expected",-1,-1}, {"stored",40000,"expected",40001,-1},
  {"stored",40000,"expected",32768,-1}, {"partial",40000,"expected",40000,-1},
  {"partial",40000,"wrong",16384,-1}, {"partial",39999,"expected",16384,-1},
  {"partial",40000,"expected",32768,16384}, {"partial",40000,"expected",16384,32768},
  {"unknown",40000,"expected",0,-1}, {"stored",-1,"expected",40000,-1}
 };
 for(size_t i=0;i<sizeof(cases)/sizeof(cases[0]);i++) {
  struct invalid *c=&cases[i];
  assert(accept_receipt(c->status,c->size,c->hash,c->offset,c->expected)==-EBADMSG);
  assert(confirmed==32768);
 }
 assert(accept_receipt("stored",40000,"expected",40000,40000)==0 && confirmed==40000);
 assert(accept_receipt("stored",40000,"expected",40000,-1)==0 && confirmed==40000);
 assert(accept_receipt("partial",40000,"expected",0,-1)==0 && confirmed==0);
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test.c").write_text(fixture)
            subprocess.run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror",
                            "-fsanitize=address,undefined", str(root / "test.c"),
                            "-o", str(root / "test")], check=True)
            subprocess.run([str(root / "test")], check=True)
