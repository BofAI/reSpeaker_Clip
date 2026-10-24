/* One upload-worker producer; publish only its completed data-phase snapshot.
 * RTC ticks keep wall-time measurements independent of the CPU boost clock.
 * Nested counters (SD/AEAD/UDP) are reported separately, never added twice. */
#include <zephyr/kernel.h>
#include <zephyr/sys/atomic.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include "dtls_profile.h"
#include "dtls_profile_crypto.h"

static struct {
    uint64_t started, ticks[DP_METRICS];
    uint32_t calls[DP_METRICS];
    bool window_full, overflow;
} sample;
static atomic_ptr_t owner;

static bool measuring(void) { return atomic_ptr_get(&owner) == k_current_get(); }
uint64_t dtls_profile_now(void) { return k_uptime_ticks(); }

void dtls_profile_begin(void)
{
    atomic_ptr_clear(&owner);
    memset(&sample, 0, sizeof(sample));
    sample.started = dtls_profile_now();
    atomic_ptr_set(&owner, k_current_get());
}

void dtls_profile_add(enum dtls_profile_metric metric, uint64_t ticks)
{
    if ((unsigned int)metric >= DP_METRICS || !measuring()) { return; }
    if (UINT64_MAX - sample.ticks[metric] < ticks || sample.calls[metric] == UINT32_MAX) {
        sample.overflow = true; return;
    }
    sample.ticks[metric] += ticks;
    sample.calls[metric]++;
}

void dtls_profile_window(bool full)
{
    if (!measuring()) { return; }
    sample.window_full = full;
    if (full) { dtls_profile_add(DP_WINDOW_STALL, 0); }
}

void dtls_profile_poll(bool writing, uint64_t ticks)
{
    dtls_profile_add(writing ? DP_POLL_WRITE : DP_POLL_READ, ticks);
    if (measuring() && sample.window_full) { dtls_profile_add(DP_WINDOW_WAIT, ticks); }
}

static uint32_t micros(uint64_t ticks, bool *saturated)
{
    /* Check before multiplying in the conversion helper. */
    if (ticks > (uint64_t)UINT32_MAX * CONFIG_SYS_CLOCK_TICKS_PER_SEC / 1000000U) {
        *saturated = true; return UINT32_MAX;
    }
    return (uint32_t)k_ticks_to_us_floor64(ticks);
}

void dtls_profile_end(struct dtls_profile_result *out)
{
    memset(out, 0, sizeof(*out));
    if (!measuring()) { return; }
    uint64_t total = dtls_profile_now() - sample.started;
    atomic_ptr_clear(&owner);
    uint64_t *t = sample.ticks;
    uint64_t tls = t[DP_TLS_WRITE] + t[DP_TLS_READ];
    uint64_t nested = t[DP_AEAD_ENCRYPT] + t[DP_AEAD_DECRYPT] + t[DP_UDP_SEND] + t[DP_UDP_RECV];
    uint64_t polls = t[DP_POLL_READ] + t[DP_POLL_WRITE];
    bool valid = !sample.overflow && nested <= tls &&
        t[DP_SOURCE] + tls + polls <= total &&
        t[DP_SD_READ] + t[DP_SD_SEEK] <= t[DP_SOURCE] && t[DP_WINDOW_WAIT] <= polls;
    out->saturated = sample.overflow;
    out->total_us = micros(total, &out->saturated);
#define TIME(field, kind) out->field = micros(t[kind], &out->saturated)
    TIME(source_us, DP_SOURCE); TIME(sd_read_us, DP_SD_READ); TIME(sd_seek_us, DP_SD_SEEK);
    TIME(encrypt_us, DP_AEAD_ENCRYPT); TIME(decrypt_us, DP_AEAD_DECRYPT);
    TIME(udp_send_us, DP_UDP_SEND); TIME(udp_recv_us, DP_UDP_RECV);
    TIME(poll_read_us, DP_POLL_READ); TIME(poll_write_us, DP_POLL_WRITE);
    TIME(window_wait_us, DP_WINDOW_WAIT);
#undef TIME
    out->tls_other_us = micros(nested <= tls ? tls - nested : 0, &out->saturated);
    uint64_t used = (uint64_t)out->source_us + out->encrypt_us + out->decrypt_us +
        out->udp_send_us + out->udp_recv_us + out->tls_other_us +
        out->poll_read_us + out->poll_write_us;
    out->other_us = used <= out->total_us ? out->total_us - used : 0;
    out->valid = valid && !out->saturated && used <= out->total_us;
#define CALLS(field, kind) out->field = sample.calls[kind]
    CALLS(sd_reads, DP_SD_READ); CALLS(sd_seeks, DP_SD_SEEK); CALLS(cache_hits, DP_CACHE_HIT);
    CALLS(source_calls, DP_SOURCE); CALLS(encrypt_calls, DP_AEAD_ENCRYPT);
    CALLS(decrypt_calls, DP_AEAD_DECRYPT); CALLS(tx_calls, DP_UDP_SEND); CALLS(rx_calls, DP_UDP_RECV);
    CALLS(tls_write_calls, DP_TLS_WRITE); CALLS(tls_read_calls, DP_TLS_READ);
    CALLS(window_stalls, DP_WINDOW_STALL); CALLS(tx_blocked, DP_TX_BLOCKED); CALLS(rx_empty, DP_RX_EMPTY);
#undef CALLS
}

/* The generated ssl_msg.c copy calls these at the actual AEAD call sites.
 * This also works with LTO; link-time --wrap can miss inlined PSA calls. */
psa_status_t clip_profile_aead_encrypt(mbedtls_svc_key_id_t key, psa_algorithm_t alg,
    const uint8_t *nonce, size_t nonce_length, const uint8_t *additional_data,
    size_t additional_data_length, const uint8_t *plaintext, size_t plaintext_length,
    uint8_t *ciphertext, size_t ciphertext_size, size_t *ciphertext_length)
{
    bool enabled = measuring();
    uint64_t start = enabled ? dtls_profile_now() : 0;
    psa_status_t ret = psa_aead_encrypt(key, alg, nonce, nonce_length, additional_data,
        additional_data_length, plaintext, plaintext_length, ciphertext, ciphertext_size, ciphertext_length);
    if (enabled) { dtls_profile_add(DP_AEAD_ENCRYPT, dtls_profile_now() - start); }
    return ret;
}

psa_status_t clip_profile_aead_decrypt(mbedtls_svc_key_id_t key, psa_algorithm_t alg,
    const uint8_t *nonce, size_t nonce_length, const uint8_t *additional_data,
    size_t additional_data_length, const uint8_t *ciphertext, size_t ciphertext_length,
    uint8_t *plaintext, size_t plaintext_size, size_t *plaintext_length)
{
    bool enabled = measuring();
    uint64_t start = enabled ? dtls_profile_now() : 0;
    psa_status_t ret = psa_aead_decrypt(key, alg, nonce, nonce_length, additional_data,
        additional_data_length, ciphertext, ciphertext_length, plaintext, plaintext_size, plaintext_length);
    if (enabled) { dtls_profile_add(DP_AEAD_DECRYPT, dtls_profile_now() - start); }
    return ret;
}

int dtls_profile_format(const struct dtls_profile_result *p, char *out, size_t size)
{
    int n = snprintf(out, size,
        "{\"ok\":true,\"data\":{\"schema\":1,\"scope\":\"dtls_data_phase\",\"tick_hz\":%u,"
        "\"valid\":%s,\"saturated\":%s,\"total_us\":%u,\"source_us\":%u,"
        "\"sd_read_us\":%u,\"sd_seek_us\":%u,\"encrypt_us\":%u,\"decrypt_us\":%u,"
        "\"udp_send_us\":%u,\"udp_recv_us\":%u,\"tls_other_us\":%u,"
        "\"poll_read_us\":%u,\"poll_write_us\":%u,\"other_us\":%u,\"window_wait_us\":%u,"
        "\"sd_reads\":%u,\"sd_seeks\":%u,\"cache_hits\":%u,\"source_calls\":%u,"
        "\"encrypt_calls\":%u,\"decrypt_calls\":%u,\"tx_calls\":%u,\"rx_calls\":%u,"
        "\"tls_write_calls\":%u,\"tls_read_calls\":%u,\"window_stalls\":%u,"
        "\"tx_blocked\":%u,\"rx_empty\":%u}}\n",
        CONFIG_SYS_CLOCK_TICKS_PER_SEC, p->valid ? "true" : "false", p->saturated ? "true" : "false",
        p->total_us, p->source_us, p->sd_read_us, p->sd_seek_us, p->encrypt_us, p->decrypt_us,
        p->udp_send_us, p->udp_recv_us, p->tls_other_us, p->poll_read_us, p->poll_write_us,
        p->other_us, p->window_wait_us, p->sd_reads, p->sd_seeks, p->cache_hits, p->source_calls,
        p->encrypt_calls, p->decrypt_calls, p->tx_calls, p->rx_calls, p->tls_write_calls,
        p->tls_read_calls, p->window_stalls, p->tx_blocked, p->rx_empty);
    if (n < 0 || (size_t)n >= size) { if (size) { out[0] = 0; } return -ENOSPC; }
    return n;
}
