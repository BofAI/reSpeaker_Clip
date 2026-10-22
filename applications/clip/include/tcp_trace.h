#ifndef CLIP_TCP_TRACE_H
#define CLIP_TCP_TRACE_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

enum clip_tcp_kind { CLIP_TCP_ARM, CLIP_TCP_TX, CLIP_TCP_RTO, CLIP_TCP_ACK,
                     CLIP_TCP_FAST, CLIP_TCP_RECOVERY };
/* packet_ack contains the length for TX events. value is TX result, ACK payload
 * length, recovered bytes, or the armed delay, depending on kind. */
struct clip_tcp_event {
    uint32_t sequence, ms, kind, connection, seq, packet_seq, packet_ack;
    uint32_t total, unacked, cwnd, rto, retry, mode, state, duplicates;
    int32_t value;
};
void clip_tcp_trace(const struct clip_tcp_event *event);
/* Control caller holds uploader control_lock; reads are allowed while busy. */
int clip_tcp_trace_command(const char *args, bool busy, char *out, size_t size);
#endif
