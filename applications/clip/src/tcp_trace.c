/* Opt-in RAM-only observations. No allocation or I/O in the TCP callbacks. */
#include <zephyr/kernel.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "tcp_trace.h"

#define TCP_TRACE_EVENTS 128
struct tcp_trace_ring {
    struct clip_tcp_event events[TCP_TRACE_EVENTS];
    uint32_t sequence;
};
static struct tcp_trace_ring *ring;
static struct k_spinlock trace_lock;

void clip_tcp_trace(const struct clip_tcp_event *event)
{
    uint32_t now = k_uptime_get_32();
    k_spinlock_key_t key = k_spin_lock(&trace_lock);
    if (ring && ring->sequence != UINT32_MAX) {
        uint32_t sequence = ++ring->sequence;
        struct clip_tcp_event *slot = &ring->events[(sequence - 1) % TCP_TRACE_EVENTS];
        *slot = *event;
        slot->ms = now;
        slot->sequence = sequence;
    }
    k_spin_unlock(&trace_lock, key);
}

int clip_tcp_trace_command(const char *args, bool busy, char *out, size_t size)
{
    if (!args) { return -EINVAL; }
    if (!strcmp(args, "on") || !strcmp(args, "off")) {
        if (busy) { return -EBUSY; }
        struct tcp_trace_ring *next = NULL;
        if (!strcmp(args, "on")) {
            next = k_calloc(1, sizeof(*next));
            if (!next) { return -ENOMEM; }
        }
        k_spinlock_key_t key = k_spin_lock(&trace_lock);
        struct tcp_trace_ring *previous = ring;
        ring = next;
        k_spin_unlock(&trace_lock, key);
        k_free(previous);
        return snprintf(out, size, "{\"ok\":true,\"data\":{\"enabled\":%s,\"bytes\":%u}}\n",
                        next ? "true" : "false", next ? (unsigned int)sizeof(*next) : 0);
    }
    if (!*args || strlen(args) > 10) { return -EINVAL; }
    for (const char *p = args; *p; p++) { if (*p < '0' || *p > '9') { return -EINVAL; } }
    unsigned long long cursor = strtoull(args, NULL, 10);
    if (cursor > UINT32_MAX) { return -EINVAL; }
    struct clip_tcp_event e = {0};
    k_spinlock_key_t key = k_spin_lock(&trace_lock);
    bool enabled = ring != NULL;
    uint32_t latest = enabled ? ring->sequence : 0;
    uint32_t oldest = latest > TCP_TRACE_EVENTS ? latest - TCP_TRACE_EVENTS + 1 : latest ? 1 : 0;
    bool found = cursor < latest, lost = oldest && cursor < oldest - 1;
    if (found) { e = ring->events[((lost ? oldest : (uint32_t)cursor + 1) - 1) % TCP_TRACE_EVENTS]; }
    k_spin_unlock(&trace_lock, key);
    int n = snprintf(out, size,
        "{\"ok\":true,\"data\":{\"schema\":1,\"enabled\":%s,\"oldest\":%u,\"latest\":%u,\"lost\":%s,\"event\":",
        enabled ? "true" : "false", oldest, latest, lost ? "true" : "false");
    if (n < 0 || (size_t)n >= size) { return -ENOSPC; }
    int used;
    if (!found) { used = snprintf(out + n, size - n, "null}}\n"); }
    else {
        used = snprintf(out + n, size - n,
            "{\"seq\":%u,\"ms\":%u,\"kind\":%u,\"conn\":%u,\"snd_una\":%u,"
            "\"packet_seq\":%u,\"packet_ack\":%u,\"total\":%u,\"unacked\":%u,\"cwnd\":%u,"
            "\"rto\":%u,\"retry\":%u,\"mode\":%u,\"state\":%u,\"duplicates\":%u,\"value\":%d}}}\n",
            e.sequence, e.ms, e.kind, e.connection, e.seq, e.packet_seq, e.packet_ack,
            e.total, e.unacked, e.cwnd, e.rto, e.retry, e.mode, e.state, e.duplicates, e.value);
    }
    if (used < 0 || (size_t)used >= size - n) { return -ENOSPC; }
    return n + used;
}
