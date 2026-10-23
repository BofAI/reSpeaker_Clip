#ifndef CLIP_DTLS_BENCH_H
#define CLIP_DTLS_BENCH_H
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
struct dtls_bench_result {
    uint32_t size, confirmed, packets, retransmissions, acknowledgments;
    uint32_t prepare_us, handshake_us, transfer_us, fs_read_us, write_us, wait_us, raw_send_us;
    uint32_t read_us, raw_receive_us, fs_write_us, verify_us, sync_us, received_bytes, repair_requests, duplicates;
    uint32_t rejected_kind, rejected_offset;
    int error, tls_error;
    char run[17];
    char output[64], actual_sha256[65];
};
int dtls_bench_run(const char *path, const char *host, const char *port, const char *token,
                   const unsigned char *ca, size_t ca_size, uint8_t *batch, size_t capacity,
                   unsigned int window, unsigned int download, int (*check)(void),
                   void (*notify)(uint32_t sent, uint32_t confirmed),
                   struct dtls_bench_result *result);
#endif
