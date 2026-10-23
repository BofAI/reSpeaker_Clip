/* Two authenticated TLS streams multiplexed by the existing upload owner.
 * Each file has independent immutable pending writes, parser and durable state.
 * This is an opt-in diagnostic, not the automatic uploader's scheduler. */
#include "upload_dual.h"
#include "upload_state.h"
#include "storage.h"
#include <zephyr/fs/fs.h>
#include <zephyr/net/http/parser.h>
#include <zephyr/data/json.h>
#include <mbedtls/sha256.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>

enum phase { HEADER, BODY, RECEIPT, DONE };
struct lane {
    struct fs_file_t file;
    struct https_stream *stream;
    struct upload_file_record stamp;
    struct http_parser parser;
    char session[15], hash[65], body[1025];
    uint8_t tx[2048], rx[512];
    uint32_t segment, end, offset;
    size_t tx_size, tx_done, body_size, header_size;
    enum phase phase;
    bool opened, ready, query, complete, keep_alive;
    int status;
    int64_t progressed;
};

static void hex(const uint8_t *digest, char *out)
{
    for (unsigned int i = 0; i < 32; i++) { snprintf(out + 2*i, 3, "%02x", digest[i]); }
}

static int hash_range(struct lane *l, uint32_t offset, uint32_t length,
                      uint8_t digest[32], int (*check)(void), void (*pulse)(void))
{
    int ret = fs_seek(&l->file, offset, FS_SEEK_SET);
    mbedtls_sha256_context sha;
    mbedtls_sha256_init(&sha);
    if (!ret) { ret = mbedtls_sha256_starts(&sha, 0); }
    uint32_t total = 0;
    while (!ret && total < length) {
        ret = check();
        if (ret) { break; }
        ssize_t n = fs_read(&l->file, l->tx, MIN(sizeof(l->tx), length-total));
        if (n <= 0) { ret = n ? n : -EIO; break; }
        ret = mbedtls_sha256_update(&sha, l->tx, n);
        total += n;
        pulse();
    }
    if (!ret) { ret = mbedtls_sha256_finish(&sha, digest); }
    mbedtls_sha256_free(&sha);
    return ret ? ret : fs_seek(&l->file, offset, FS_SEEK_SET);
}

static int prepare(struct lane *l, const char *session, uint32_t segment,
                    const uint8_t destination[32], int (*check)(void), void (*pulse)(void))
{
    strcpy(l->session, session); l->segment = segment;
    char path[STORAGE_PATH_MAX];
    int ret = storage_build_chunk_path(session, segment, path, sizeof(path));
    struct fs_dirent entry;
    if (!ret) { ret = fs_stat(path, &entry); }
    if (!ret && (entry.type != FS_DIR_ENTRY_FILE || !entry.size || entry.size > 8U*1024U*1024U)) {
        ret = -EINVAL;
    }
    fs_file_t_init(&l->file);
    if (!ret) { ret = fs_open(&l->file, path, FS_O_READ); l->opened = !ret; }
    if (!ret) {
        l->stamp.size = entry.size;
        memcpy(l->stamp.destination_sha256, destination, 32);
        ret = hash_range(l, 0, entry.size, l->stamp.content_sha256, check, pulse);
    }
    if (!ret) {
        hex(l->stamp.content_sha256, l->hash);
        ret = upload_state_save(session, segment, &l->stamp);
        l->ready = !ret;
    }
    return ret;
}

static int header(struct http_parser *p, const char *data, size_t length)
{
    ARG_UNUSED(data);
    struct lane *l = p->data;
    if (length > 4096-l->header_size) { return -EMSGSIZE; }
    l->header_size += length;
    return 0;
}
static int body(struct http_parser *p, const char *data, size_t length)
{
    struct lane *l = p->data;
    if (length > 1024-l->body_size) { return -EMSGSIZE; }
    memcpy(l->body+l->body_size, data, length); l->body_size += length;
    return 0;
}
static int complete(struct http_parser *p)
{
    struct lane *l = p->data;
    l->complete = true; l->status = p->status_code; l->keep_alive = http_should_keep_alive(p);
    return 0;
}

static int receipt(struct lane *l, const struct upload_tuning *tune)
{
    if (l->status != 200 && (l->query || l->status != 201)) { return -EBADMSG; }
    struct reply { char *status; int size; char *sha256; int offset, max_chunk; } reply = {0};
    static const struct json_obj_descr fields[] = {
        JSON_OBJ_DESCR_PRIM(struct reply, status, JSON_TOK_STRING),
        JSON_OBJ_DESCR_PRIM(struct reply, size, JSON_TOK_NUMBER),
        JSON_OBJ_DESCR_PRIM(struct reply, sha256, JSON_TOK_STRING),
        JSON_OBJ_DESCR_PRIM(struct reply, offset, JSON_TOK_NUMBER),
        JSON_OBJ_DESCR_PRIM(struct reply, max_chunk, JSON_TOK_NUMBER),
    };
    int mask = json_obj_parse(l->body, l->body_size, fields, ARRAY_SIZE(fields), &reply);
    if (mask < 0 || (mask & 15) != 15 || reply.size < 0 ||
        (uint32_t)reply.size != l->stamp.size || strcmp(reply.sha256, l->hash) ||
        reply.offset < 0 || (uint32_t)reply.offset > l->stamp.size ||
        (!l->query && (uint32_t)reply.offset != l->end) ||
        (strcmp(reply.status, "stored") && strcmp(reply.status, "partial")) ||
        (!strcmp(reply.status, "stored") != ((uint32_t)reply.offset == l->stamp.size))) { return -EBADMSG; }
    if (l->query) {
        int ret = upload_tune_server_limit(tune->chunk_size, reply.max_chunk, mask & 16);
        if (ret < 0) { return ret; }
    }
    l->stamp.confirmed = reply.offset;
    return 0;
}

static int request(struct lane *l, const struct upload_config *config,
                    const struct upload_tuning *tune, bool query,
                    int (*check)(void), void (*pulse)(void))
{
    l->query = query; l->offset = l->stamp.confirmed;
    uint32_t length = query ? 0 : MIN(tune->chunk_size, l->stamp.size-l->offset);
    l->end = l->offset + length;
    char chunk[90] = {0};
    if (!query) {
        uint8_t digest[32]; char text[65];
        int ret = hash_range(l, l->offset, length, digest, check, pulse);
        if (ret) { return ret; }
        hex(digest, text); snprintf(chunk, sizeof(chunk), "X-Chunk-SHA256: %s\r\n", text);
    }
    int n = snprintf((char *)l->tx, sizeof(l->tx),
        "%s /v2/devices/%s/recordings/%s/%u HTTP/1.1\r\n"
        "Host: %s:%s\r\nAuthorization: Bearer %s\r\n"
        "X-Recording-Size: %u\r\nX-Content-SHA256: %s\r\nUpload-Offset: %u\r\n%s"
        "X-Clip-Batch: 1\r\nConnection: keep-alive\r\nContent-Type: application/octet-stream\r\nContent-Length: %u\r\n\r\n",
        query ? "GET" : "PUT", config->device, l->session, l->segment,
        config->host, config->port, config->token, l->stamp.size, l->hash, l->offset, chunk, length);
    if (n < 0 || (size_t)n >= sizeof(l->tx)) { return -EMSGSIZE; }
    l->tx_size = n; l->tx_done = 0; l->body_size = l->header_size = 0;
    l->complete = false; l->status = 0; l->phase = HEADER;
    memset(l->body, 0, sizeof(l->body));
    http_parser_init(&l->parser, HTTP_RESPONSE);
    l->parser.data = l; l->parser.method = query ? HTTP_GET : HTTP_PUT;
    l->progressed = k_uptime_get();
    return 0;
}

static int step(struct lane *l, const struct upload_tuning *tune, void (*pulse)(void))
{
    if (l->phase == DONE) { return 0; }
    ssize_t n;
    if (l->phase == HEADER || l->phase == BODY) {
        if (l->tx_done == l->tx_size) {
            if (l->phase == HEADER) { l->phase = l->query ? RECEIPT : BODY; }
            if (l->phase == BODY && l->offset == l->end) { l->phase = RECEIPT; }
            if (l->phase == BODY) {
                n = fs_read(&l->file, l->tx, MIN(sizeof(l->tx), l->end-l->offset));
                if (n <= 0) { return n ? n : -EIO; }
                l->tx_size = n; l->tx_done = 0;
            }
        }
        if (l->phase != RECEIPT) {
            n = https_stream_try_write(l->stream, l->tx+l->tx_done, l->tx_size-l->tx_done);
            if (n == -EAGAIN) { return 0; }
            if (n <= 0) { return n ? n : -EIO; }
            l->tx_done += n;
            if (l->phase == BODY) { l->offset += n; }
            l->progressed = k_uptime_get(); pulse();
            return 1;
        }
    }
    n = https_stream_try_read(l->stream, l->rx, sizeof(l->rx));
    if (n == -EAGAIN) { return 0; }
    if (n <= 0) { return n ? n : -ECONNRESET; }
    static const struct http_parser_settings callbacks = {
        .on_header_field = header, .on_header_value = header,
        .on_body = body, .on_message_complete = complete,
    };
    size_t used = http_parser_execute(&l->parser, &callbacks, (char *)l->rx, n);
    if (used != (size_t)n || HTTP_PARSER_ERRNO(&l->parser) != HPE_OK) { return -EBADMSG; }
    l->progressed = k_uptime_get(); pulse();
    if (l->complete) {
        int ret = receipt(l, tune);
        if (ret) { return ret; }
        l->phase = DONE;
    }
    return 1;
}

int upload_dual_run(const struct upload_config *config, const struct upload_tuning *tune, bool parallel,
                    const char *first, uint32_t first_segment,
                    const char *second, uint32_t second_segment,
                    const uint8_t destination[32],
                    int (*connect)(struct https_stream **), int (*check)(void),
                    void (*pulse)(void), struct dual_result *result)
{
    int64_t started = k_uptime_get(), transferring = 0;
    struct lane *lanes = k_calloc(2, sizeof(*lanes));
    if (!lanes) { atomic_set(&result->error, -ENOMEM); atomic_set(&result->finished, 1); return -ENOMEM; }
    int ret = prepare(&lanes[0], first, first_segment, destination, check, pulse);
    if (!ret) { ret = prepare(&lanes[1], second, second_segment, destination, check, pulse); }
    for (unsigned int i = 0; !ret && i < 2; i++) {
        atomic_set(&result->size[i], lanes[i].stamp.size);
        if (i == 1 && !parallel) { lanes[i].stream = lanes[0].stream; }
        else { ret = connect(&lanes[i].stream); }
    }
    /* Authenticate and reconcile separately, before overlapping any payloads. */
    for (unsigned int i = 0; !ret && i < 2; i++) {
        struct lane *l = &lanes[i];
        ret = request(l, config, tune, true, check, pulse);
        while (!ret && l->phase != DONE) {
            ret = check(); if (ret) { break; }
            int moved = step(l, tune, pulse);
            if (moved < 0) { ret = moved; break; }
            if (k_uptime_get()-l->progressed >= 15000) { ret = -ETIMEDOUT; break; }
            if (!moved) { ret = https_stream_wait_pair(l->stream, NULL); }
        }
        atomic_set(&result->resumed[i], l->stamp.confirmed);
        atomic_set(&result->confirmed[i], l->stamp.confirmed);
        atomic_set(&result->sent[i], l->stamp.confirmed);
    }
    if (!ret) { transferring = k_uptime_get(); }
    while (!ret) {
        bool finished = true, moved = false;
        for (unsigned int i = 0; i < 2; i++) {
            struct lane *l = &lanes[i];
            ret = check(); if (ret) { break; }
            if (!parallel && i == 1 && lanes[0].stamp.confirmed < lanes[0].stamp.size) { continue; }
            if (l->phase == DONE && l->stamp.confirmed < l->stamp.size) {
                if (!l->keep_alive) { ret = -ECONNRESET; break; }
                ret = request(l, config, tune, false, check, pulse);
                if (ret) { break; }
            }
            if (l->phase == DONE) { continue; }
            finished = false;
            int progress = step(l, tune, pulse);
            if (progress < 0) { ret = progress; break; }
            moved |= progress > 0;
            atomic_set(&result->sent[i], l->offset);
            atomic_set(&result->confirmed[i], l->stamp.confirmed);
            if (k_uptime_get()-l->progressed >= 15000) { ret = -ETIMEDOUT; break; }
        }
        if (ret || finished) { break; }
        if (!moved) {
            ret = https_stream_wait_pair(lanes[0].phase == DONE ? NULL : lanes[0].stream,
                                         lanes[1].phase == DONE ? NULL : lanes[1].stream);
        }
    }
    if (transferring) { atomic_set(&result->transfer_ms, k_uptime_get()-transferring); }
    /* Close both streams before SD state writes and before recording can start. */
    https_stream_close(lanes[0].stream);
    if (parallel) { https_stream_close(lanes[1].stream); }
    int cleanup = 0;
    for (unsigned int i = 0; i < 2; i++) {
        struct lane *l = &lanes[i];
        if (l->opened) {
            int closed = fs_close(&l->file);
            if (!cleanup) { cleanup = closed; }
        }
        if (l->ready && !cleanup) {
            l->stamp.last_error = ret;
            l->stamp.state = l->stamp.confirmed == l->stamp.size ? UPLOAD_STORED :
                             l->stamp.confirmed ? UPLOAD_PARTIAL :
                             ret == -ECANCELED ? UPLOAD_PENDING : UPLOAD_FAILED;
            if (l->stamp.state == UPLOAD_STORED) { l->stamp.last_error = 0; }
            int saved = upload_state_save(l->session, l->segment, &l->stamp);
            if (!cleanup) { cleanup = saved; }
        }
    }
    k_free(lanes);
    if (!ret) { ret = cleanup; }
    atomic_set(&result->cleanup, cleanup); atomic_set(&result->error, ret);
    atomic_set(&result->elapsed_ms, k_uptime_get()-started); atomic_set(&result->finished, 1);
    return ret;
}
