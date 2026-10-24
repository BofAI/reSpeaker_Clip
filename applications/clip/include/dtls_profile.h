/* Optional data-phase wall-time accounting. Counters never contain payloads. */
#ifndef CLIP_DTLS_PROFILE_H
#define CLIP_DTLS_PROFILE_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
enum dtls_profile_metric {
    DP_SOURCE, DP_SD_READ, DP_SD_SEEK, DP_CACHE_HIT,
    DP_TLS_WRITE, DP_TLS_READ, DP_AEAD_ENCRYPT, DP_AEAD_DECRYPT,
    DP_UDP_SEND, DP_UDP_RECV, DP_POLL_READ, DP_POLL_WRITE,
    DP_WINDOW_WAIT, DP_WINDOW_STALL, DP_TX_BLOCKED, DP_RX_EMPTY, DP_METRICS
};
struct dtls_profile_result {
    uint32_t total_us, source_us, sd_read_us, sd_seek_us;
    uint32_t encrypt_us, decrypt_us, udp_send_us, udp_recv_us, tls_other_us;
    uint32_t poll_read_us, poll_write_us, other_us, window_wait_us;
    uint32_t sd_reads, sd_seeks, cache_hits, source_calls;
    uint32_t encrypt_calls, decrypt_calls, tx_calls, rx_calls;
    uint32_t tls_write_calls, tls_read_calls, window_stalls, tx_blocked, rx_empty;
    bool valid, saturated;
};
#ifdef CONFIG_CLIP_DTLS_PROFILE
uint64_t dtls_profile_now(void);
void dtls_profile_begin(void);
void dtls_profile_add(enum dtls_profile_metric metric, uint64_t ticks);
void dtls_profile_window(bool full);
void dtls_profile_poll(bool writing, uint64_t ticks);
void dtls_profile_end(struct dtls_profile_result *out);
int dtls_profile_format(const struct dtls_profile_result *out, char *response, size_t size);
#else
static inline uint64_t dtls_profile_now(void) { return 0; }
static inline void dtls_profile_add(enum dtls_profile_metric m, uint64_t t) { (void)m; (void)t; }
static inline void dtls_profile_window(bool full) { (void)full; }
static inline void dtls_profile_poll(bool writing, uint64_t t) { (void)writing; (void)t; }
#endif
#endif
