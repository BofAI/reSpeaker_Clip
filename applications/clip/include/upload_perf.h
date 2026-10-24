/* Optional passive timing. The upload worker is the single event producer. */
#ifndef CLIP_UPLOAD_PERF_H
#define CLIP_UPLOAD_PERF_H
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>

enum upload_perf_kind { UP_SCAN, UP_PREPARE, UP_WIFI, UP_CLOCK, UP_TLS,
                        UP_GET, UP_PUT, UP_CLEANUP };
enum upload_perf_metric { UP_READ_US, UP_HASH_US, UP_HEADER_US, UP_BODY_US,
    UP_RECEIPT_US, UP_SSL_WRITE_US, UP_SSL_READ_US, UP_WAIT_WRITE_US,
    UP_WAIT_READ_US, UP_WRITE_CALLS, UP_READ_CALLS, UP_WANT_WRITE,
    UP_WANT_READ, UP_FS_READS, UP_STATE_US, UP_HANDSHAKE_US, UP_SEEK_US, UP_METRICS };
#ifdef CONFIG_CLIP_HTTPS_PROFILE
uint64_t upload_perf_now(void);
void upload_perf_add(enum upload_perf_metric metric, uint64_t value);
void upload_perf_begin(enum upload_perf_kind kind, const char *session,
                       uint32_t segment, uint32_t offset, uint32_t length);
void upload_perf_end(int result);
/* Control changes require the caller to hold control_lock and verify idle. */
int upload_perf_command(const char *args, bool busy, char *out, size_t size);
#else
static inline uint64_t upload_perf_now(void) { return 0; }
static inline void upload_perf_add(enum upload_perf_metric m, uint64_t v) { (void)m; (void)v; }
static inline void upload_perf_begin(enum upload_perf_kind k, const char *s,
    uint32_t n, uint32_t o, uint32_t l) { (void)k; (void)s; (void)n; (void)o; (void)l; }
static inline void upload_perf_end(int r) { (void)r; }
#endif
#endif
