#include "upload_window.h"
#include <errno.h>
#include <string.h>
#define MINIMUM(a,b) ((a) < (b) ? (a) : (b))
#define MAXIMUM(a,b) ((a) > (b) ? (a) : (b))
static uint32_t packet_end(const struct upload_window *w, uint32_t off)
{
    return off + MINIMUM(UPLOAD_PACKET_BYTES, w->size - off);
}
int upload_window_init(struct upload_window *w, uint32_t size, uint32_t durable,
                       unsigned int ceiling)
{
    if (!w || !size || size > 8U * 1024U * 1024U || durable > size ||
        (durable != size && durable % UPLOAD_PACKET_BYTES) || ceiling < 16 || ceiling > UPLOAD_WINDOW_MAX) {
        return -EINVAL;
    }
    memset(w, 0, sizeof(*w));
    w->size = size; w->next = w->received = w->durable = durable;
    w->cwnd = 16; w->ceiling = w->threshold = ceiling; w->rto_ms = 1000;
    return 0;
}
int upload_window_pick(struct upload_window *w, uint32_t now, uint32_t *offset)
{
    if (w->complete || (w->pace_ms && (int32_t)(now - w->next_send_ms) < 0)) { return 0; }
    for (unsigned int i = 0; i < UPLOAD_WINDOW_MAX; i++) {
        struct upload_flight *f = &w->flight[i];
        if (!f->active) { continue; }
        unsigned int shift = MINIMUM((unsigned int)f->attempts - 1U, 3U);
        uint32_t retry = MINIMUM(w->rto_ms << shift, 8000U);
        if (now - f->sent_ms >= retry) {
            if (f->attempts >= 8) { return -ETIMEDOUT; }
            *offset = f->offset; return 1;
        }
    }
    if (w->next < w->size && w->next < w->received + w->cwnd * UPLOAD_PACKET_BYTES) {
        struct upload_flight *f = &w->flight[(w->next / UPLOAD_PACKET_BYTES) % UPLOAD_WINDOW_MAX];
        if (f->active) { return -EIO; }
        *offset = w->next; return 1;
    }
    return 0;
}
int upload_window_sent(struct upload_window *w, uint32_t offset, uint32_t now)
{
    if (offset >= w->size || offset % UPLOAD_PACKET_BYTES) { return -EINVAL; }
    struct upload_flight *f = &w->flight[(offset / UPLOAD_PACKET_BYTES) % UPLOAD_WINDOW_MAX];
    if (f->active) {
        if (f->offset != offset || f->attempts >= 8) { return -EINVAL; }
        f->attempts++; w->retransmissions++;
        if (!w->loss_seen || now - w->last_loss_ms >= w->rto_ms) {
            w->threshold = MAXIMUM(4, w->cwnd / 2);
            w->cwnd = w->threshold; w->growth = 0;
            w->pace_ms = MINIMUM(w->pace_ms + 1U, 16U);
            w->last_loss_ms = now; w->loss_seen = true;
        }
    } else {
        if (offset != w->next || offset >= w->received + w->cwnd * UPLOAD_PACKET_BYTES) { return -EINVAL; }
        f->offset = offset; f->active = 1; f->attempts = 1;
        w->next = packet_end(w, offset);
    }
    f->sent_ms = now; w->next_send_ms = now + w->pace_ms;
    return 0;
}
int upload_window_ack(struct upload_window *w, uint32_t durable, uint32_t received,
                      const uint8_t bitmap[16], bool complete, uint32_t now)
{
    if (!bitmap || (bitmap[0] & 1U) || durable > received || received > w->size || received > w->next ||
        (received != w->size && received % UPLOAD_PACKET_BYTES) ||
        (durable != w->size && durable % UPLOAD_PACKET_BYTES) ||
        ((durable == w->size) != complete) || (complete && received != w->size)) { return -EBADMSG; }
    for (unsigned int i = 0; i < UPLOAD_WINDOW_MAX; i++) {
        if ((bitmap[i / 8] & (1U << (i % 8))) &&
            (received == w->size || received + i * UPLOAD_PACKET_BYTES >= w->next)) { return -EBADMSG; }
    }
    if (durable < w->durable || received < w->received) { return 0; }
    unsigned int freed = 0;
    for (unsigned int i = 0; i < UPLOAD_WINDOW_MAX; i++) {
        struct upload_flight *f = &w->flight[i];
        if (!f->active) { continue; }
        bool acked = packet_end(w, f->offset) <= received;
        if (!acked && f->offset >= received) {
            unsigned int bit = (f->offset - received) / UPLOAD_PACKET_BYTES;
            acked = bit < UPLOAD_WINDOW_MAX && (bitmap[bit / 8] & (1U << (bit % 8)));
        }
        if (!acked) { continue; }
        if (f->attempts == 1) {
            uint32_t sample = now - f->sent_ms;
            if (!w->srtt_ms) { w->srtt_ms = MAXIMUM(sample, 1U); w->variance_ms = sample / 2; }
            else {
                uint32_t delta = w->srtt_ms > sample ? w->srtt_ms - sample : sample - w->srtt_ms;
                w->variance_ms = (3 * w->variance_ms + delta) / 4;
                w->srtt_ms = (7 * w->srtt_ms + sample) / 8;
            }
            w->rto_ms = MINIMUM(MAXIMUM(w->srtt_ms + 4 * w->variance_ms, 1000U), 8000U);
        }
        f->active = 0; freed++;
    }
    w->acknowledged += freed;
    if (freed) {
        if (w->cwnd < w->threshold) { w->cwnd = MINIMUM(w->cwnd + freed, w->ceiling); }
        else {
            w->growth += freed;
            if (w->growth >= w->cwnd) {
                w->growth -= w->cwnd;
                if (w->cwnd < w->ceiling) { w->cwnd++; }
                if (w->pace_ms) { w->pace_ms--; }
            }
        }
    }
    w->durable = durable; w->received = received; w->complete = complete;
    return 1;
}
