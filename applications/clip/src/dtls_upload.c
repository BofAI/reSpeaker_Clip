/* DTLS recording transfer v3. Durability is an application-layer promise. */
#include "dtls_upload.h"
#include "upload_window.h"
#include "upload_perf.h"
#include <zephyr/kernel.h>
#include <zephyr/net/socket.h>
#include <zephyr/sys/byteorder.h>
#include <mbedtls/ssl.h>
#include <mbedtls/net_sockets.h>
#include <mbedtls/x509_crt.h>
#include <psa/crypto.h>
#include <errno.h>
#include <string.h>
#define HEADER 24U
#define RECEIPT 57U
#define MAX_FILE (8U * 1024U * 1024U)
enum { OPEN = 1, DATA, QUERY, ACK, FINISH, COMPLETE, ERROR };
struct dtls_upload {
    int fd;
    int (*check)(void);
    mbedtls_ssl_context ssl;
    mbedtls_ssl_config config;
    mbedtls_x509_crt ca;
    int64_t timer_start;
    uint32_t intermediate, final;
    uint8_t id[8], wire[HEADER + UPLOAD_PACKET_BYTES];
    struct upload_window window;
#ifdef CONFIG_CLIP_DTLS_PROFILE
    uint16_t initial_window, window_ceiling;
#endif
};
#ifdef CONFIG_CLIP_DTLS_PROFILE
int dtls_upload_set_window(struct dtls_upload *s, unsigned int initial, unsigned int ceiling)
{
    if (!s || initial < 16 || initial > ceiling || ceiling > UPLOAD_WINDOW_MAX) { return -EINVAL; }
    s->initial_window = initial; s->window_ceiling = ceiling;
    return 0;
}
#endif
static int random_bytes(void *unused, unsigned char *out, size_t length)
{
    ARG_UNUSED(unused);
    return psa_generate_random(out, length) == PSA_SUCCESS ? 0 : MBEDTLS_ERR_SSL_INTERNAL_ERROR;
}
static int transmit(void *context, const unsigned char *data, size_t length)
{
    struct dtls_upload *s = context;
    uint64_t measured = dtls_profile_now();
    int ret = zsock_send(s->fd, data, length, ZSOCK_MSG_DONTWAIT);
    int error = errno;
    dtls_profile_add(DP_UDP_SEND, dtls_profile_now() - measured);
    if (ret >= 0) { return ret; }
    if (error == EAGAIN || error == EWOULDBLOCK) { dtls_profile_add(DP_TX_BLOCKED, 0); }
    return error == EAGAIN || error == EWOULDBLOCK || error == EINTR ?
        MBEDTLS_ERR_SSL_WANT_WRITE : MBEDTLS_ERR_NET_SEND_FAILED;
}
static int receive(void *context, unsigned char *data, size_t length)
{
    struct dtls_upload *s = context;
    uint64_t measured = dtls_profile_now();
    int ret = zsock_recv(s->fd, data, length, ZSOCK_MSG_DONTWAIT);
    int error = errno;
    dtls_profile_add(DP_UDP_RECV, dtls_profile_now() - measured);
    if (ret >= 0) { return ret; }
    if (error == EAGAIN || error == EWOULDBLOCK) { dtls_profile_add(DP_RX_EMPTY, 0); }
    return error == EAGAIN || error == EWOULDBLOCK || error == EINTR ?
        MBEDTLS_ERR_SSL_WANT_READ : MBEDTLS_ERR_NET_RECV_FAILED;
}
static void set_timer(void *context, uint32_t intermediate, uint32_t final)
{
    struct dtls_upload *s = context;
    s->timer_start = k_uptime_get(); s->intermediate = intermediate; s->final = final;
}
static int get_timer(void *context)
{
    struct dtls_upload *s = context;
    if (!s->final) { return -1; }
    int64_t elapsed = k_uptime_get() - s->timer_start;
    return elapsed >= s->final ? 2 : elapsed >= s->intermediate ? 1 : 0;
}
static int wait_for(struct dtls_upload *s, int want, int64_t deadline)
{
    int ret = s->check(); if (ret) { return ret; }
    int64_t left = deadline - k_uptime_get();
    if (left <= 0) { return -ETIMEDOUT; }
    struct zsock_pollfd poll = {.fd = s->fd,
        .events = want == MBEDTLS_ERR_SSL_WANT_WRITE ? ZSOCK_POLLOUT : ZSOCK_POLLIN};
    uint64_t measured = dtls_profile_now();
    ret = zsock_poll(&poll, 1, (int)MIN(left, 50));
    int error = errno;
    dtls_profile_poll(want == MBEDTLS_ERR_SSL_WANT_WRITE, dtls_profile_now() - measured);
    if (ret < 0 && error != EINTR) { return -error; }
    if (poll.revents & ZSOCK_POLLNVAL) { return -EBADF; }
    return s->check();
}
void dtls_upload_close(struct dtls_upload *s)
{
    if (!s) { return; }
    if (s->fd >= 0) { (void)mbedtls_ssl_close_notify(&s->ssl); zsock_close(s->fd); }
    mbedtls_ssl_free(&s->ssl); mbedtls_ssl_config_free(&s->config); mbedtls_x509_crt_free(&s->ca);
    k_free(s);
}
int dtls_upload_open(struct dtls_upload **out, const char *host, const char *port,
                      const unsigned char *ca, size_t ca_size, int (*check)(void))
{
    *out = NULL;
    int ret = check(); if (ret) { return ret; }
    if (psa_crypto_init() != PSA_SUCCESS) { return -EIO; }
    struct dtls_upload *s = k_calloc(1, sizeof(*s));
    if (!s) { return -ENOMEM; }
    s->fd = -1; s->check = check;
#ifdef CONFIG_CLIP_DTLS_PROFILE
    dtls_upload_set_window(s, CONFIG_CLIP_DTLS_INITIAL_WINDOW_PACKETS, CONFIG_CLIP_DTLS_WINDOW_PACKETS);
#endif
    mbedtls_ssl_init(&s->ssl); mbedtls_ssl_config_init(&s->config); mbedtls_x509_crt_init(&s->ca);
    struct zsock_addrinfo hints = {.ai_family = AF_INET, .ai_socktype = SOCK_DGRAM}, *address = NULL;
    if (zsock_getaddrinfo(host, port, &hints, &address)) { ret = -EHOSTUNREACH; }
    else {
        ret = check();
        if (!ret) {
            s->fd = zsock_socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
            ret = s->fd < 0 ? -errno : 0;
        }
        if (!ret && zsock_connect(s->fd, address->ai_addr, address->ai_addrlen)) { ret = -errno; }
        zsock_freeaddrinfo(address);
    }
    if (!ret && mbedtls_x509_crt_parse(&s->ca, ca, ca_size)) { ret = -EACCES; }
    if (!ret && mbedtls_ssl_config_defaults(&s->config, MBEDTLS_SSL_IS_CLIENT,
                                           MBEDTLS_SSL_TRANSPORT_DATAGRAM, MBEDTLS_SSL_PRESET_DEFAULT)) { ret = -ENOMEM; }
    if (!ret) {
        mbedtls_ssl_conf_authmode(&s->config, MBEDTLS_SSL_VERIFY_REQUIRED);
        mbedtls_ssl_conf_ca_chain(&s->config, &s->ca, NULL);
        mbedtls_ssl_conf_rng(&s->config, random_bytes, NULL);
        mbedtls_ssl_conf_handshake_timeout(&s->config, 1000, 8000);
        if (mbedtls_ssl_setup(&s->ssl, &s->config) || mbedtls_ssl_set_hostname(&s->ssl, host)) { ret = -ENOMEM; }
    }
    if (!ret) {
        mbedtls_ssl_set_mtu(&s->ssl, 1200);
        mbedtls_ssl_set_timer_cb(&s->ssl, s, set_timer, get_timer);
        mbedtls_ssl_set_bio(&s->ssl, s, transmit, receive, NULL);
        int64_t deadline = k_uptime_get() + 15000;
        for (;;) {
            ret = check(); if (ret) { break; }
            ret = mbedtls_ssl_handshake(&s->ssl);
            if (!ret) { ret = mbedtls_ssl_get_verify_result(&s->ssl) ? -EACCES : 0; break; }
            if (ret != MBEDTLS_ERR_SSL_WANT_READ && ret != MBEDTLS_ERR_SSL_WANT_WRITE) {
                ret = ret == MBEDTLS_ERR_SSL_TIMEOUT ? -ETIMEDOUT : -EACCES; break;
            }
            ret = wait_for(s, ret, deadline); if (ret) { break; }
        }
    }
    if (ret) { dtls_upload_close(s); return ret; }
    *out = s; return 0;
}
static void header(struct dtls_upload *s, uint8_t *packet, uint8_t kind, uint32_t offset, uint32_t length)
{
    memset(packet, 0, HEADER); memcpy(packet, "CU01", 4); packet[4] = kind;
    sys_put_be32(offset, packet + 8); sys_put_be32(length, packet + 12);
    memcpy(packet + 16, s->id, sizeof(s->id));
}
static int send_packet(struct dtls_upload *s, const uint8_t *packet, size_t length)
{
    int64_t deadline = k_uptime_get() + 3000;
    for (;;) {
        int ret = s->check(); if (ret) { return ret; }
        uint64_t measured = dtls_profile_now();
        ret = mbedtls_ssl_write(&s->ssl, packet, length);
        dtls_profile_add(DP_TLS_WRITE, dtls_profile_now() - measured);
        if (ret == (int)length) { return 0; }
        if (ret != MBEDTLS_ERR_SSL_WANT_READ && ret != MBEDTLS_ERR_SSL_WANT_WRITE) { return -EIO; }
        ret = wait_for(s, ret, deadline); if (ret) { return ret; }
    }
}
static int read_packet(struct dtls_upload *s, int64_t deadline)
{
    for (;;) {
        int ret = s->check(); if (ret) { return ret; }
        uint64_t measured = dtls_profile_now();
        ret = mbedtls_ssl_read(&s->ssl, s->wire, sizeof(s->wire));
        dtls_profile_add(DP_TLS_READ, dtls_profile_now() - measured);
        if (ret > 0) {
            if (ret < (int)HEADER || memcmp(s->wire, "CU01", 4) || s->wire[5] || s->wire[6] || s->wire[7] ||
                sys_get_be32(s->wire + 12) != (uint32_t)ret - HEADER) { return -EBADMSG; }
            if (!memcmp(s->wire + 16, s->id, sizeof(s->id))) { return ret; }
            if (k_uptime_get() >= deadline) { return -ETIMEDOUT; }
            continue;
        }
        if (!ret || ret == MBEDTLS_ERR_SSL_PEER_CLOSE_NOTIFY) { return -ECONNRESET; }
        if (ret != MBEDTLS_ERR_SSL_WANT_READ && ret != MBEDTLS_ERR_SSL_WANT_WRITE) { return -EIO; }
        ret = wait_for(s, ret, deadline); if (ret) { return ret; }
    }
}
static int accept_packet(struct dtls_upload *s, const struct dtls_upload_identity *id, int n)
{
    uint8_t kind = s->wire[4];
    if (kind == ERROR && n == (int)HEADER + 2) {
        uint16_t code = sys_get_be16(s->wire + HEADER);
        return code == 401 || code == 403 ? -EACCES : code == 409 || code == 422 ? -EBADMSG :
               code == 400 ? -EINVAL : code == 507 ? -ENOSPC : -EIO;
    }
    if ((kind != ACK && kind != COMPLETE) || n != (int)(HEADER + RECEIPT)) { return -EBADMSG; }
    const uint8_t *body = s->wire + HEADER;
    if (sys_get_be32(body) != id->size || memcmp(body + 4, id->sha256, 32) ||
        body[56] > 1 || (kind == COMPLETE) != (body[56] == 1)) { return -EBADMSG; }
    return upload_window_ack(&s->window, sys_get_be32(s->wire + 8), sys_get_be32(body + 36),
                              body + 40, body[56] == 1, (uint32_t)k_uptime_get());
}
int dtls_upload_file(struct dtls_upload *s, const struct dtls_upload_identity *id,
                      dtls_upload_read_fn read_at, dtls_upload_progress_fn progress,
                      struct dtls_upload_stats *stats)
{
    memset(stats, 0, sizeof(*stats));
    if (!s || !id || !read_at || !progress || !id->size || id->size > MAX_FILE ||
        !strlen(id->device) || strlen(id->device) > 32 || strlen(id->session) != 14 ||
        strlen(id->credential) < 32 || strlen(id->credential) > 96) { return -EINVAL; }
    if (psa_generate_random(s->id, sizeof(s->id)) != PSA_SUCCESS) { return -EIO; }
    uint8_t request[256];
    unsigned int name = strlen(id->device), secret = strlen(id->credential), at = HEADER;
    request[at++] = name; memcpy(request + at, id->device, name); at += name;
    memcpy(request + at, id->session, 14); at += 14;
    sys_put_be32(id->segment, request + at); at += 4;
    sys_put_be32(id->size, request + at); at += 4;
    memcpy(request + at, id->sha256, 32); at += 32;
    request[at++] = secret; memcpy(request + at, id->credential, secret); at += secret;
    header(s, request, OPEN, 0, at - HEADER);
    upload_perf_begin(UP_GET, id->session, id->segment, 0, id->size);
    int ret = -ETIMEDOUT;
    /* OPEN has an arbitrary durable starting offset, so initialize after its
     * identity-bound receipt. Delayed OPEN ACKs are valid normal ACKs later. */
    for (unsigned int attempt = 0; attempt < 8; attempt++) {
        ret = send_packet(s, request, at); if (ret) { break; }
        int n = read_packet(s, k_uptime_get() + 1000);
        if (n == -ETIMEDOUT) { ret = n; continue; }
        if (n < 0) { ret = n; break; }
        if ((s->wire[4] == ACK || s->wire[4] == COMPLETE) && n == (int)(HEADER + RECEIPT)) {
            uint32_t durable = sys_get_be32(s->wire + 8);
            unsigned int ceiling = CONFIG_CLIP_DTLS_WINDOW_PACKETS;
            unsigned int initial = CONFIG_CLIP_DTLS_INITIAL_WINDOW_PACKETS;
#ifdef CONFIG_CLIP_DTLS_PROFILE
            ceiling = s->window_ceiling;
            initial = s->initial_window;
#endif
            ret = upload_window_init(&s->window, id->size, durable, ceiling);
            if (!ret) { s->window.cwnd = initial; }
            if (!ret) { ret = accept_packet(s, id, n); }
        } else { ret = accept_packet(s, id, n); }
        if (ret >= 0) { stats->resume_from = s->window.durable; ret = 0; }
        break;
    }
    memset(request, 0, sizeof(request));
    upload_perf_end(ret);
    if (ret) { return ret; }
    upload_perf_begin(UP_PUT, id->session, id->segment, stats->resume_from, id->size);
    progress(s->window.next, s->window.received, s->window.durable, stats->resume_from);
#ifdef CONFIG_CLIP_DTLS_PROFILE
    dtls_profile_begin();
#endif
    int64_t started = k_uptime_get(), last_query = started, last_reply = started;
    while (!ret && !s->window.complete) {
        uint32_t offset;
        int selected = upload_window_pick(&s->window, (uint32_t)k_uptime_get(), &offset);
        if (selected < 0) { ret = selected; break; }
        if (selected) {
            uint32_t length = MIN(UPLOAD_PACKET_BYTES, id->size - offset);
            header(s, s->wire, DATA, offset, length);
            uint64_t measured = dtls_profile_now();
            ret = read_at(offset, s->wire + HEADER, length);
            dtls_profile_add(DP_SOURCE, dtls_profile_now() - measured);
            if (!ret) { ret = send_packet(s, s->wire, HEADER + length); }
            if (!ret) { ret = upload_window_sent(&s->window, offset, (uint32_t)k_uptime_get()); }
            if (ret) { break; }
            stats->sent_packets++;
            progress(s->window.next, s->window.received, s->window.durable, stats->resume_from);
        }
        /* Drain ready receipts between sends. Otherwise wait at most20ms so
         * packet timers, cancellation and pacing do not depend on a peer. */
        int64_t end = k_uptime_get() + (selected ? 0 : 20);
        dtls_profile_window(!selected && s->window.next < s->window.size &&
            s->window.next >= s->window.received + s->window.cwnd * UPLOAD_PACKET_BYTES);
        int n = read_packet(s, end);
        dtls_profile_window(false);
        if (n > 0) {
            ret = accept_packet(s, id, n);
            if (ret >= 0) {
                stats->receipts++; ret = 0; last_reply = k_uptime_get();
                progress(s->window.next, s->window.received, s->window.durable, stats->resume_from);
            }
        } else if (n != -ETIMEDOUT) { ret = n; }
        int64_t now = k_uptime_get();
        if (!ret && !s->window.complete && now - last_query >= 250 && (now - last_reply >= 250 || s->window.received == id->size)) {
            uint8_t query[HEADER];
            bool finish = s->window.received == id->size;
            header(s, query, finish ? FINISH : QUERY, finish ? id->size : 0, 0);
            ret = send_packet(s, query, sizeof(query)); last_query = now;
        }
    }
#ifdef CONFIG_CLIP_DTLS_PROFILE
    dtls_profile_end(&stats->timing);
#endif
    stats->elapsed_ms = k_uptime_get() - started;
    stats->received = s->window.received; stats->durable = s->window.durable;
    stats->retransmissions = s->window.retransmissions;
    stats->rtt_ms = s->window.srtt_ms; stats->rto_ms = s->window.rto_ms;
    stats->window_packets = s->window.cwnd;
    upload_perf_end(ret);
    return ret;
}
