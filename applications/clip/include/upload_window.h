/* Portable, bounded selective-ACK sender. Audio bytes remain on SD. */
#ifndef CLIP_UPLOAD_WINDOW_H
#define CLIP_UPLOAD_WINDOW_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#define UPLOAD_PACKET_BYTES 1024U
#define UPLOAD_WINDOW_MAX 128U
struct upload_flight {
    uint32_t offset, sent_ms;
    uint8_t attempts, active;
};
struct upload_window {
    uint32_t size, next, received, durable, rto_ms, srtt_ms, variance_ms;
    uint32_t last_loss_ms, next_send_ms, acknowledged, retransmissions;
    uint16_t cwnd, ceiling, threshold, growth, pace_ms;
    bool complete, loss_seen;
    struct upload_flight flight[UPLOAD_WINDOW_MAX];
};
int upload_window_init(struct upload_window *w, uint32_t size, uint32_t durable,
                       unsigned int ceiling);
int upload_window_pick(struct upload_window *w, uint32_t now, uint32_t *offset);
int upload_window_sent(struct upload_window *w, uint32_t offset, uint32_t now);
int upload_window_ack(struct upload_window *w, uint32_t durable, uint32_t received,
                      const uint8_t bitmap[16], bool complete, uint32_t now);
#endif
