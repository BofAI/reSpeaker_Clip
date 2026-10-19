"""Compile the actual handler against a small Zephyr test double; run with sanitizers."""
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]


class ResourceHandlerTest(unittest.TestCase):
    def test_handler(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = pathlib.Path(directory)
            (temp / "zephyr/sys").mkdir(parents=True)
            (temp / "zephyr/kernel.h").write_text(r'''
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
struct k_thread { struct { size_t size; } stack_info; };
typedef struct k_thread *k_tid_t;
typedef struct { uint64_t total_cycles, execution_cycles; } k_thread_runtime_stats_t;
struct k_heap { int heap, lock; };
typedef int k_spinlock_key_t;
int64_t k_uptime_get(void);
int k_thread_runtime_stats_cpu_get(int, k_thread_runtime_stats_t *);
const char *k_thread_name_get(k_tid_t);
int k_thread_stack_space_get(const struct k_thread *, size_t *);
void k_thread_foreach(void (*)(const struct k_thread *, void *), void *);
int k_spin_lock(int *);
void k_spin_unlock(int *, int);
''')
            (temp / "zephyr/sys/sys_heap.h").write_text(r'''
#include <stddef.h>
struct sys_memory_stats { size_t allocated_bytes, free_bytes, max_allocated_bytes; };
int sys_heap_runtime_stats_get(int *, struct sys_memory_stats *);
''')
            (temp / "test.c").write_text(r'''
#include "at_server.h"
#include "resource_stats.h"
#include <zephyr/sys/sys_heap.h>
#include <assert.h>
#include <string.h>
struct k_heap _system_heap;
static const struct at_command *command;
static int64_t ms;
static k_thread_runtime_stats_t cycles;
static int failure, locked, stack_error;
int at_server_register_cmd(const struct at_command *c) { command=c; return 0; }
int64_t k_uptime_get(void) { return ms; }
int k_thread_runtime_stats_cpu_get(int cpu, k_thread_runtime_stats_t *s) {
    assert(cpu==0); *s=cycles; return failure;
}
int k_spin_lock(int *l) { assert(l==&_system_heap.lock && !locked); locked=1; return 0; }
void k_spin_unlock(int *l, int key) { (void)l; (void)key; assert(locked); locked=0; }
int sys_heap_runtime_stats_get(int *h, struct sys_memory_stats *s) {
    (void)h; assert(locked); *s=(struct sys_memory_stats){100,200,150}; return 0;
}
const char *k_thread_name_get(k_tid_t t) { (void)t; return "a\"\\\n"; }
int k_thread_stack_space_get(const struct k_thread *t, size_t *s) {
    (void)t; *s=256; return stack_error;
}
void k_thread_foreach(void (*f)(const struct k_thread *, void *), void *arg) {
    static struct k_thread t={.stack_info={1024}}; f(&t,arg);
}
static char out[512];
static int run(int type, const char *args, size_t size) {
    struct at_cmd_ctx c={.type=type,.args=args}; return command->handler(&c,out,size);
}
int main(void) {
    assert(resource_stats_register()==0 && !strcmp(command->name,"RES"));
    assert(run(2,0,sizeof(out))>0 && strstr(out,"\"cpu_bp\":-1"));
    ms=500; cycles=(k_thread_runtime_stats_t){10,20};
    run(3,0,sizeof(out)); assert(strstr(out,"\"cpu_bp\":-1"));
    ms=2000; cycles=(k_thread_runtime_stats_t){25,100};
    run(2,0,sizeof(out)); assert(strstr(out,"\"cpu_bp\":2500"));
    ms=4000; cycles=(k_thread_runtime_stats_t){0,0};
    run(2,0,sizeof(out)); assert(strstr(out,"\"cpu_bp\":-1"));
    failure=1; run(2,0,sizeof(out)); assert(strstr(out,"\"ok\":false")); failure=0;
    const char *bad[]={"", "-1", "1x", "256", "999999999999999999999", " 0"};
    for (unsigned i=0;i<sizeof(bad)/sizeof(bad[0]);i++) {
        run(1,bad[i],sizeof(out)); assert(strstr(out,"\"ok\":false"));
    }
    run(1,"1",sizeof(out)); assert(strstr(out,"out of range"));
    run(1,"0",sizeof(out)); assert(strstr(out,"\"stack_peak\":768"));
    assert(strstr(out,"\"name\":\"a___\""));
    stack_error=1; run(1,"0",sizeof(out)); assert(strstr(out,"\"stack_valid\":false"));
    assert(run(1,"0",8)==-AT_ERR_NOMEM && out[0]==0);
    assert(run(2,0,0)==-AT_ERR_NOMEM);
    return 0;
}
''')
            subprocess.run([
                "clang", "-std=c11", "-Wall", "-Wextra", "-Werror",
                "-fsanitize=address,undefined", "-I" + str(temp),
                "-I" + str(ROOT / "applications/clip/include"),
                str(ROOT / "applications/clip/src/resource_stats.c"),
                str(temp / "test.c"), "-o", str(temp / "test"),
            ], check=True)
            subprocess.run([str(temp / "test")], check=True)


if __name__ == "__main__":
    unittest.main()
