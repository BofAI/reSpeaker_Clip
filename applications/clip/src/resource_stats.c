/* SPDX-License-Identifier: Apache-2.0 */
#include "at_server.h"
#include "resource_stats.h"
#include <zephyr/sys/sys_heap.h>
#include <stdio.h>
#include <stdarg.h>

extern struct k_heap _system_heap;

/* Accessed only by the serialized AT server. CPU counters are cumulative. */
static k_thread_runtime_stats_t previous;
static int64_t previous_ms;
static bool primed;

static int reply(char *out, size_t size, const char *format, ...)
{
    va_list ap;
    va_start(ap, format);
    int n = vsnprintf(out, size, format, ap);
    va_end(ap);
    if (n < 0 || (size_t)n >= size) {
        if (size) out[0] = '\0';
        return -AT_ERR_NOMEM;
    }
    return n;
}

struct thread_sample {
    unsigned int total, wanted, id, size, free;
    bool found, valid;
    char name[32];
};

static void sample_thread(const struct k_thread *thread, void *arg)
{
    struct thread_sample *s = arg;
    if (s->total++ != s->wanted) return;
    s->found = true;
    s->id = (unsigned int)(uintptr_t)thread;
    s->size = thread->stack_info.size;
    const char *name = k_thread_name_get((k_tid_t)thread);
    size_t i = 0;
    while (name && name[i] && i < sizeof(s->name) - 1) {
        unsigned char c = name[i];
        s->name[i++] = (c >= 32 && c < 127 && c != '"' && c != '\\') ? c : '_';
    }
    s->name[i] = '\0';
    size_t unused;
    s->valid = k_thread_stack_space_get(thread, &unused) == 0 && unused <= s->size;
    if (s->valid) s->free = unused;
}

static int resources(struct at_cmd_ctx *ctx, char *out, size_t size)
{
    if (ctx->type == AT_CMD_TYPE_SET) {
        unsigned int index = 0;
        const char *p = ctx->args;
        if (!p || !*p) goto invalid;
        for (; *p; ++p) {
            if (*p < '0' || *p > '9' || index > 25) goto invalid;
            index = index * 10 + (*p - '0');
            if (index > 255) goto invalid;
        }
        struct thread_sample s = {.wanted = index};
        /* Monitor lock protects thread lifetime; scan only the selected stack. */
        k_thread_foreach(sample_thread, &s);
        if (!s.found) return reply(out, size, "{\"ok\":false,\"msg\":\"thread index out of range\"}\n");
        return reply(out, size,
            "{\"ok\":true,\"data\":{\"index\":%u,\"total\":%u,\"id\":%u,"
            "\"name\":\"%s\",\"stack_valid\":%s,\"stack_size\":%u,"
            "\"stack_free\":%u,\"stack_peak\":%u}}\n",
            index, s.total, s.id, s.name, s.valid ? "true" : "false",
            s.size, s.free, s.valid ? s.size - s.free : 0);
    }
    if (ctx->type != AT_CMD_TYPE_EXEC && ctx->type != AT_CMD_TYPE_READ &&
        ctx->type != AT_CMD_TYPE_TEST) goto invalid;

    k_thread_runtime_stats_t now;
    struct sys_memory_stats heap;
    int64_t ms = k_uptime_get();
    if (k_thread_runtime_stats_cpu_get(0, &now))
        return reply(out, size, "{\"ok\":false,\"msg\":\"CPU stats unavailable\"}\n");
    int cpu_bp = -1;
    unsigned int interval = primed ? (unsigned int)(ms - previous_ms) : 0;
    if (primed && interval >= 1000 && now.execution_cycles > previous.execution_cycles &&
        now.total_cycles >= previous.total_cycles) {
        uint64_t busy = now.total_cycles - previous.total_cycles;
        uint64_t elapsed = now.execution_cycles - previous.execution_cycles;
        if (busy <= elapsed) cpu_bp = (int)(busy * 10000 / elapsed);
    }
    if (!primed || interval >= 1000) {
        previous = now;
        previous_ms = ms;
        primed = true;
    }
    k_spinlock_key_t key = k_spin_lock(&_system_heap.lock);
    int err = sys_heap_runtime_stats_get(&_system_heap.heap, &heap);
    k_spin_unlock(&_system_heap.lock, key);
    if (err) return reply(out, size, "{\"ok\":false,\"msg\":\"heap stats unavailable\"}\n");
    return reply(out, size,
        "{\"ok\":true,\"data\":{\"schema\":1,\"build\":\"runtime-0.1.2\","
        "\"uptime_ms\":%u,\"cpu_bp\":%d,\"interval_ms\":%u,"
        "\"heap_allocated\":%u,\"heap_free\":%u,\"heap_peak\":%u}}\n",
        (unsigned int)ms, cpu_bp, interval, (unsigned int)heap.allocated_bytes,
        (unsigned int)heap.free_bytes, (unsigned int)heap.max_allocated_bytes);
invalid:
    return reply(out, size, "{\"ok\":false,\"msg\":\"expected RES or RES=0..255\"}\n");
}

int resource_stats_register(void)
{
    static const struct at_command command = {
        .name = "RES", .flags = AT_CMD_EXEC | AT_CMD_QUERY | AT_CMD_SET,
        .handler = resources,
    };
    return at_server_register_cmd(&command);
}
