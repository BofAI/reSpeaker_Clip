/* Own the raw nonblocking TCP socket and mbedTLS context in the upload thread.
 * The SDK TLS socket's connect blocks even with O_NONBLOCK, and shutdown takes
 * the same fd lock. No second thread may close or manipulate this stream. */
#include "https_stream.h"
#include <zephyr/kernel.h>
#include <zephyr/sys/fdtable.h>
#include <mbedtls/ssl.h>
#include <mbedtls/x509_crt.h>
#include <mbedtls/net_sockets.h>
#include <psa/crypto.h>
#include <errno.h>
#include "upload_perf.h"

#define WAIT_SLICE_MS 50
#define IO_TIMEOUT_MS 15000

struct https_stream {
    int socket;
    bool tls_started;
    int (*check)(void);
    mbedtls_ssl_context ssl;
    mbedtls_ssl_config config;
    mbedtls_x509_crt ca;
};

static int random_bytes(void *context, unsigned char *data, size_t length)
{
    (void)context;
    return psa_generate_random(data, length) == PSA_SUCCESS ? 0 : MBEDTLS_ERR_SSL_INTERNAL_ERROR;
}

static int send_bytes(void *context, const unsigned char *data, size_t length)
{
    struct https_stream *s = context;
    ssize_t n = zsock_send(s->socket, data, length, ZSOCK_MSG_DONTWAIT);
    if (n >= 0) { return n ? (int)n : MBEDTLS_ERR_NET_CONN_RESET; }
    return errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR ?
           MBEDTLS_ERR_SSL_WANT_WRITE : MBEDTLS_ERR_NET_SEND_FAILED;
}

static int receive_bytes(void *context, unsigned char *data, size_t length)
{
    struct https_stream *s = context;
    ssize_t n = zsock_recv(s->socket, data, length, ZSOCK_MSG_DONTWAIT);
    if (n >= 0) { return (int)n; }
    return errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR ?
           MBEDTLS_ERR_SSL_WANT_READ : MBEDTLS_ERR_NET_RECV_FAILED;
}

static int wait_ready(struct https_stream *s, int want, int64_t end)
{
    for (;;) {
        int ret = s->check();
        if (ret) { return ret; }
        int64_t remaining = end - k_uptime_get();
        if (remaining <= 0) { return -ETIMEDOUT; }
        struct zsock_pollfd pfd = {
            .fd = s->socket,
            .events = want == MBEDTLS_ERR_SSL_WANT_READ ? ZSOCK_POLLIN : ZSOCK_POLLOUT,
        };
        uint64_t measured = upload_perf_now();
        ret = zsock_poll(&pfd, 1, (int)MIN(remaining, WAIT_SLICE_MS));
        upload_perf_add(want == MBEDTLS_ERR_SSL_WANT_READ ? UP_WAIT_READ_US : UP_WAIT_WRITE_US,
                        upload_perf_now() - measured);
        if (ret < 0) {
            if (errno == EINTR) { continue; }
            return -errno;
        }
        if (pfd.revents & ZSOCK_POLLNVAL) { return -EBADF; }
        /* Let connect's SO_ERROR / TLS read or write classify error and EOF. */
        if (ret) { return s->check(); }
    }
}

void https_stream_close(struct https_stream *s)
{
    if (!s) { return; }
    /* A best-effort alert uses nonblocking BIO callbacks; never wait for peer. */
    if (s->tls_started) { (void)mbedtls_ssl_close_notify(&s->ssl); }
    if (s->socket >= 0) { (void)zsock_close(s->socket); }
    mbedtls_ssl_free(&s->ssl);
    mbedtls_ssl_config_free(&s->config);
    mbedtls_x509_crt_free(&s->ca);
    k_free(s);
}

int https_stream_open(struct https_stream **out, const struct sockaddr *address,
                      socklen_t address_length, const char *hostname,
                      const unsigned char *ca, size_t ca_length, int (*check)(void))
{
    *out = NULL;
    int ret = check();
    if (ret) { return ret; }
    struct https_stream *s = k_calloc(1, sizeof(*s));
    if (!s) { return -ENOMEM; }
    s->socket = -1;
    s->check = check;
    mbedtls_ssl_init(&s->ssl);
    mbedtls_ssl_config_init(&s->config);
    mbedtls_x509_crt_init(&s->ca);
    ret = -EIO;
    if (psa_crypto_init() != PSA_SUCCESS) { goto fail; }
    if (mbedtls_x509_crt_parse(&s->ca, ca, ca_length)) { ret = -EACCES; goto fail; }
    if (mbedtls_ssl_config_defaults(&s->config, MBEDTLS_SSL_IS_CLIENT,
                                    MBEDTLS_SSL_TRANSPORT_STREAM,
                                    MBEDTLS_SSL_PRESET_DEFAULT)) { ret = -ENOMEM; goto fail; }
    mbedtls_ssl_conf_authmode(&s->config, MBEDTLS_SSL_VERIFY_REQUIRED);
    mbedtls_ssl_conf_ca_chain(&s->config, &s->ca, NULL);
    mbedtls_ssl_conf_rng(&s->config, random_bytes, NULL);
#if defined(MBEDTLS_SSL_MAX_FRAGMENT_LENGTH)
    if (mbedtls_ssl_conf_max_frag_len(&s->config, MBEDTLS_SSL_MAX_FRAG_LEN_4096)) {
        ret = -EINVAL; goto fail;
    }
#endif
    if (mbedtls_ssl_setup(&s->ssl, &s->config)) { ret = -ENOMEM; goto fail; }
    if (mbedtls_ssl_set_hostname(&s->ssl, hostname)) { ret = -ENOMEM; goto fail; }
    s->socket = zsock_socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s->socket < 0) { ret = -errno; goto fail; }
    int flags = zsock_fcntl(s->socket, ZVFS_F_GETFL, 0);
    if (flags < 0 || zsock_fcntl(s->socket, ZVFS_F_SETFL, flags | ZVFS_O_NONBLOCK) < 0) {
        ret = -errno; goto fail;
    }
    int64_t end = k_uptime_get() + CONFIG_NET_SOCKETS_CONNECT_TIMEOUT;
    ret = zsock_connect(s->socket, address, address_length);
    if (ret < 0) {
        if (errno != EINPROGRESS) { ret = -errno; goto fail; }
        ret = wait_ready(s, MBEDTLS_ERR_SSL_WANT_WRITE, end);
        if (ret) { goto fail; }
        int error = 0;
        socklen_t size = sizeof(error);
        if (zsock_getsockopt(s->socket, SOL_SOCKET, SO_ERROR, &error, &size) < 0) {
            ret = -errno; goto fail;
        }
        if (error) { ret = -error; goto fail; }
    }
    mbedtls_ssl_set_bio(&s->ssl, s, send_bytes, receive_bytes, NULL);
    s->tls_started = true;
    end = k_uptime_get() + CONFIG_NET_SOCKETS_TLS_CONNECT_TIMEOUT;
    for (;;) {
        ret = check();
        if (ret) { goto fail; }
        uint64_t measured = upload_perf_now();
        ret = mbedtls_ssl_handshake(&s->ssl);
        upload_perf_add(UP_HANDSHAKE_US, upload_perf_now() - measured);
        if (!ret) { break; }
        if (ret != MBEDTLS_ERR_SSL_WANT_READ && ret != MBEDTLS_ERR_SSL_WANT_WRITE) {
            ret = -EACCES; goto fail;
        }
        ret = wait_ready(s, ret, end);
        if (ret) { goto fail; }
    }
    if (mbedtls_ssl_get_verify_result(&s->ssl)) { ret = -EACCES; goto fail; }
    ret = check();
    if (ret) { goto fail; }
    *out = s;
    return 0;
fail:
    https_stream_close(s);
    return ret;
}

int https_stream_write(struct https_stream *s, const void *data, size_t length)
{
    const unsigned char *cursor = data;
    int64_t end = k_uptime_get() + IO_TIMEOUT_MS;
    while (length) {
        int ret = s->check();
        if (ret) { return ret; }
        uint64_t measured = upload_perf_now();
        ret = mbedtls_ssl_write(&s->ssl, cursor, length);
        upload_perf_add(UP_SSL_WRITE_US, upload_perf_now() - measured);
        upload_perf_add(UP_WRITE_CALLS, 1);
        if (ret > 0) { cursor += ret; length -= ret; continue; }
        if (ret != MBEDTLS_ERR_SSL_WANT_READ && ret != MBEDTLS_ERR_SSL_WANT_WRITE) {
            return -EIO;
        }
        upload_perf_add(ret == MBEDTLS_ERR_SSL_WANT_READ ? UP_WANT_READ : UP_WANT_WRITE, 1);
        ret = wait_ready(s, ret, end);
        if (ret) { return ret; }
    }
    return 0;
}

ssize_t https_stream_read(struct https_stream *s, void *data, size_t length)
{
    int64_t end = k_uptime_get() + IO_TIMEOUT_MS;
    for (;;) {
        int ret = s->check();
        if (ret) { return ret; }
        uint64_t measured = upload_perf_now();
        ret = mbedtls_ssl_read(&s->ssl, data, length);
        upload_perf_add(UP_SSL_READ_US, upload_perf_now() - measured);
        upload_perf_add(UP_READ_CALLS, 1);
        if (ret == MBEDTLS_ERR_SSL_PEER_CLOSE_NOTIFY) { return 0; }
        if (ret >= 0) { return ret; }
        if (ret != MBEDTLS_ERR_SSL_WANT_READ && ret != MBEDTLS_ERR_SSL_WANT_WRITE) {
            return -EIO;
        }
        upload_perf_add(ret == MBEDTLS_ERR_SSL_WANT_READ ? UP_WANT_READ : UP_WANT_WRITE, 1);
        ret = wait_ready(s, ret, end);
        if (ret) { return ret; }
    }
}
