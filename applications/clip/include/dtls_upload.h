/* Persistent encrypted datagram connection; one owner, bounded cancel waits. */
#ifndef CLIP_DTLS_UPLOAD_H
#define CLIP_DTLS_UPLOAD_H
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
struct dtls_upload;
struct dtls_upload_identity {
    const char *device, *session, *credential;
    uint32_t segment, size;
    uint8_t sha256[32];
};
struct dtls_upload_stats {
    uint32_t elapsed_ms, sent_packets, retransmissions, receipts;
    uint32_t received, durable, resume_from, rtt_ms, rto_ms, window_packets;
};
typedef int (*dtls_upload_read_fn)(uint32_t offset, uint8_t *out, size_t length);
typedef void (*dtls_upload_progress_fn)(uint32_t sent, uint32_t received,
                                      uint32_t durable, uint32_t resumed);
int dtls_upload_open(struct dtls_upload **out, const char *host, const char *port,
                      const unsigned char *ca, size_t ca_size, int (*check)(void));
int dtls_upload_file(struct dtls_upload *connection, const struct dtls_upload_identity *identity,
                      dtls_upload_read_fn read_at, dtls_upload_progress_fn progress,
                      struct dtls_upload_stats *stats);
void dtls_upload_close(struct dtls_upload *connection);
#endif
