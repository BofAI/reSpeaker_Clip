"""Compile the actual maintenance application with fake UART/SPI flash."""
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]


class FirmwareProtocolTests(unittest.TestCase):
    def test_bounds_lock_crc_and_truncated_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = pathlib.Path(directory)
            for name in ("kernel.h", "device.h", "drivers/flash.h", "drivers/hwinfo.h", "drivers/uart.h", "sys/crc.h"):
                header = temp/"zephyr"/name
                header.parent.mkdir(parents=True, exist_ok=True)
                header.write_text('#include "fake.h"\n')
            (temp/"fake.h").write_text(r'''
#pragma once
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
struct device { int dummy; };
extern struct device fake_device;
#define DT_NODELABEL(x) 0
#define DT_PROP(n,p) 67108864U
#define DEVICE_DT_GET(x) (&fake_device)
#define K_MSEC(x) (x)
#define K_SECONDS(x) ((x)*1000)
struct flash_pages_info { size_t size; };
int64_t k_uptime_get(void);
void k_sleep(int);
int uart_poll_in(const struct device *, uint8_t *);
void uart_poll_out(const struct device *, uint8_t);
int flash_read(const struct device *, unsigned int, void *, size_t);
int flash_write(const struct device *, unsigned int, const void *, size_t);
int flash_erase(const struct device *, unsigned int, size_t);
int flash_get_page_info_by_offs(const struct device *, unsigned int, struct flash_pages_info *);
int hwinfo_get_device_id(uint8_t *, size_t);
uint32_t crc32_ieee(const uint8_t *, size_t);
int device_is_ready(const struct device *);
''')
            code = '#define main maintenance_main\n#include "' + str(ROOT/'applications/flash_maintenance/src/main.c') + '"\n#undef main\n'
            code += r'''
#include <assert.h>
#include <stdlib.h>
struct device fake_device;
static uint8_t *flash_data, incoming[32], outgoing[20000];
static size_t input_pos,input_size,output_size;
static int writes,erases;
static int64_t now;
int64_t k_uptime_get(void) { return now; }
void k_sleep(int n) { now+=n; }
int uart_poll_in(const struct device *d,uint8_t *out) {
    (void)d;if(input_pos==input_size)return -1;*out=incoming[input_pos++];return 0;
}
void uart_poll_out(const struct device *d,uint8_t c) { (void)d;assert(output_size<sizeof(outgoing));outgoing[output_size++]=c; }
int flash_read(const struct device *d,unsigned int off,void *out,size_t n) {
    (void)d;assert(off+n<=FLASH_BYTES);memcpy(out,flash_data+off,n);return 0;
}
int flash_write(const struct device *d,unsigned int off,const void *in,size_t n) {
    (void)d;assert(off>=WRITE_START && off+n<=FLASH_BYTES);memcpy(flash_data+off,in,n);writes++;return 0;
}
int flash_erase(const struct device *d,unsigned int off,size_t n) {
    (void)d;assert(off>=WRITE_START && off+n<=FLASH_BYTES);memset(flash_data+off,255,n);erases++;return 0;
}
int flash_get_page_info_by_offs(const struct device *d,unsigned int off,struct flash_pages_info *p) {
    (void)d;(void)off;p->size=65536;return 0;
}
int hwinfo_get_device_id(uint8_t *id,size_t n) { memset(id,1,n);return (int)n; }
int device_is_ready(const struct device *d) { (void)d;return 1; }
uint32_t crc32_ieee(const uint8_t *p,size_t n) {
    uint32_t crc=~0U;
    for(size_t i=0;i<n;i++){crc^=p[i];for(int bit=0;bit<8;bit++)crc=(crc>>1)^((crc&1)?0xedb88320U:0);}
    return ~crc;
}
static bool run(char *text) { output_size=0;input_pos=0;return command(text); }
int main(void) {
    flash_data=calloc(1,FLASH_BYTES);assert(flash_data);
    assert(!valid_range(WRITE_START,4,true));
    run("WRITE 130000 4 0");assert(!writes && !memcmp(outgoing,"ERR",3));
    run("ERASE 130000 10000");assert(!erases);
    run("READ 7fffff 2");assert(!memcmp(outgoing,"ERR",3));
    run("READ ffffffff 2");assert(!memcmp(outgoing,"ERR",3));
    run("READ 130000 0");assert(!memcmp(outgoing,"ERR",3));
    run("ARM LFS_PATCH_0x130000_0x800000");assert(armed);
    run("ERASE 0 10000");run("ERASE 130001 10000");run("ERASE 130000 20000");assert(!erases);
    run("ERASE 130000 10000");assert(erases==1);
    memcpy(incoming,"test",4);input_size=4;
    run("WRITE 130000 4 0");assert(!writes);
    char request[80];snprintf(request,sizeof(request),"WRITE 130000 4 %08x",crc32_ieee(incoming,4));
    assert(run(request));assert(writes==1 && !memcmp(flash_data+WRITE_START,"test",4));
    assert(output_size==15 && !memcmp(outgoing,"READY\nOK WRITE\n",15));
    run("READ 130000 4");assert(!memcmp(outgoing,"DATA 4 ",7));assert(!memcmp(outgoing+output_size-4,"test",4));
    input_size=2;assert(!run(request));assert(!armed && writes==1);
    free(flash_data);return 0;
}
'''
            (temp/"test.c").write_text(code)
            subprocess.run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror",
                            "-fsanitize=address,undefined", "-I"+str(temp),
                            str(temp/"test.c"), "-o", str(temp/"test")], check=True)
            subprocess.run([str(temp/"test")], check=True)


if __name__ == "__main__": unittest.main()
