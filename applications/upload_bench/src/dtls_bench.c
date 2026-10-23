/* Diagnostic protocol over standard DTLS. No production checkpoints are written. */
#include <zephyr/kernel.h>
#include <zephyr/net/socket.h>
#include <zephyr/fs/fs.h>
#include <zephyr/sys/byteorder.h>
#include <mbedtls/ssl.h>
#include <mbedtls/net_sockets.h>
#include <mbedtls/x509_crt.h>
#include <mbedtls/sha256.h>
#include <psa/crypto.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include "dtls_bench.h"
#include "upload_perf.h"

#define HEADER 16U
#define PAYLOAD 1024U
#define MAX_FILE (8U * 1024U * 1024U)
enum { START = 1, DATA, QUERY, ACK, FINISH, DONE, SUMMARY, MISSING, PULL, CREDIT };
struct channel {
    int fd;
    bool raw;
    uint8_t tag[7], cookie[32];
    int (*check)(void);
    mbedtls_ssl_context ssl;
    mbedtls_ssl_config config;
    mbedtls_x509_crt ca;
    int64_t timer_start;
    uint32_t intermediate, final;
    struct dtls_bench_result *result;
};
static int random_bytes(void *unused, unsigned char *out, size_t size)
{
    ARG_UNUSED(unused);
    return psa_generate_random(out, size) == PSA_SUCCESS ? 0 : MBEDTLS_ERR_SSL_INTERNAL_ERROR;
}
static int transmit(void *context, const unsigned char *data, size_t size)
{
    struct channel *s = context;
    uint64_t started = upload_perf_now();
    int n = zsock_send(s->fd, data, size, ZSOCK_MSG_DONTWAIT);
    s->result->raw_send_us += (uint32_t)(upload_perf_now() - started);
    if (n >= 0) { return n; }
    return errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR ?
        MBEDTLS_ERR_SSL_WANT_WRITE : MBEDTLS_ERR_NET_SEND_FAILED;
}
static int receive(void *context, unsigned char *data, size_t size)
{
    struct channel *s = context;
    uint64_t started = upload_perf_now();
    int n = zsock_recv(s->fd, data, size, ZSOCK_MSG_DONTWAIT);
    s->result->raw_receive_us += (uint32_t)(upload_perf_now() - started);
    if (n >= 0) { return n; }
    return errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR ?
        MBEDTLS_ERR_SSL_WANT_READ : MBEDTLS_ERR_NET_RECV_FAILED;
}
static void set_timer(void *context, uint32_t intermediate, uint32_t final)
{
    struct channel *s = context;
    s->timer_start = k_uptime_get(); s->intermediate = intermediate; s->final = final;
}
static int get_timer(void *context)
{
    struct channel *s = context;
    if (!s->final) { return -1; }
    int64_t elapsed = k_uptime_get() - s->timer_start;
    return elapsed >= s->final ? 2 : elapsed >= s->intermediate ? 1 : 0;
}
static int wait_slice(struct channel *s, int want, int64_t end)
{
    int ret = s->check();
    if (ret) { return ret; }
    int64_t left = end - k_uptime_get();
    if (left <= 0) { return -ETIMEDOUT; }
    struct zsock_pollfd p = {.fd = s->fd, .events = want == MBEDTLS_ERR_SSL_WANT_WRITE ?
        ZSOCK_POLLOUT : ZSOCK_POLLIN};
    uint64_t started = upload_perf_now();
    ret = zsock_poll(&p, 1, (int)MIN(left, 50));
    s->result->wait_us += (uint32_t)(upload_perf_now() - started);
    if (ret < 0 && errno != EINTR) { return -errno; }
    if (p.revents & ZSOCK_POLLNVAL) { return -EBADF; }
    /* Re-enter DTLS even after a poll timeout so handshake timers can expire. */
    return s->check();
}
static void close_channel(struct channel *s)
{
    if (!s) { return; }
    if (s->fd >= 0) { if (!s->raw) { (void)mbedtls_ssl_close_notify(&s->ssl); } zsock_close(s->fd); }
    mbedtls_ssl_free(&s->ssl); mbedtls_ssl_config_free(&s->config); mbedtls_x509_crt_free(&s->ca);
    k_free(s);
}
static int open_channel(struct channel *s, const char *host, const char *port,
                         const unsigned char *ca, size_t size)
{
    struct zsock_addrinfo hints = {.ai_family = AF_INET, .ai_socktype = SOCK_DGRAM};
    struct zsock_addrinfo *address = NULL;
    if (zsock_getaddrinfo(host, port, &hints, &address)) { return -EHOSTUNREACH; }
    s->fd = zsock_socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    int ret = s->fd < 0 ? -errno : 0;
    if (!ret && zsock_connect(s->fd, address->ai_addr, address->ai_addrlen)) { ret = -errno; }
    zsock_freeaddrinfo(address);
    if (ret || s->raw) { return ret; }
    if (mbedtls_x509_crt_parse(&s->ca, ca, size)) { return -EACCES; }
    ret = mbedtls_ssl_config_defaults(&s->config, MBEDTLS_SSL_IS_CLIENT,
                                      MBEDTLS_SSL_TRANSPORT_DATAGRAM, MBEDTLS_SSL_PRESET_DEFAULT);
    if (ret) { return -ENOMEM; }
    mbedtls_ssl_conf_authmode(&s->config, MBEDTLS_SSL_VERIFY_REQUIRED);
    mbedtls_ssl_conf_ca_chain(&s->config, &s->ca, NULL);
    mbedtls_ssl_conf_rng(&s->config, random_bytes, NULL);
    mbedtls_ssl_conf_handshake_timeout(&s->config, 1000, 4000);
    if (mbedtls_ssl_setup(&s->ssl, &s->config) || mbedtls_ssl_set_hostname(&s->ssl, host)) {
        return -ENOMEM;
    }
    mbedtls_ssl_set_mtu(&s->ssl, 1200);
    mbedtls_ssl_set_timer_cb(&s->ssl, s, set_timer, get_timer);
    mbedtls_ssl_set_bio(&s->ssl, s, transmit, receive, NULL);
    int64_t end = k_uptime_get() + 15000;
    for (;;) {
        ret = s->check(); if (ret) { return ret; }
        ret = mbedtls_ssl_handshake(&s->ssl);
        if (!ret) { break; }
        if (ret != MBEDTLS_ERR_SSL_WANT_READ && ret != MBEDTLS_ERR_SSL_WANT_WRITE) {
            s->result->tls_error = ret;
            return ret == MBEDTLS_ERR_SSL_TIMEOUT ? -ETIMEDOUT : -EACCES;
        }
        ret = wait_slice(s, ret, end); if (ret) { return ret; }
    }
    return mbedtls_ssl_get_verify_result(&s->ssl) ? -EACCES : 0;
}
static void header(uint8_t *out, uint8_t type, uint32_t offset, uint32_t size)
{
    memset(out, 0, HEADER); memcpy(out, "CDB1", 4); out[4] = type;
    sys_put_be32(offset, out + 8); sys_put_be32(size, out + 12);
}
static int send_frame(struct channel *s, uint8_t *packet, size_t size)
{
    int64_t end = k_uptime_get() + 3000;
    for (;;) {
        int ret = s->check(); if (ret) { return ret; }
        uint64_t started = upload_perf_now();
        if (s->raw) {
            memcpy(packet, s->tag, 4); memcpy(packet + 5, s->tag + 4, 3);
            ret = transmit(s, packet, size);
        } else { ret = mbedtls_ssl_write(&s->ssl, packet, size); }
        s->result->write_us += (uint32_t)(upload_perf_now() - started);
        if (ret == (int)size) { return 0; }
        if (ret != MBEDTLS_ERR_SSL_WANT_READ && ret != MBEDTLS_ERR_SSL_WANT_WRITE) {
            s->result->tls_error = ret; return -EIO;
        }
        ret = wait_slice(s, ret, end); if (ret) { return ret; }
    }
}
static int read_frame(struct channel *s, uint8_t *packet, size_t size, int64_t end)
{
    for (;;) {
        int ret = s->check(); if (ret) { return ret; }
        uint64_t started = upload_perf_now();
        if (s->raw) {
            ret = zsock_recv(s->fd, packet, size, ZSOCK_MSG_DONTWAIT | ZSOCK_MSG_TRUNC);
            if (ret < 0) {
                ret = errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR ?
                    MBEDTLS_ERR_SSL_WANT_READ : MBEDTLS_ERR_NET_RECV_FAILED;
            }
        } else { ret = mbedtls_ssl_read(&s->ssl, packet, size); }
        uint32_t elapsed = (uint32_t)(upload_perf_now() - started);
        s->result->read_us += elapsed;
        if (s->raw) { s->result->raw_receive_us += elapsed; }
        if (s->raw && ret > 0) {
            if (ret > (int)size || ret < (int)HEADER || memcmp(packet, s->tag, 4) ||
                memcmp(packet + 5, s->tag + 4, 3)) {
                if (k_uptime_get() >= end) { return -ETIMEDOUT; }
                continue;
            }
            memcpy(packet, "CDB1", 4); memset(packet + 5, 0, 3);
        }
        if (ret > 0) {
            if (ret < (int)HEADER || memcmp(packet, "CDB1", 4) || packet[5] || packet[6] || packet[7] ||
                sys_get_be32(packet + 12) != (uint32_t)ret - HEADER) { return -EBADMSG; }
            return ret;
        }
        if (ret != MBEDTLS_ERR_SSL_WANT_READ && ret != MBEDTLS_ERR_SSL_WANT_WRITE) {
            s->result->tls_error = ret; return -EIO;
        }
        ret = wait_slice(s, ret, end); if (ret) { return ret; }
    }
}
static int control(struct channel *s, uint8_t *request, size_t length, uint8_t type,
                    uint32_t offset, uint8_t *reply, size_t capacity)
{
    for (unsigned int retry = 0; retry < 8; retry++) {
        int ret = send_frame(s, request, length); if (ret) { return ret; }
        int64_t end = k_uptime_get() + 750;
        do {
            ret = read_frame(s, reply, capacity, end);
            if (ret > 0 && reply[4] == type && sys_get_be32(reply + 8) == offset) { return ret; }
        } while (ret > 0);
        if (ret != -ETIMEDOUT) { return ret; }
    }
    return -ETIMEDOUT;
}
/* One file may be in flight without keeping it all in RAM. Repair only missing
 * 1 KiB packets by seeking SD; DTLS protects each new transmission separately. */
static int burst_transfer(struct channel *s, struct fs_file_t *file, uint8_t *batch,
                           size_t capacity, uint8_t *packet, const uint8_t digest[32],
                           void (*notify)(uint32_t sent, uint32_t confirmed))
{
    struct dtls_bench_result *r = s->result;
    uint32_t sent = 0;
    while (sent < r->size) {
        uint32_t length = MIN(capacity, r->size - sent), filled = 0;
        while (filled < length) {
            int ret = s->check(); if (ret) { return ret; }
            uint64_t started = upload_perf_now();
            ssize_t n = fs_read(file, batch + filled, MIN(2048U, length - filled));
            r->fs_read_us += (uint32_t)(upload_perf_now() - started);
            if (n <= 0) { return n < 0 ? (int)n : -EIO; }
            filled += n; notify(sent, 0);
        }
        for (uint32_t offset = 0; offset < length; offset += PAYLOAD) {
            uint32_t size = MIN(PAYLOAD, length - offset);
            header(packet, DATA, sent + offset, size);
            memcpy(packet + HEADER, batch + offset, size);
            int ret = send_frame(s, packet, HEADER + size); if (ret) { return ret; }
            r->packets++; notify(sent + offset + size, 0);
        }
        sent += length;
    }
    uint32_t count = DIV_ROUND_UP(r->size, PAYLOAD), bitmap_bytes = DIV_ROUND_UP(count, 8U);
    for (unsigned int round = 0; round < 8; round++) {
        int length = -ETIMEDOUT;
        /* Lost summary replies retry only the query, not all file data. */
        for (unsigned int retry = 0; retry < 8; retry++) {
            header(packet, SUMMARY, r->size, 0);
            int ret = send_frame(s, packet, HEADER); if (ret) { return ret; }
            int64_t end = k_uptime_get() + 750;
            do {
                length = read_frame(s, batch, HEADER + PAYLOAD, end);
                if (length > 0 && sys_get_be32(batch + 8) == r->size &&
                    (batch[4] == DONE || batch[4] == MISSING)) { break; }
            } while (length > 0);
            if (length != -ETIMEDOUT) { break; }
        }
        if (length < 0) { return length; }
        r->acknowledgments++;
        if (batch[4] == DONE) {
            if (length != (int)(HEADER + 32) || memcmp(batch + HEADER, digest, 32)) { return -EBADMSG; }
            r->confirmed = r->size; notify(r->size, r->size); return 0;
        }
        if (length != (int)(HEADER + bitmap_bytes) ||
            (count % 8 && (batch[HEADER + bitmap_bytes - 1] >> (count % 8)))) { return -EBADMSG; }
        bool any = false;
        for (uint32_t index = 0; index < count; index++) {
            if (!(batch[HEADER + index / 8] & BIT(index % 8))) { continue; }
            any = true;
            int ret = s->check(); if (ret) { return ret; }
            uint32_t offset = index * PAYLOAD, size = MIN(PAYLOAD, r->size - offset), filled = 0;
            ret = fs_seek(file, offset, FS_SEEK_SET); if (ret) { return ret; }
            header(packet, DATA, offset, size);
            while (filled < size) {
                ret = s->check(); if (ret) { return ret; }
                uint64_t started = upload_perf_now();
                ssize_t n = fs_read(file, packet + HEADER + filled, size - filled);
                r->fs_read_us += (uint32_t)(upload_perf_now() - started);
                if (n <= 0) { return n < 0 ? (int)n : -EIO; }
                filled += n;
            }
            ret = send_frame(s, packet, HEADER + size); if (ret) { return ret; }
            r->packets++; r->retransmissions++; notify(r->size, 0);
        }
        if (!any) { return -EBADMSG; }
    }
    return -ETIMEDOUT;
}

static int download_transfer(struct channel *s, uint8_t *batch, size_t capacity,
                              uint8_t *packet, const uint8_t expected[32],
                              void (*notify)(uint32_t sent, uint32_t confirmed))
{
    struct dtls_bench_result *r = s->result;
    uint32_t count = DIV_ROUND_UP(r->size, PAYLOAD), bitmap_bytes = DIV_ROUND_UP(count, 8U);
    if (capacity < bitmap_bytes || capacity < HEADER + PAYLOAD) { return -ENOMEM; }
    memset(batch, 0, capacity);
    char partial[64];
    snprintf(partial, sizeof(partial), "/SD:/BENCH/%s.part", r->run);
    snprintf(r->output, sizeof(r->output), "/SD:/BENCH/%s.bin", r->run);
    int ret = fs_mkdir("/SD:/BENCH");
    if (ret && ret != -EEXIST) { return ret; }
    struct fs_dirent entry;
    ret = fs_stat(partial, &entry); if (ret != -ENOENT) { return ret ? ret : -EEXIST; }
    ret = fs_stat(r->output, &entry); if (ret != -ENOENT) { return ret ? ret : -EEXIST; }
    struct fs_file_t target; fs_file_t_init(&target);
    ret = fs_open(&target, partial, FS_O_CREATE | FS_O_READ | FS_O_WRITE);
    if (ret) { return ret; }
    uint32_t position = 0, controls = 0;
    bool opened = true, started = false;
    /* Zephyr FAT fs_seek rejects positions beyond EOF. Reserve the file
     * before requesting UDP data so reordered packets can be written safely.
     * Chunked zero writes avoid fatfs_truncate's byte-at-a-time expansion. */
    uint64_t allocated = upload_perf_now();
    uint32_t reserved = 0;
    while (!ret && reserved < r->size) {
        ret = s->check(); if (ret) { break; }
        ssize_t size = fs_write(&target, batch, MIN(capacity, r->size - reserved));
        if (size <= 0) { ret = size < 0 ? (int)size : -EIO; break; }
        reserved += size;
        notify(0, 0);
    }
    r->fs_write_us += (uint32_t)(upload_perf_now() - allocated);
    if (!ret) { ret = fs_seek(&target, 0, FS_SEEK_SET); }
    if (!ret) { header(packet, PULL, 0, 0); ret = send_frame(s, packet, HEADER); }
    while (!ret) {
        int n = read_frame(s, packet, HEADER + PAYLOAD, k_uptime_get() + 3000);
        bool ask = n == -ETIMEDOUT;
        if (n < 0 && !ask) { ret = n; break; }
        if (n > 0 && packet[4] == DATA) {
            started = true;
            uint32_t offset = sys_get_be32(packet + 8), length = n - HEADER;
            if (offset >= r->size || offset % PAYLOAD || length != MIN(PAYLOAD, r->size - offset)) {
                ret = -EBADMSG; break;
            }
            r->packets++;
            uint32_t index = offset / PAYLOAD;
            if (batch[index / 8] & BIT(index % 8)) { r->duplicates++; continue; }
            uint64_t measured = upload_perf_now();
            if (offset != position) { ret = fs_seek(&target, offset, FS_SEEK_SET); }
            uint32_t written = 0;
            while (!ret && written < length) {
                ret = s->check(); if (ret) { break; }
                ssize_t size = fs_write(&target, packet + HEADER + written, length - written);
                if (size <= 0) { ret = size < 0 ? (int)size : -EIO; break; }
                written += size;
            }
            r->fs_write_us += (uint32_t)(upload_perf_now() - measured);
            if (ret) { break; }
            position = offset + length;
            batch[index / 8] |= BIT(index % 8); r->received_bytes += length;
            notify(r->received_bytes, 0);
        } else if (n > 0 && packet[4] == SUMMARY) {
            started = true;
            if (n != (int)(HEADER + 8) || sys_get_be32(packet + 8) != r->size) { ret = -EBADMSG; break; }
            r->retransmissions = sys_get_be32(packet + HEADER + 4); ask = true;
        }
        if (r->received_bytes == r->size && ask) { break; }
        if (ask) {
            if (++controls > 12) { ret = -ETIMEDOUT; break; }
            if (!started) { header(packet, PULL, 0, 0); }
            else {
                header(packet, MISSING, r->size, bitmap_bytes);
                for (uint32_t i = 0; i < bitmap_bytes; i++) { packet[HEADER + i] = ~batch[i]; }
                if (count % 8) { packet[HEADER + bitmap_bytes - 1] &= (1U << (count % 8)) - 1; }
                r->repair_requests++;
            }
            ret = send_frame(s, packet, HEADER + sys_get_be32(packet + 12));
            notify(r->received_bytes, 0);
        }
    }
    if (!ret) {
        uint64_t measured = upload_perf_now(); ret = fs_sync(&target);
        r->sync_us = (uint32_t)(upload_perf_now() - measured);
    }
    uint8_t digest[32];
    if (!ret) {
        uint64_t measured = upload_perf_now();
        ret = fs_seek(&target, 0, FS_SEEK_SET);
        mbedtls_sha256_context hash; mbedtls_sha256_init(&hash);
        if (!ret) { ret = mbedtls_sha256_starts(&hash, 0); }
        uint32_t read = 0;
        while (!ret && read < r->size) {
            ret = s->check(); if (ret) { break; }
            ssize_t size = fs_read(&target, batch, MIN(capacity, r->size - read));
            if (size <= 0) { ret = size < 0 ? (int)size : -EIO; break; }
            ret = mbedtls_sha256_update(&hash, batch, size); read += size;
            notify(r->size, 0);
        }
        if (!ret) { ret = mbedtls_sha256_finish(&hash, digest); }
        mbedtls_sha256_free(&hash);
        if (!ret && memcmp(digest, expected, 32)) { ret = -EBADMSG; }
        if (!ret) { for (unsigned int i = 0; i < 32; i++) { snprintf(r->actual_sha256 + i * 2, 3, "%02x", digest[i]); } }
        r->verify_us = (uint32_t)(upload_perf_now() - measured);
    }
    if (!ret) {
        ret = fs_close(&target); opened = ret != 0;
        if (!ret) { ret = fs_rename(partial, r->output); }
        if (!ret) {
            header(packet, DONE, r->size, 32); memcpy(packet + HEADER, digest, 32);
            int size = control(s, packet, HEADER + 32, ACK, r->size, batch, capacity);
            ret = size == (int)(HEADER + 32) && !memcmp(batch + HEADER, digest, 32) ? 0 : size < 0 ? size : -EBADMSG;
            if (!ret) { r->confirmed = r->size; r->acknowledgments++; notify(r->size, r->size); }
        }
    }
    if (opened) { int closed = fs_close(&target); if (!ret) { ret = closed; } }
    return ret;
}

/* Receive ceiling diagnostic: deterministic 4 MiB payload, no SD operations.
 * Authenticate DTLS, check each byte against its absolute offset, and account
 * unique packets before granting sender credit. The bitmap is only 512 bytes. */
static int download_ram(struct channel *s, uint8_t *bitmap, size_t capacity, uint8_t *packet,
                        const uint8_t digest[32], void (*notify)(uint32_t, uint32_t))
{
    struct dtls_bench_result *r = s->result;
    const uint32_t count = r->size / PAYLOAD, bitmap_bytes = DIV_ROUND_UP(count, 8U);
    memset(bitmap, 0, bitmap_bytes);
    strcpy(r->output, "RAM:offset-pattern-validated");
    header(packet, PULL, 0, s->raw ? sizeof(s->cookie) : 0);
    if (s->raw) { memcpy(packet + HEADER, s->cookie, sizeof(s->cookie)); }
    int ret = send_frame(s, packet, HEADER + (s->raw ? sizeof(s->cookie) : 0));
    int64_t progress_at = k_uptime_get(), credit_at = progress_at;
    uint32_t credited = 0;
    while (!ret && r->received_bytes < r->size) {
        int n = read_frame(s, packet, HEADER + PAYLOAD, k_uptime_get() + 100);
        if (n < 0 && n != -ETIMEDOUT) { ret = n; break; }
        if (n > 0 && packet[4] == DATA) {
            uint32_t off = sys_get_be32(packet + 8);
            if (off >= r->size || off % PAYLOAD || n != (int)(HEADER + PAYLOAD)) { ret = -EBADMSG; break; }
            r->packets++;
            uint32_t index = off / PAYLOAD;
            if (bitmap[index / 8] & BIT(index % 8)) { r->duplicates++; }
            else {
                uint64_t verified = upload_perf_now();
                for (uint32_t i = 0; i < PAYLOAD; i++) {
                    if (packet[HEADER + i] != (uint8_t)(off + i)) { ret = -EBADMSG; break; }
                }
                r->verify_us += (uint32_t)(upload_perf_now() - verified);
                if (ret) { break; }
                bitmap[index / 8] |= BIT(index % 8);
                r->received_bytes += PAYLOAD; progress_at = k_uptime_get();
            }
        } else if ((n == (int)(HEADER + 4) ||
                    (s->raw && n == (int)(HEADER + 4 + sizeof(s->cookie)) &&
                     !memcmp(packet + HEADER + 4, s->cookie, sizeof(s->cookie)))) && packet[4] == ACK &&
                   sys_get_be32(packet + 8) == UINT32_MAX && sys_get_be32(packet + HEADER) == 0) {
            /* A retried START can leave a late, authenticated START ACK queued
             * after the first ACK has already allowed PULL. It grants no data
             * credit and must not abort a valid receive run. */
            continue;
        } else if (n > 0 && packet[4] != QUERY) {
            r->rejected_kind = packet[4]; r->rejected_offset = sys_get_be32(packet + 8);
            ret = -EBADMSG; break;
        }
        int64_t now = k_uptime_get();
        if (r->received_bytes - credited >= 32768U || now - credit_at >= 50 || r->received_bytes == r->size) {
            header(packet, CREDIT, r->received_bytes, bitmap_bytes);
            memcpy(packet + HEADER, bitmap, bitmap_bytes);
            ret = send_frame(s, packet, HEADER + bitmap_bytes);
            credited = r->received_bytes; credit_at = now;
            r->acknowledgments++; notify(r->received_bytes, 0);
        }
        if (now - progress_at > 15000) { ret = -ETIMEDOUT; }
    }
    if (!ret) {
        header(packet, DONE, r->size, 32); memcpy(packet + HEADER, digest, 32);
        int n = control(s, packet, HEADER + 32, ACK, r->size, bitmap, capacity);
        ret = n == (int)(HEADER + 32) && !memcmp(bitmap + HEADER, digest, 32) ? 0 : n < 0 ? n : -EBADMSG;
        if (!ret) { r->confirmed = r->size; notify(r->size, r->size); }
    }
    return ret;
}

int dtls_bench_run(const char *path, const char *host, const char *port, const char *token,
                   const unsigned char *ca, size_t ca_size, uint8_t *batch, size_t capacity,
                   unsigned int window, unsigned int download, int (*check)(void),
                   void (*notify)(uint32_t sent, uint32_t confirmed), struct dtls_bench_result *r)
{
    memset(r, 0, sizeof(*r));
    if ((window != 8 && window != 16 && window != 32 && window != 64) || capacity < window * PAYLOAD ||
        strlen(token) < 32 || strlen(token) > 96) { r->error = -EINVAL; return r->error; }
    if (psa_crypto_init() != PSA_SUCCESS) { r->error = -EIO; return r->error; }
    struct fs_file_t file; fs_file_t_init(&file);
    struct fs_dirent entry;
    int ret = download >= 2 ? 0 : fs_stat(path, &entry);
    if (ret || (download < 2 && (entry.type != FS_DIR_ENTRY_FILE || !entry.size || entry.size > MAX_FILE))) {
        r->error = ret ? ret : -EINVAL; return r->error;
    }
    r->size = download >= 2 ? 4U * 1024U * 1024U : entry.size;
    ret = download >= 2 ? 0 : fs_open(&file, path, FS_O_READ);
    if (ret) { r->error = ret; return ret; }
    struct channel *s = k_calloc(1, sizeof(*s));
    if (!s) { if (download < 2) { fs_close(&file); } r->error = -ENOMEM; return r->error; }
    s->fd = -1; s->raw = download == 3; s->check = check; s->result = r;
    mbedtls_ssl_init(&s->ssl); mbedtls_ssl_config_init(&s->config); mbedtls_x509_crt_init(&s->ca);
    uint8_t digest[32], nonce[8], packet[HEADER + PAYLOAD], reply[128];
    mbedtls_sha256_context hash; mbedtls_sha256_init(&hash);
    uint64_t started = upload_perf_now();
    ret = mbedtls_sha256_starts(&hash, 0);
    uint32_t total = 0;
    while (!ret && total < r->size) {
        ret = check(); if (ret) { break; }
        ssize_t n;
        if (download >= 2) {
            n = MIN(2048U, r->size - total);
            for (ssize_t i = 0; i < n; i++) { batch[i] = (uint8_t)(total + i); }
        } else { n = fs_read(&file, batch, MIN(2048U, r->size - total)); }
        if (n <= 0) { ret = n < 0 ? (int)n : -EIO; break; }
        ret = mbedtls_sha256_update(&hash, batch, n); total += n; notify(0, 0);
    }
    if (!ret) { ret = mbedtls_sha256_finish(&hash, digest); }
    mbedtls_sha256_free(&hash);
    if (!ret && download < 2) { ret = fs_seek(&file, 0, FS_SEEK_SET); }
    if (!ret && psa_generate_random(nonce, sizeof(nonce)) != PSA_SUCCESS) { ret = -EIO; }
    if (!ret) { memcpy(s->tag, nonce, sizeof(s->tag)); }
    if (!ret) { for (unsigned int i = 0; i < 8; i++) { snprintf(r->run + i * 2, 3, "%02x", nonce[i]); } }
    r->prepare_us = (uint32_t)(upload_perf_now() - started);
    started = upload_perf_now();
    if (!ret) { ret = open_channel(s, host, port, ca, ca_size); }
    r->handshake_us = (uint32_t)(upload_perf_now() - started);
    const char *wire_token = s->raw ? "clip-udp-diagnostic-not-a-secret-v1" : token;
    if (!ret) {
        header(packet, START, s->raw ? 4 : download >= 2 ? 3 : download ? 2 : window == 64 ? 1 : 0, 44 + strlen(wire_token)); memcpy(packet + HEADER, nonce, 8);
        sys_put_be32(r->size, packet + HEADER + 8); memcpy(packet + HEADER + 12, digest, 32);
        memcpy(packet + HEADER + 44, wire_token, strlen(wire_token));
        ret = control(s, packet, HEADER + 44 + strlen(wire_token), ACK, UINT32_MAX, reply, sizeof(reply));
        if (ret > 0 && s->raw) {
            if (ret != (int)(HEADER + 4 + sizeof(s->cookie)) || sys_get_be32(reply + HEADER) != 0) {
                ret = -EBADMSG;
            } else { memcpy(s->cookie, reply + HEADER + 4, sizeof(s->cookie)); }
        }
        memset(packet, 0, sizeof(packet)); if (ret > 0) { ret = 0; }
    }
    started = upload_perf_now();
    r->write_us = 0; r->wait_us = 0; r->raw_send_us = 0;
    r->read_us = 0; r->raw_receive_us = 0;
    if (!ret && download >= 2) {
        ret = download_ram(s, batch, capacity, packet, digest, notify);
        goto finished;
    }
    if (!ret && download) {
        ret = download_transfer(s, batch, capacity, packet, digest, notify);
        goto finished;
    }
    if (!ret && window == 64) {
        ret = burst_transfer(s, &file, batch, capacity, packet, digest, notify);
        goto finished;
    }
    uint32_t base = 0;
    while (!ret && base < r->size) {
        uint32_t length = MIN(window * PAYLOAD, r->size - base), filled = 0;
        while (filled < length) {
            ret = check(); if (ret) { break; }
            uint64_t read_started = upload_perf_now();
            ssize_t n = fs_read(&file, batch + filled, MIN(2048U, length - filled));
            r->fs_read_us += (uint32_t)(upload_perf_now() - read_started);
            if (n <= 0) { ret = n < 0 ? (int)n : -EIO; break; }
            filled += n; notify(base, base);
        }
        if (ret) { break; }
        unsigned int count = DIV_ROUND_UP(length, PAYLOAD);
        uint32_t all = count == 32 ? UINT32_MAX : (1U << count) - 1, acknowledged = 0;
        for (unsigned int round = 0; round < 8 && acknowledged != all && !ret; round++) {
            for (unsigned int i = 0; i < count; i++) {
                if (acknowledged & BIT(i)) { continue; }
                uint32_t size = MIN(PAYLOAD, length - i * PAYLOAD);
                header(packet, DATA, base + i * PAYLOAD, size);
                memcpy(packet + HEADER, batch + i * PAYLOAD, size);
                ret = send_frame(s, packet, HEADER + size); if (ret) { break; }
                r->packets++; if (round) { r->retransmissions++; }
                notify(base + i * PAYLOAD + size, base);
            }
            if (ret) { break; }
            header(packet, QUERY, base, 4); sys_put_be32(count, packet + HEADER);
            ret = send_frame(s, packet, HEADER + 4); if (ret) { break; }
            int64_t end = k_uptime_get() + 750;
            for (;;) {
                ret = read_frame(s, reply, sizeof(reply), end);
                if (ret == -ETIMEDOUT) { ret = 0; break; }
                if (ret < 0) { break; }
                if (ret == (int)(HEADER + 4) && reply[4] == ACK && sys_get_be32(reply + 8) == base) {
                    uint32_t mask = sys_get_be32(reply + HEADER);
                    if (mask & ~all) { ret = -EBADMSG; break; }
                    acknowledged |= mask; r->acknowledgments++; ret = 0; break;
                }
            }
        }
        if (!ret && acknowledged != all) { ret = -ETIMEDOUT; }
        if (!ret) { base += length; r->confirmed = base; notify(base, base); }
    }
    if (!ret) {
        header(packet, FINISH, r->size, 0);
        ret = control(s, packet, HEADER, DONE, r->size, reply, sizeof(reply));
        if (ret > 0) { ret = ret == (int)(HEADER + 32) && !memcmp(reply + HEADER, digest, 32) ? 0 : -EBADMSG; }
    }
finished:
    r->transfer_us = (uint32_t)(upload_perf_now() - started);
    uint32_t raw_send_us = r->raw_send_us;
    close_channel(s);
    r->raw_send_us = raw_send_us; /* Exclude the best-effort close alert. */
    int closed = download >= 2 ? 0 : fs_close(&file); if (!ret) { ret = closed; }
    r->error = ret; return ret;
}
