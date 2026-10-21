/* No serial/file output on the worker path; a bounded RAM ring is read by USB. */
#include <zephyr/kernel.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "upload_perf.h"

#define PERF_EVENTS 16
struct perf_event {
    uint32_t sequence, kind, segment, offset, length;
    int result;
    uint64_t started, elapsed, session, values[UP_METRICS];
};
struct profiler {
    struct perf_event current, events[PERF_EVENTS];
    uint32_t sequence;
    bool writing;
};
static struct profiler *profiler;
static struct k_spinlock perf_lock;

/* This image's minimal printf does not implement %llu. Keep its global
 * formatting configuration unchanged and convert our 64-bit fields locally. */
static void decimal(uint64_t value, char out[21])
{
    char reverse[20];
    unsigned int count = 0;
    do { reverse[count++] = '0' + value % 10; value /= 10; } while (value);
    for (unsigned int i = 0; i < count; i++) { out[i] = reverse[count - i - 1]; }
    out[count] = 0;
}

uint64_t upload_perf_now(void) { return k_ticks_to_us_floor64(k_uptime_ticks()); }

void upload_perf_add(enum upload_perf_metric metric, uint64_t value)
{
    k_spinlock_key_t key = k_spin_lock(&perf_lock);
    if (profiler && profiler->writing && (unsigned int)metric < UP_METRICS) {
        profiler->current.values[metric] += value;
    }
    k_spin_unlock(&perf_lock, key);
}

void upload_perf_begin(enum upload_perf_kind kind, const char *session,
                       uint32_t segment, uint32_t offset, uint32_t length)
{
    uint64_t started = upload_perf_now();
    uint64_t identity = session ? strtoull(session, NULL, 10) : 0;
    k_spinlock_key_t key = k_spin_lock(&perf_lock);
    if (profiler) {
        memset(&profiler->current, 0, sizeof(profiler->current));
        profiler->current.kind = kind;
        profiler->current.session = identity;
        profiler->current.segment = segment;
        profiler->current.offset = offset;
        profiler->current.length = length;
        profiler->current.started = started;
        profiler->writing = true;
    }
    k_spin_unlock(&perf_lock, key);
}

void upload_perf_end(int result)
{
    uint64_t ended = upload_perf_now();
    k_spinlock_key_t key = k_spin_lock(&perf_lock);
    if (profiler && profiler->writing && profiler->sequence != UINT32_MAX) {
        struct perf_event *event = &profiler->current;
        event->sequence = ++profiler->sequence;
        event->result = result;
        event->elapsed = ended - event->started;
        profiler->events[(event->sequence - 1) % PERF_EVENTS] = *event;
        profiler->writing = false;
    }
    k_spin_unlock(&perf_lock, key);
}

int upload_perf_command(const char *args, bool busy, char *out, size_t size)
{
    if (!args) { return -EINVAL; }
    if (!strcmp(args, "on") || !strcmp(args, "off")) {
        if (busy) { return -EBUSY; }
        struct profiler *next = NULL;
        if (!strcmp(args, "on")) {
            next = k_calloc(1, sizeof(*next));
            if (!next) { return -ENOMEM; }
        }
        k_spinlock_key_t key = k_spin_lock(&perf_lock);
        struct profiler *previous = profiler;
        profiler = next;
        k_spin_unlock(&perf_lock, key);
        k_free(previous);
        return snprintf(out, size, "{\"ok\":true,\"data\":{\"enabled\":%s,\"schema\":1,\"bytes\":%u}}\n",
                        next ? "true" : "false", next ? (unsigned int)sizeof(*next) : 0);
    }
    /* Numeric reads never reset state and are permitted while uploading. */
    if (!*args || strlen(args) > 10) { return -EINVAL; }
    for (const char *p = args; *p; p++) { if (*p < '0' || *p > '9') { return -EINVAL; } }
    unsigned long long cursor = strtoull(args, NULL, 10);
    if (cursor > UINT32_MAX) { return -EINVAL; }
    struct perf_event event = {0};
    k_spinlock_key_t key = k_spin_lock(&perf_lock);
    uint32_t latest = profiler ? profiler->sequence : 0;
    uint32_t oldest = latest > PERF_EVENTS ? latest - PERF_EVENTS + 1 : latest ? 1 : 0;
    bool enabled = profiler != NULL, found = cursor < latest;
    bool lost = oldest && cursor < oldest - 1;
    if (found) { event = profiler->events[((lost ? oldest : (uint32_t)cursor + 1) - 1) % PERF_EVENTS]; }
    k_spin_unlock(&perf_lock, key);
    int n = snprintf(out, size,
        "{\"ok\":true,\"data\":{\"schema\":1,\"enabled\":%s,\"oldest\":%u,\"latest\":%u,\"lost\":%s,\"event\":",
        enabled ? "true" : "false", oldest, latest, lost ? "true" : "false");
    if (n < 0 || (size_t)n >= size) { return -ENOSPC; }
    int used;
    if (!found) { used = snprintf(out + n, size - n, "null}}\n"); }
    else {
        char identity[21], started[21], elapsed[21];
        decimal(event.session, identity); decimal(event.started, started); decimal(event.elapsed, elapsed);
        used = snprintf(out + n, size - n,
            "{\"seq\":%u,\"kind\":%u,\"session\":\"%s\",\"segment\":%u,\"offset\":%u,"
            "\"length\":%u,\"result\":%d,\"started_us\":%s,\"elapsed_us\":%s,\"values\":[",
            event.sequence, event.kind, identity, event.segment,
            event.offset, event.length, event.result, started, elapsed);
        if (used < 0 || (size_t)used >= size - n) { return -ENOSPC; }
        n += used;
        for (unsigned int i = 0; i < UP_METRICS; i++) {
            decimal(event.values[i], elapsed);
            used = snprintf(out + n, size - n, "%s%s", i ? "," : "", elapsed);
            if (used < 0 || (size_t)used >= size - n) { return -ENOSPC; }
            n += used;
        }
        used = snprintf(out + n, size - n, "]}}}\n");
    }
    if (used < 0 || (size_t)used >= size - n) { return -ENOSPC; }
    return n + used;
}
