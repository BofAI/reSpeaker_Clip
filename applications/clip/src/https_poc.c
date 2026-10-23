/* Serial HTTPS uploader: recording priority, durable state and opt-in idle scheduling. */
#include <zephyr/kernel.h>
#include <zephyr/net/socket.h>
#include <zephyr/net/sntp.h>
#include <zephyr/net/http/parser.h>
#include <zephyr/data/json.h>
#include <zephyr/fs/fs.h>
#include <mbedtls/sha256.h>
#include <mbedtls/platform_time.h>
#include <time.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <ctype.h>
#include "https_poc.h"
#include "config.h"
#include "https_stream.h"
#ifdef CONFIG_CLIP_DTLS_UPLOAD
#include "dtls_upload.h"
static struct dtls_upload *datagram;
static struct dtls_upload_stats last_datagram;
static atomic_t datagram_received;
static uint32_t cache_offset, cache_length;
#endif
#include "upload_perf.h"
#ifdef CONFIG_CLIP_TCP_TRACE
#include "tcp_trace.h"
#endif
#include "upload_state.h"
#include "upload_config.h"
#include "upload_scan.h"
#include "wifi_diag.h"
#include "wifi_trace.h"
#include "at_server.h"
#include "transport.h"
#include "storage.h"
#include "wifi.h"
#include "https_ca.h"

BUILD_ASSERT(IS_ENABLED(CONFIG_PSA_WANT_ALG_ECDSA) &&
             IS_ENABLED(CONFIG_MBEDTLS_KEY_EXCHANGE_ECDHE_ECDSA_ENABLED),
             "The HTTPS experiment's EC test certificates require ECDHE-ECDSA support");
#define MAX_FILE (8U * 1024U * 1024U)
#define UPLOAD_CHUNK (16U * 1024U)
#define CONNECT_BUDGET_MS (CONFIG_NET_SOCKETS_CONNECT_TIMEOUT + \
                           CONFIG_NET_SOCKETS_TLS_CONNECT_TIMEOUT)
BUILD_ASSERT(CONFIG_NET_SOCKETS_CONNECT_TIMEOUT > 0 &&
             CONFIG_NET_SOCKETS_TLS_CONNECT_TIMEOUT > 0 && CONNECT_BUDGET_MS <= 30000,
             "HTTPS requires bounded TCP + TLS connection timeouts (at most 30 seconds)");
static atomic_t active, cancelled, stage, last_error, bytes_sent;
static atomic_t confirmed, resume_from, total_bytes;
static atomic_t state_error;
static atomic_t stalled;
static atomic_t record_pending, record_error, cleanup_error;
enum { TLS_SETUP_IDLE, TLS_SETUP_DNS, TLS_SETUP_SOCKET, TLS_SETUP_CERTIFICATE,
       TLS_SETUP_VERIFY, TLS_SETUP_HOSTNAME, TLS_SETUP_SEND_TIMEOUT,
       TLS_SETUP_RECEIVE_TIMEOUT, TLS_SETUP_HANDSHAKE, TLS_SETUP_HTTP };
static atomic_t tls_step;
static const char *const tls_steps[] = {"idle", "resolve", "socket", "certificate",
    "peer_verify", "hostname", "send_timeout", "receive_timeout", "handshake",
#ifdef CONFIG_CLIP_DTLS_UPLOAD
    "datagram"
#else
    "http"
#endif
};
static K_SEM_DEFINE(start_job, 0, 1);
static struct upload_config *configuration;
#define settings (*configuration)
#define ssid settings.ssid
#define password settings.password
#define host settings.host
#define port settings.port
#define device_id settings.device
#define token settings.token
static K_MUTEX_DEFINE(control_lock);
static atomic_t initialized, auto_job, auto_paused, activity;
static int config_error, scan_error;
static bool config_saved;
static uint32_t failures, cursor_segment;
static char cursor_session[15];
static int64_t next_auto, time_synced_at;
static atomic_val_t observed_activity;
static char session[32];
static uint32_t segment;
static atomic_t time_ready;
static int64_t deadline;
static struct fs_file_t file;
/* Allocate only during a job, from the same heap as TLS. This saves static
 * reservation, not peak upload memory; allocation failure is a normal error. */
static struct upload_buffers {
#ifdef CONFIG_CLIP_DTLS_UPLOAD
    uint8_t file_io[4096];
#else
    uint8_t file_io[2048], receive[2048];
    char body[1025];
#endif
} *buffers;
#define io (buffers->file_io)
static char expected_hash[65];
static size_t file_length;
#ifndef CONFIG_CLIP_DTLS_UPLOAD
#define recv_buffer (buffers->receive)
#define response_body (buffers->body)
static size_t response_length, header_bytes;
static bool response_final;
static bool response_keep_alive;
static int response_status;
#endif

/* Wall time uses libc time()/CLOCK_REALTIME; TLS timers use monotonic uptime. */
mbedtls_ms_time_t mbedtls_ms_time(void) { return k_uptime_get(); }

static char *split_field(char **cursor)
{
    char *value = *cursor;
    if (!value) { return NULL; }
    char *end = strchr(value, ',');
    if (end) { *end = 0; *cursor = end + 1; }
    else { *cursor = NULL; }
    return value;
}

static void request_cancel(void)
{
    /* Only the upload thread owns TCP and TLS. Its bounded waits observe this
     * flag, without blocking the event thread or the system workqueue. */
    atomic_set(&cancelled, 1);
}

static void stall_handler(struct k_work *work)
{
    ARG_UNUSED(work);
    atomic_set(&stalled, 1);
}
static K_WORK_DELAYABLE_DEFINE(stall_work, stall_handler);

static void progress(void) { k_work_reschedule(&stall_work, K_SECONDS(15)); }

enum { IDLE, PREPARING, CONNECTING, TLS, SENDING, VERIFYING, SUCCEEDED, FAILED, CANCELLED };
static const char *const names[] = {"idle", "preparing", "connecting", "tls", "sending",
    "verifying", "succeeded", "failed", "cancelled"};

bool https_poc_active(void) { return atomic_get(&active); }
bool https_poc_record_pending(void) { return atomic_get(&record_pending); }
int https_poc_record_error(void) { return atomic_get(&record_error); }

void https_poc_record_complete(enum clip_event_result result)
{
    int error = result == CLIP_EVENT_OK ? 0 : result == CLIP_EVENT_BUSY ? -EBUSY :
                result == CLIP_EVENT_INVALID ? -EINVAL : -EIO;
    atomic_set(&record_error, error);
    atomic_clear(&record_pending);
}

bool https_poc_command_allowed(const char *name)
{
    /* Read-only instrumentation must remain available during TLS/upload. */
    if (!strcmp(name, "RES")) { return true; }
#ifdef CONFIG_CLIP_TCP_TRACE
    if (!strcmp(name, "TCPTRACE")) { return true; }
#endif
#ifdef CONFIG_CLIP_HTTPS_PROFILE
    if (!strcmp(name, "UPLOADPERF")) { return true; }
#endif
#ifdef CONFIG_CLIP_DTLS_UPLOAD
    if (!strcmp(name, "UPLOADNET")) { return true; }
#endif
    if (IS_ENABLED(CONFIG_CLIP_WIFI_DIAG) &&
        (!strcmp(name, "NETDIAG") || !strcmp(name, "NETTRACE") || !strcmp(name, "NETTX"))) {
        return true;
    }
    /* A deliberately small control surface also excludes deferred erase/reboot
     * commands which could outlive the AT handler and race a subsequent job. */
    if (https_poc_active() || https_poc_record_pending()) {
        return !strcmp(name, "UPLOAD") || !strcmp(name, "UPLOADCANCEL") || !strcmp(name, "UPLOADAUTO") ||
               !strcmp(name, "START") || !strcmp(name, "STOP") ||
               !strcmp(name, "GSTAT") || !strcmp(name, "BATT") || !strcmp(name, "VERSION");
    }
    return !strcmp(name, "NETCFG") || !strcmp(name, "UPLOADCFG") ||
           !strcmp(name, "UPTIME") || !strcmp(name, "UPLOAD") ||
           !strcmp(name, "UPLOADCANCEL") || !strcmp(name, "USB") ||
           !strcmp(name, "GSTAT") || !strcmp(name, "BATT") ||
           !strcmp(name, "VERSION") || !strcmp(name, "LIST") ||
           !strcmp(name, "START") || !strcmp(name, "STOP") ||
           !strcmp(name, "UPLOADSTATE") || !strcmp(name, "UPLOADLIST") ||
           !strcmp(name, "UPLOADAUTO") || !strcmp(name, "UPLOADSAVE") ||
           !strcmp(name, "UPLOADCLEAR") || !strcmp(name, "UPLOADCLOCK") ||
           !strcmp(name, "TIME") || !strcmp(name, "STORAGE");
}

bool https_poc_event(enum clip_event event, struct clip_event_result_info *result,
                     bool device_idle)
{
    enum clip_event_result ret = CLIP_EVENT_OK;
    int error = 0;
    if (event == CLIP_EVENT_START || event == CLIP_EVENT_STOP) { atomic_inc(&activity); }
    if (event == CLIP_EVENT_POWER_OFF_EXEC) { atomic_set(&auto_paused, 1); }
    if (event == CLIP_EVENT_UPLOAD_BEGIN) {
        if (!device_idle || https_poc_record_pending() || !atomic_cas(&active, 0, 1)) {
            ret = CLIP_EVENT_BUSY;
        } else {
            /* Initialize in event order. Clearing after BEGIN returns could
             * erase a button cancellation arriving before the worker wakes. */
            atomic_clear(&cancelled); atomic_clear(&stalled);
            atomic_clear(&last_error); atomic_clear(&bytes_sent);
            atomic_clear(&confirmed); atomic_clear(&resume_from); atomic_clear(&total_bytes);
            atomic_clear(&state_error);
            atomic_clear(&cleanup_error);
            atomic_set(&stage, PREPARING);
        }
    } else if (event == CLIP_EVENT_UPLOAD_END) {
        atomic_clear(&active);
        if (https_poc_record_pending() && atomic_get(&cleanup_error)) {
            atomic_set(&record_error, atomic_get(&cleanup_error));
            atomic_clear(&record_pending);
        }
    } else if (event == CLIP_EVENT_START && https_poc_active()) {
        if (!https_poc_record_pending()) { atomic_clear(&record_error); }
        atomic_set(&record_pending, 1);
        request_cancel();
        error = -EINPROGRESS;
    } else if (event == CLIP_EVENT_STOP && https_poc_record_pending()) {
        atomic_set(&record_error, -ECANCELED);
        atomic_clear(&record_pending);
    } else if (https_poc_active() && event != CLIP_EVENT_STATUS_SHOW &&
               event != CLIP_EVENT_USB_CONNECTED) {
        if (event == CLIP_EVENT_POWER_OFF_SHOW || event == CLIP_EVENT_POWER_OFF_EXEC) {
            atomic_clear(&record_pending);
            atomic_set(&record_error, -ECANCELED);
        }
        if (event == CLIP_EVENT_POWER_OFF_EXEC) {
            request_cancel();
        }
        ret = CLIP_EVENT_BUSY;
    } else { return false; }
    if (result) { result->result = ret; result->error_code = error; }
    return true;
}

static int check_cancel(void)
{
    if (atomic_get(&cancelled)) { return -ECANCELED; }
    if (atomic_get(&stalled)) { return -ETIMEDOUT; }
    return k_uptime_get() >= deadline ? -ETIMEDOUT : 0;
}

static bool valid_id(const char *s, size_t max)
{
    size_t n = strlen(s);
    if (!n || n > max) { return false; }
    for (; *s; s++) {
        if (!isalnum((unsigned char)*s) && *s != '_' && *s != '-') { return false; }
    }
    return true;
}

static bool valid_session(const char *s)
{
    if (strlen(s) != 14) { return false; }
    for (; *s; s++) {
        if (*s < '0' || *s > '9') { return false; }
    }
    return true;
}

static int hash_file_range(size_t offset, size_t length, char output[65])
{
    int ret = fs_seek(&file, offset, FS_SEEK_SET);
    if (ret) { return ret; }
    mbedtls_sha256_context hash;
    uint8_t digest[32];
    mbedtls_sha256_init(&hash);
    ret = mbedtls_sha256_starts(&hash, 0);
    size_t total = 0;
    while (!ret && total < length) {
        ret = check_cancel();
        if (ret) { break; }
        uint64_t measured = upload_perf_now();
        ssize_t n = fs_read(&file, io, MIN(sizeof(io), length - total));
        upload_perf_add(UP_READ_US, upload_perf_now() - measured);
        upload_perf_add(UP_FS_READS, 1);
        if (n <= 0) { ret = n < 0 ? (int)n : -EIO; break; }
        total += n;
        measured = upload_perf_now();
        ret = mbedtls_sha256_update(&hash, io, n);
        upload_perf_add(UP_HASH_US, upload_perf_now() - measured);
    }
    if (!ret) { ret = mbedtls_sha256_finish(&hash, digest); }
    mbedtls_sha256_free(&hash);
    if (ret) { return ret; }
    for (int i = 0; i < 32; i++) { snprintf(output + i * 2, 3, "%02x", digest[i]); }
    return fs_seek(&file, offset, FS_SEEK_SET);
}

static int hash_file(void) { return hash_file_range(0, file_length, expected_hash); }

#ifndef CONFIG_CLIP_DTLS_UPLOAD
static int send_payload(struct https_stream *stream, size_t offset, size_t length)
{
    size_t total = 0;
    atomic_set(&stage, SENDING);
    while (total < length) {
        int ret = check_cancel();
        if (ret) { return ret; }
        uint64_t measured = upload_perf_now();
        ssize_t n = fs_read(&file, io, MIN(sizeof(io), length - total));
        upload_perf_add(UP_READ_US, upload_perf_now() - measured);
        upload_perf_add(UP_FS_READS, 1);
        if (n <= 0) { return n < 0 ? (int)n : -EIO; }
        ret = https_stream_write(stream, io, n);
        if (ret) { return ret; }
        total += n;
        progress();
        atomic_set(&bytes_sent, offset + total);
    }
    return (int)total;
}

static int header_fragment(struct http_parser *parser, const char *at, size_t length)
{
    ARG_UNUSED(parser); ARG_UNUSED(at);
    progress();
    if (length > 4096 - header_bytes) { return -EMSGSIZE; }
    header_bytes += length;
    return check_cancel();
}

static int body_fragment(struct http_parser *parser, const char *at, size_t length)
{
    ARG_UNUSED(parser);
    progress();
    if (length > 1024 - response_length) { return -EMSGSIZE; }
    memcpy(response_body + response_length, at, length);
    response_length += length;
    return check_cancel();
}

static int message_complete(struct http_parser *parser)
{
    response_status = parser->status_code;
    response_keep_alive = http_should_keep_alive(parser);
    response_final = true;
    return check_cancel();
}

static int accept_receipt(const char *status, int size, const char *sha256,
                           int offset, int expected_offset)
{
    if (size < 0 || (size_t)size != file_length || strcmp(sha256, expected_hash) || offset < 0 ||
        (size_t)offset > file_length || (expected_offset >= 0 && offset != expected_offset) ||
        (strcmp(status, "stored") && strcmp(status, "partial")) ||
        (!strcmp(status, "stored") != ((size_t)offset == file_length))) { return -EBADMSG; }
    atomic_set(&confirmed, offset);
    return 0;
}

static int receive_receipt(struct https_stream *stream, int expected_offset)
{
    struct http_parser parser;
    http_parser_init(&parser, HTTP_RESPONSE);
    parser.method = expected_offset < 0 ? HTTP_GET : HTTP_PUT;
    const struct http_parser_settings callbacks = {
        .on_header_field = header_fragment, .on_header_value = header_fragment,
        .on_body = body_fragment, .on_message_complete = message_complete,
    };
    response_length = header_bytes = 0;
    response_final = false;
    response_status = 0;
    memset(response_body, 0, sizeof(response_body));
    while (!response_final) {
        ssize_t count = https_stream_read(stream, recv_buffer, sizeof(recv_buffer));
        if (count < 0) { return count; }
        size_t used = http_parser_execute(&parser, &callbacks, (char *)recv_buffer, count);
        int ret = check_cancel();
        if (ret) { return ret; }
        if (HTTP_PARSER_ERRNO(&parser) != HPE_OK || used != (size_t)count) { return -EBADMSG; }
        if (!count && !response_final) { return -ECONNRESET; }
    }
    atomic_set(&stage, VERIFYING);
    if (response_status != 200 && (expected_offset < 0 || response_status != 201)) { return -EBADMSG; }
    struct receipt { char *status; int size; char *sha256; int offset; } receipt;
    static const struct json_obj_descr fields[] = {
        JSON_OBJ_DESCR_PRIM(struct receipt, status, JSON_TOK_STRING),
        JSON_OBJ_DESCR_PRIM(struct receipt, size, JSON_TOK_NUMBER),
        JSON_OBJ_DESCR_PRIM(struct receipt, sha256, JSON_TOK_STRING),
        JSON_OBJ_DESCR_PRIM(struct receipt, offset, JSON_TOK_NUMBER),
    };
    int ret = json_obj_parse(response_body, response_length, fields, ARRAY_SIZE(fields), &receipt);
    if (ret != 15) { return -EBADMSG; }
    return accept_receipt(receipt.status, receipt.size, receipt.sha256, receipt.offset, expected_offset);
}

static int request_chunk_impl(struct https_stream *stream, bool query, size_t offset, size_t length)
{
    char chunk_header[90] = {0};
    if (!query) {
        char digest[65];
        int ret = hash_file_range(offset, length, digest);
        if (ret) { return ret; }
        snprintf(chunk_header, sizeof(chunk_header), "X-Chunk-SHA256: %s\r\n", digest);
    }
    uint64_t measured = upload_perf_now();
    int n = snprintf((char *)io, sizeof(io),
        "%s /v2/devices/%s/recordings/%s/%u HTTP/1.1\r\n"
        "Host: %s:%s\r\nAuthorization: Bearer %s\r\n"
        "X-Recording-Size: %u\r\nX-Content-SHA256: %s\r\n"
        "Upload-Offset: %u\r\n%s%sConnection: keep-alive\r\n"
        "Content-Type: application/octet-stream\r\nContent-Length: %u\r\n\r\n",
        query ? "GET" : "PUT", device_id, session, segment, host, port, token,
        (unsigned int)file_length, expected_hash, (unsigned int)offset, chunk_header,
        atomic_get(&auto_job) ? "X-Clip-Batch: 1\r\n" : "",
        (unsigned int)length);
    if (n < 0 || (size_t)n >= sizeof(io)) { return -EMSGSIZE; }
    progress();
    int ret = https_stream_write(stream, io, n);
    upload_perf_add(UP_HEADER_US, upload_perf_now() - measured);
    memset(io, 0, sizeof(io));
    if (ret) { return ret; }
    if (!query) {
        /* Hashing rewound the file; request formatting only reused the buffer. */
        measured = upload_perf_now();
        ret = send_payload(stream, offset, length);
        upload_perf_add(UP_BODY_US, upload_perf_now() - measured);
        if (ret < 0) { return ret; }
    }
    measured = upload_perf_now();
    ret = receive_receipt(stream, query ? -1 : (int)(offset + length));
    upload_perf_add(UP_RECEIPT_US, upload_perf_now() - measured);
    return ret;
}

static int request_chunk(struct https_stream *stream, bool query, size_t offset, size_t length)
{
    upload_perf_begin(query ? UP_GET : UP_PUT, session, segment, offset, length);
    int ret = request_chunk_impl(stream, query, offset, length);
    upload_perf_end(ret);
    return ret;
}

static int upload(struct https_stream *stream)
{
    int ret = request_chunk(stream, true, 0, 0);
    if (ret) { return ret; }
    size_t offset = atomic_get(&confirmed);
    atomic_set(&resume_from, offset);
    atomic_set(&bytes_sent, offset);
    while (offset < file_length) {
        ret = check_cancel();
        if (ret) { return ret; }
        size_t length = MIN(UPLOAD_CHUNK, file_length - offset);
        ret = request_chunk(stream, false, offset, length);
        if (ret) { return ret; }
        offset = atomic_get(&confirmed);
    }
    return check_cancel();
}

static int tls_connect(struct https_stream **stream)
{
    atomic_set(&tls_step, TLS_SETUP_DNS);
    struct zsock_addrinfo hints = {.ai_family = AF_INET, .ai_socktype = SOCK_STREAM};
    struct zsock_addrinfo *address = NULL;
    int ret = zsock_getaddrinfo(host, port, &hints, &address);
    if (ret) { return -EHOSTUNREACH; }
#ifdef CONFIG_CLIP_WIFI_DIAG
    struct net_in_addr local = {0};
    struct wifi_station_network network;
    wifi_station_get_network(&network);
    net_addr_pton(AF_INET, network.ip, &local);
    wifi_trace_peer(((struct sockaddr_in *)address->ai_addr)->sin_addr.s_addr, local.s_addr);
#endif
    atomic_set(&tls_step, TLS_SETUP_SOCKET);
    if (deadline - k_uptime_get() < CONNECT_BUDGET_MS) {
        zsock_freeaddrinfo(address); return -ETIMEDOUT;
    }
    atomic_set(&tls_step, TLS_SETUP_HANDSHAKE);
    wifi_diag_capture(WIFI_DIAG_CONNECT_BEGIN, 0);
    ret = https_stream_open(stream, address->ai_addr, address->ai_addrlen,
                            host, https_ca, sizeof(https_ca), check_cancel);
    wifi_diag_capture(WIFI_DIAG_CONNECT_END, ret);
    zsock_freeaddrinfo(address);
    return ret;
}

#endif

static void digest_text(const uint8_t digest[32], char text[65])
{
    for (int i = 0; i < 32; i++) { snprintf(text + 2 * i, 3, "%02x", digest[i]); }
}

static void digest_bytes(const char text[65], uint8_t digest[32])
{
    for (int i = 0; i < 32; i++) {
        unsigned int a = text[2 * i], b = text[2 * i + 1];
        digest[i] = ((a <= '9' ? a - '0' : a - 'a' + 10) << 4) |
                    (b <= '9' ? b - '0' : b - 'a' + 10);
    }
}

static int destination_digest(uint8_t digest[32])
{
    if (!host[0]) { return -EINVAL; }
    mbedtls_sha256_context hash;
    mbedtls_sha256_init(&hash);
    int ret = mbedtls_sha256_starts(&hash, 0);
    const char *fields[] = {host, port, device_id};
    for (size_t i = 0; !ret && i < ARRAY_SIZE(fields); i++) {
        ret = mbedtls_sha256_update(&hash, (const uint8_t *)fields[i], strlen(fields[i]) + 1);
    }
#ifdef CONFIG_CLIP_DTLS_UPLOAD
    static const uint8_t protocol_identity[] = "dtls-upload-v3";
    if (!ret) { ret = mbedtls_sha256_update(&hash, protocol_identity, sizeof(protocol_identity)); }
#endif
    if (!ret) { ret = mbedtls_sha256_update(&hash, https_ca, sizeof(https_ca)); }
    if (!ret) { ret = mbedtls_sha256_finish(&hash, digest); }
    mbedtls_sha256_free(&hash);
    return ret;
}

static int state_query(const char *args, char *response, size_t length)
{
    if (clip_event_get_state() != CLIP_STATE_IDLE) { return -EBUSY; }
    char selector[48];
    if (strlen(args) >= sizeof(selector)) { return -EINVAL; }
    strcpy(selector, args);
    char *colon = strchr(selector, ':');
    if (!colon) { return -EINVAL; }
    *colon++ = 0;
    char *end;
    unsigned long number = strtoul(colon, &end, 10);
    if (!valid_session(selector) || !*colon || *end || !number || number > 999999999) { return -EINVAL; }
    uint8_t destination[32];
    int ret = destination_digest(destination);
    if (ret) { return ret; }
    ret = storage_ensure_mounted();
    if (ret) { return ret; }
    char path[STORAGE_PATH_MAX];
    ret = storage_build_chunk_path(selector, number, path, sizeof(path));
    if (ret) { return ret; }
    struct fs_dirent entry;
    ret = fs_stat(path, &entry);
    if (ret) { return ret; }
    if (entry.type != FS_DIR_ENTRY_FILE) { return -EINVAL; }
    struct upload_file_record record;
    int loaded = upload_state_load(selector, number, &record);
    if (loaded && loaded != -ENOENT && loaded != -EBADMSG) { return loaded; }
    bool size_match = !loaded && record.size == entry.size;
    bool destination_match = !loaded &&
                             !memcmp(record.destination_sha256, destination, sizeof(destination));
    bool matches = size_match && destination_match;
    char hash[65] = {0};
    if (!loaded) { digest_text(record.content_sha256, hash); }
    return snprintf(response, length,
        "{\"ok\":true,\"data\":{\"session\":\"%s\",\"segment\":%u,\"state\":\"%s\","
        "\"size\":%u,\"confirmed\":%u,\"last_error\":%d,\"state_valid\":%s,"
        "\"destination_match\":%s,\"size_match\":%s,"
        "\"state_error\":%d,\"sha256\":\"%s\"}}\n",
        selector, (unsigned int)number, upload_state_name(matches ? record.state : UPLOAD_PENDING),
        (unsigned int)entry.size, matches ? (unsigned int)record.confirmed : 0,
        matches ? (int)record.last_error : 0, loaded ? "false" : "true",
        destination_match ? "true" : "false", size_match ? "true" : "false",
        loaded == -EBADMSG ? loaded : 0, hash);
}

/* Keep only the nearest bounded batch, never the whole recording inventory.
 * A cancelled/failed batch is discarded; its next scan uses the last attempted
 * key so files after a failure still get a turn. */
#define AUTO_BATCH_FILES 8
struct candidate { char session[15]; uint32_t segment; };
struct selection {
    struct candidate first[AUTO_BATCH_FILES], after[AUTO_BATCH_FILES];
    uint8_t destination[32];
    unsigned first_count, after_count, index;
};
static struct selection *auto_batch;
static int compare_key(const char *a, uint32_t an, const char *b, uint32_t bn)
{
    int ret = strcmp(a, b);
    return ret ? ret : an < bn ? -1 : an > bn ? 1 : 0;
}
static void add_candidate(struct candidate *items, unsigned *count,
                          const char *id, uint32_t number)
{
    unsigned at = 0;
    while (at < *count && compare_key(items[at].session, items[at].segment, id, number) < 0) { at++; }
    if (at == AUTO_BATCH_FILES || (at < *count &&
        compare_key(items[at].session, items[at].segment, id, number) == 0)) { return; }
    unsigned used = MIN(*count, AUTO_BATCH_FILES - 1);
    memmove(items + at + 1, items + at, (used - at) * sizeof(*items));
    strcpy(items[at].session, id); items[at].segment = number;
    *count = MIN(*count + 1, AUTO_BATCH_FILES);
}
static int choose_visit(const char *id, uint32_t number, uint32_t size, bool sealed, void *context)
{
    struct selection *selection = context;
    if (!sealed || !size || size > MAX_FILE) { return 0; }
    struct upload_file_record record;
    int ret = upload_state_load(id, number, &record);
    if (ret && ret != -ENOENT && ret != -EBADMSG) { return ret; }
    if (!ret && record.state == UPLOAD_STORED && record.size == size &&
        !memcmp(record.destination_sha256, selection->destination, 32)) { return 0; }
    add_candidate(selection->first, &selection->first_count, id, number);
    if (compare_key(id, number, cursor_session, cursor_segment) > 0) {
        add_candidate(selection->after, &selection->after_count, id, number);
    }
    return 0;
}
static void select_candidate(const struct candidate *next)
{
    strcpy(session, next->session); segment = next->segment;
    strcpy(cursor_session, session); cursor_segment = segment;
}
static bool next_auto_file(void)
{
    if (check_cancel() || !auto_batch || auto_batch->index >= auto_batch->first_count) { return false; }
    select_candidate(&auto_batch->first[auto_batch->index++]);
    return true;
}
static int refill_auto_batch(void)
{
    upload_perf_begin(UP_SCAN, NULL, 0, 0, 0);
    memset(auto_batch, 0, sizeof(*auto_batch));
    int ret = destination_digest(auto_batch->destination);
    if (!ret) { ret = upload_scan(choose_visit, auto_batch, check_cancel); }
    if (!ret) { ret = check_cancel(); }
    upload_perf_end(ret);
    k_mutex_lock(&control_lock, K_FOREVER);
    scan_error = ret;
    k_mutex_unlock(&control_lock);
    if (ret) { return ret; }
    if (auto_batch->after_count) {
        memcpy(auto_batch->first, auto_batch->after, auto_batch->after_count * sizeof(struct candidate));
        auto_batch->first_count = auto_batch->after_count;
    }
    return auto_batch->first_count ? 1 : 0;
}
static int begin_auto(void)
{
    int selected = 0;
    k_mutex_lock(&control_lock, K_FOREVER);
    atomic_val_t generation = atomic_get(&activity);
    if (generation != observed_activity) {
        observed_activity = generation; next_auto = k_uptime_get() + 5000;
    }
    if (!atomic_get(&initialized) || !settings.enabled || atomic_get(&auto_paused) ||
        !upload_config_valid(&settings) || k_uptime_get() < next_auto) { goto done; }
    struct clip_event_result_info result;
    if (clip_post_event_sync(CLIP_EVENT_UPLOAD_BEGIN, &result) || result.result != CLIP_EVENT_OK) {
        goto done;
    }
    deadline = k_uptime_get() + 300000;
    atomic_set(&auto_job, 1);
    auto_batch = k_calloc(1, sizeof(*auto_batch));
    int ret = auto_batch ? 0 : -ENOMEM;
    /* Storage is claimed and settings cannot change while active. Let USB
     * status/off and recording cancellation run during the directory walk. */
    k_mutex_unlock(&control_lock);
    if (!ret) { int found = refill_auto_batch(); ret = found < 0 ? found : 0; }
    k_mutex_lock(&control_lock, K_FOREVER);
    scan_error = ret;
    if (!ret && auto_batch->first_count) {
        auto_batch->index = 1;
        select_candidate(&auto_batch->first[0]);
        wifi_diag_reset(); selected = 1;
    } else {
        k_free(auto_batch); auto_batch = NULL; atomic_clear(&auto_job);
        atomic_set(&stage, ret == -ECANCELED ? CANCELLED : ret ? FAILED : IDLE);
        atomic_set(&last_error, ret);
        /* STOP/new recordings wake us sooner. Avoid keeping an empty SD awake
         * with scans almost as frequent as its 45-second idle power timeout. */
        next_auto = k_uptime_get() + (ret ? 30000 : 300000);
        if (ret && ret != -ECANCELED) { atomic_set(&cleanup_error, ret); }
        while (clip_post_event_sync(CLIP_EVENT_UPLOAD_END, &result)) { k_sleep(K_MSEC(10)); }
    }
done:
    k_mutex_unlock(&control_lock);
    return selected;
}

/* Two records per page keep the response below the existing AT buffer limit.
 * Re-scan after the last key instead of exposing FAT enumeration positions. */
struct list_item { struct candidate key; uint32_t size; bool sealed; };
struct list_selection { struct candidate cursor; struct list_item items[3]; };
static int list_visit(const char *id, uint32_t number, uint32_t size, bool sealed, void *context)
{
    struct list_selection *s = context;
    if (compare_key(id, number, s->cursor.session, s->cursor.segment) <= 0) { return 0; }
    for (unsigned int i = 0; i < ARRAY_SIZE(s->items); i++) {
        struct list_item *item = &s->items[i];
        if (item->key.session[0] && !compare_key(id, number, item->key.session, item->key.segment)) {
            return 0;
        }
        if (!item->key.session[0] || compare_key(id, number, item->key.session, item->key.segment) < 0) {
            for (unsigned int j = ARRAY_SIZE(s->items) - 1; j > i; j--) { s->items[j] = s->items[j - 1]; }
            strcpy(item->key.session, id); item->key.segment = number;
            item->size = size; item->sealed = sealed; break;
        }
    }
    return 0;
}
static int list_check(void) { return 0; }
static int list_states(const char *args, char *response, size_t length)
{
    if (clip_event_get_state() != CLIP_STATE_IDLE) { return -EBUSY; }
    struct list_selection selection = {0};
    if (strcmp(args, "0")) {
        char key[32];
        if (strlen(args) >= sizeof(key)) { return -EINVAL; }
        strcpy(key, args); char *colon = strchr(key, ':');
        if (!colon) { return -EINVAL; }
        *colon++ = 0; char *end;
        unsigned long n = strtoul(colon, &end, 10);
        if (!valid_session(key) || !*colon || *end || !n || n > 999999999) { return -EINVAL; }
        strcpy(selection.cursor.session, key); selection.cursor.segment = n;
    }
    uint8_t destination[32];
    int ret = destination_digest(destination);
    if (!ret) {
        ret = upload_scan_after(list_visit, &selection, list_check,
                                selection.cursor.session[0] ? selection.cursor.session : NULL);
    }
    if (ret) { return ret; }
    size_t used = snprintf(response, length, "{\"ok\":true,\"data\":{\"items\":[");
    if (used >= length) { return -ENOSPC; }
    for (unsigned int i = 0; i < 2 && selection.items[i].key.session[0]; i++) {
        struct list_item *item = &selection.items[i];
        struct upload_file_record record;
        int loaded = upload_state_load(item->key.session, item->key.segment, &record);
        if (loaded && loaded != -ENOENT && loaded != -EBADMSG) { return loaded; }
        bool match = !loaded && record.size == item->size &&
                     !memcmp(record.destination_sha256, destination, sizeof(destination));
        bool eligible = item->sealed && item->size && item->size <= MAX_FILE;
        int n = snprintf(response + used, length - used,
            "%s{\"session\":\"%s\",\"segment\":%u,\"state\":\"%s\",\"size\":%u,"
            "\"confirmed\":%u,\"eligible\":%s,\"state_error\":%d}", i ? "," : "",
            item->key.session, (unsigned int)item->key.segment,
            upload_state_name(match ? record.state : UPLOAD_PENDING), (unsigned int)item->size,
            match ? (unsigned int)record.confirmed : 0, eligible ? "true" : "false",
            loaded == -EBADMSG ? loaded : 0);
        if (n < 0 || (size_t)n >= length - used) { return -ENOSPC; }
        used += n; selection.cursor = item->key;
    }
    int n = snprintf(response + used, length - used, "],\"more\":%s,\"next\":\"%s:%u\"}}\n",
        selection.items[2].key.session[0] ? "true" : "false",
        selection.cursor.session, (unsigned int)selection.cursor.segment);
    return n < 0 || (size_t)n >= length - used ? -ENOSPC : (int)(used + n);
}

/* SNTP supplies wall time after boot, never a persisted timestamp pretending to
 * include powered-off time. Replies are not authenticated; TLS chain, hostname
 * and validity checks stay mandatory. DNS retains its SDK 5-second budget. */
static int sync_upload_time(void)
{
    if (atomic_get(&time_ready) && k_uptime_get() - time_synced_at < 6 * 60 * 60 * 1000) { return 0; }
    atomic_clear(&time_ready);
    struct zsock_addrinfo hints = {.ai_family = AF_INET, .ai_socktype = SOCK_DGRAM};
    struct zsock_addrinfo *addresses = NULL;
    int ret = zsock_getaddrinfo(settings.clock_host, settings.clock_port, &hints, &addresses);
    if (ret) { return -EHOSTUNREACH; }
    ret = check_cancel();
    struct sntp_ctx clock;
    bool opened = false;
    if (!ret) {
        ret = sntp_init(&clock, addresses->ai_addr, addresses->ai_addrlen);
        opened = ret == 0;
    }
    zsock_freeaddrinfo(addresses);
    struct sntp_time now;
    int64_t end = k_uptime_get() + 5000;
    if (!ret) {
        ret = sntp_query(&clock, 50, &now);
        while (ret == -ETIMEDOUT && k_uptime_get() < end) {
            ret = check_cancel();
            if (ret) { break; }
            ret = sntp_recv_response(&clock, 50, &now);
        }
    }
    if (opened) { sntp_close(&clock); }
    if (!ret && (now.seconds < 1700000000ULL || now.seconds > 4102444800ULL)) { ret = -ERANGE; }
    if (!ret) {
        struct timespec ts = {.tv_sec = now.seconds, .tv_nsec = 0};
        ret = clock_settime(CLOCK_REALTIME, &ts) ? -errno : 0;
        if (!ret) { ret = config_set_time(now.seconds); }
        if (!ret) { atomic_set(&time_ready, 1); time_synced_at = k_uptime_get(); }
    }
    return ret ? ret : check_cancel();
}

static void schedule_after_job(int ret)
{
        k_mutex_lock(&control_lock, K_FOREVER);
        k_free(auto_batch); auto_batch = NULL;
        if (atomic_get(&auto_job)) {
            if (!ret) { failures = 0; next_auto = k_uptime_get() + 5000; }
            else if (ret == -ECANCELED) { next_auto = k_uptime_get() + 5000; }
            else {
                failures = MIN(failures + 1, 5U);
                next_auto = k_uptime_get() + MIN(300000U, 20000U << (failures - 1));
            }
        }
        atomic_clear(&auto_job);
        k_mutex_unlock(&control_lock);
}

#ifdef CONFIG_CLIP_DTLS_UPLOAD
static int datagram_read(uint32_t offset, uint8_t *out, size_t length)
{
    int ret = check_cancel(); if (ret) { return ret; }
    if (offset > file_length || length > file_length - offset) { return -EINVAL; }
    if (!cache_length || offset < cache_offset || offset + length > cache_offset + cache_length) {
        ret = fs_seek(&file, offset, FS_SEEK_SET); if (ret) { return ret; }
        cache_offset = offset; cache_length = 0;
        uint32_t wanted = MIN(sizeof(io), file_length - offset);
        while (cache_length < wanted) {
            ret = check_cancel(); if (ret) { return ret; }
            ssize_t n = fs_read(&file, io + cache_length, wanted - cache_length);
            if (n <= 0) { return n < 0 ? (int)n : -EIO; }
            cache_length += n;
        }
    }
    memcpy(out, io + offset - cache_offset, length);
    return 0;
}
static void datagram_progress(uint32_t sent, uint32_t received, uint32_t durable, uint32_t resumed)
{
    atomic_set(&stage, SENDING);
    atomic_set(&bytes_sent, sent); atomic_set(&datagram_received, received);
    atomic_set(&confirmed, durable); atomic_set(&resume_from, resumed);
    progress();
}
static int send_datagram_file(const struct upload_file_record *stamp)
{
    struct dtls_upload_identity identity = {.device = device_id, .session = session,
        .credential = token, .segment = segment, .size = file_length};
    memcpy(identity.sha256, stamp->content_sha256, sizeof(identity.sha256));
    cache_length = 0;
    struct dtls_upload_stats statistics;
    int ret = dtls_upload_file(datagram, &identity, datagram_read, datagram_progress, &statistics);
    k_mutex_lock(&control_lock, K_FOREVER); last_datagram = statistics;
    k_mutex_unlock(&control_lock);
    return ret;
}
#endif

/* Upload one file; the batch owns Wi-Fi/TLS and recording/storage exclusion. */
static int upload_file(struct https_stream **stream)
{
    upload_perf_begin(UP_PREPARE, session, segment, 0, 0);
#ifdef CONFIG_CLIP_DTLS_UPLOAD
    atomic_set(&tls_step, datagram ? TLS_SETUP_HTTP : TLS_SETUP_IDLE);
    atomic_clear(&datagram_received);
#else
    atomic_set(&tls_step, *stream ? TLS_SETUP_HTTP : TLS_SETUP_IDLE);
#endif
    atomic_set(&stage, PREPARING);
    atomic_clear(&bytes_sent); atomic_clear(&confirmed); atomic_clear(&resume_from);
    atomic_clear(&total_bytes); atomic_clear(&state_error);
    deadline = k_uptime_get() + 300000;
    int ret = check_cancel();
    char path[STORAGE_PATH_MAX];
    bool opened = false, state_ready = false;
    struct upload_file_record stamp = {0};
    struct fs_dirent entry;
    if (!ret) { ret = storage_ensure_mounted(); }
    if (!ret) { ret = storage_build_chunk_path(session, segment, path, sizeof(path)); }
    if (!ret) { ret = fs_stat(path, &entry); }
    if (!ret && (entry.type != FS_DIR_ENTRY_FILE || !entry.size || entry.size > MAX_FILE)) { ret = -EINVAL; }
    fs_file_t_init(&file);
    if (!ret) { ret = fs_open(&file, path, FS_O_READ); opened = ret == 0; }
    if (!ret) {
        file_length = entry.size;
        atomic_set(&total_bytes, file_length);
#ifdef CONFIG_CLIP_DTLS_UPLOAD
        struct upload_file_record cached;
        uint8_t destination[32];
        ret = destination_digest(destination);
        int loaded = ret ? ret : upload_state_load(session, segment, &cached);
        if (!ret && !loaded && cached.size == file_length &&
            !memcmp(cached.destination_sha256, destination, sizeof(destination))) {
            /* Sealed segments are immutable. Reuse their CRC-protected hash;
             * final server SHA256 still detects SD corruption before COMPLETE. */
            digest_text(cached.content_sha256, expected_hash);
        } else if (!ret && (loaded == -ENOENT || loaded == -EBADMSG || !loaded)) { ret = hash_file(); }
        else if (!ret) { ret = loaded; }
#else
        ret = hash_file();
#endif
    }
    if (!ret) {
        stamp.size = file_length;
        digest_bytes(expected_hash, stamp.content_sha256);
        ret = destination_digest(stamp.destination_sha256);
        if (!ret) {
            state_ready = true;
            uint64_t measured = upload_perf_now();
            ret = upload_state_save(session, segment, &stamp);
            upload_perf_add(UP_STATE_US, upload_perf_now() - measured);
            atomic_set(&state_error, ret);
        }
    }
    if (!ret) { ret = check_cancel(); }
    upload_perf_end(ret);
    if (!ret && !wifi_is_sta_connected()) {
        upload_perf_begin(UP_WIFI, session, segment, 0, 0);
#ifdef CONFIG_CLIP_DTLS_UPLOAD
        dtls_upload_close(datagram); datagram = NULL;
#else
        https_stream_close(*stream); *stream = NULL;
#endif
        atomic_set(&stage, CONNECTING);
        ret = wifi_station_connect(ssid, password, &cancelled);
        upload_perf_end(ret);
    }
    if (!ret) {
        upload_perf_begin(UP_CLOCK, session, segment, 0, 0);
        ret = sync_upload_time();
        upload_perf_end(ret);
    }
#ifdef CONFIG_CLIP_DTLS_UPLOAD
    ARG_UNUSED(stream);
    bool reused = datagram != NULL;
    if (!ret && !datagram) {
        atomic_set(&stage, TLS); atomic_set(&tls_step, TLS_SETUP_HANDSHAKE);
        ret = dtls_upload_open(&datagram, host, port, https_ca, sizeof(https_ca), check_cancel);
    }
    if (!ret) {
        atomic_set(&tls_step, TLS_SETUP_HTTP);
        ret = send_datagram_file(&stamp);
        if (reused && (ret == -EIO || ret == -ECONNRESET || ret == -EPIPE || ret == -ENOTCONN) && !check_cancel()) {
            dtls_upload_close(datagram); datagram = NULL;
            ret = dtls_upload_open(&datagram, host, port, https_ca, sizeof(https_ca), check_cancel);
            if (!ret) { ret = send_datagram_file(&stamp); }
        }
    }
#else
    bool reused = *stream != NULL;
    if (!ret && !*stream) {
        upload_perf_begin(UP_TLS, session, segment, 0, 0);
        atomic_set(&stage, TLS);
        ret = tls_connect(stream);
        upload_perf_end(ret);
    }
    if (!ret) {
        atomic_set(&tls_step, TLS_SETUP_HTTP);
        ret = upload(*stream);
        /* A peer may close an idle reused connection without a close header.
         * Reconnect once and query the durable offset again; never guess that
         * an unanswered chunk failed to commit. Protocol/auth errors fail. */
        if (reused && (ret == -EIO || ret == -ECONNRESET || ret == -EPIPE ||
                       ret == -ENOTCONN) && !check_cancel()) {
            https_stream_close(*stream); *stream = NULL;
            atomic_set(&stage, TLS);
            upload_perf_begin(UP_TLS, session, segment, 0, 0);
            ret = tls_connect(stream);
            upload_perf_end(ret);
            if (!ret) { atomic_set(&tls_step, TLS_SETUP_HTTP); ret = upload(*stream); }
        }
    }
#endif
    upload_perf_begin(UP_CLEANUP, session, segment, atomic_get(&confirmed), file_length);
    struct k_work_sync sync;
    k_work_cancel_delayable_sync(&stall_work, &sync);
    if (opened) {
        int closed = fs_close(&file);
        if (closed) { atomic_set(&cleanup_error, closed); }
        if (!ret) { ret = closed; }
    }
    if (atomic_get(&cancelled)) { ret = -ECANCELED; }
    else if (atomic_get(&stalled)) { ret = -ETIMEDOUT; }
    if (state_ready) {
        stamp.confirmed = atomic_get(&confirmed);
        stamp.last_error = ret;
        if (stamp.confirmed == stamp.size) {
            stamp.state = UPLOAD_STORED; stamp.last_error = 0;
        } else if (stamp.confirmed) { stamp.state = UPLOAD_PARTIAL; }
        else { stamp.state = ret == -ECANCELED ? UPLOAD_PENDING : UPLOAD_FAILED; }
        int saved = atomic_get(&cleanup_error);
        if (!saved) {
            uint64_t measured = upload_perf_now();
            saved = upload_state_save(session, segment, &stamp);
            upload_perf_add(UP_STATE_US, upload_perf_now() - measured);
        }
        atomic_set(&state_error, saved);
        /* Never release a queued recording until file handles and metadata
         * cleanup are known to have completed. */
        if (saved && !atomic_get(&cleanup_error)) { atomic_set(&cleanup_error, saved); }
        if (!ret && saved) { ret = saved; }
    }
#ifndef CONFIG_CLIP_DTLS_UPLOAD
    if (!ret && !response_keep_alive) {
        https_stream_close(*stream); *stream = NULL;
    }
#endif
    upload_perf_end(ret);
    return ret;
}

static void worker(void *a, void *b, void *c)
{
    ARG_UNUSED(a); ARG_UNUSED(b); ARG_UNUSED(c);
    for (;;) {
        if (k_sem_take(&start_job, K_SECONDS(1)) && !begin_auto()) { continue; }
        struct https_stream *stream = NULL;
#ifdef CONFIG_CLIP_DTLS_UPLOAD
        clip_cpu_boost_acquire();
#endif
        buffers = k_malloc(sizeof(*buffers));
        int ret = buffers ? 0 : -ENOMEM;
        if (!ret) {
            for (;;) {
                ret = upload_file(&stream);
                if (ret || !atomic_get(&auto_job) || check_cancel()) { break; }
                if (next_auto_file()) { continue; }
                /* The window bounds RAM, not the connection lifetime. Keep
                 * uploading serially over this connection until no work remains. */
                deadline = k_uptime_get() + 300000;
                int found = refill_auto_batch();
                if (found < 0) {
                    ret = found;
                    if (ret != -ECANCELED) { atomic_set(&cleanup_error, ret); }
                    break;
                }
                if (!found || !next_auto_file()) { break; }
            }
        }
        struct k_work_sync sync;
        k_work_cancel_delayable_sync(&stall_work, &sync);
#ifdef CONFIG_CLIP_DTLS_UPLOAD
        dtls_upload_close(datagram); datagram = NULL;
#else
        https_stream_close(stream);
#endif
        int radio = wifi_off();
        if (!atomic_get(&cleanup_error)) { atomic_set(&cleanup_error, radio); }
        k_free(buffers); buffers = NULL;
#ifdef CONFIG_CLIP_DTLS_UPLOAD
        clip_cpu_boost_release();
#endif
        if (!ret) { ret = atomic_get(&cleanup_error); }
        if (atomic_get(&cancelled)) { ret = -ECANCELED; }
        else if (atomic_get(&stalled)) { ret = -ETIMEDOUT; }
        atomic_set(&last_error, ret);
        atomic_set(&stage, ret == -ECANCELED ? CANCELLED : ret ? FAILED : SUCCEEDED);
        schedule_after_job(ret);
        struct clip_event_result_info result;
        while (clip_post_event_sync(CLIP_EVENT_UPLOAD_END, &result)) {
            k_sleep(K_MSEC(10));
        }
    }
}
/* Full ECDSA handshakes measured 5816 B; 6 KiB left only 328 B. */
#ifdef CONFIG_CLIP_DTLS_UPLOAD
K_THREAD_DEFINE(https_worker, 12288, worker, NULL, NULL, NULL, 7, 0, 0);
#else
K_THREAD_DEFINE(https_worker, 8192, worker, NULL, NULL, NULL, 7, 0, 0);
#endif

static int configure_network(char *args)
{
    char *split = strchr(args, ',');
    if (!split) { return -EINVAL; }
    *split++ = 0;
    if (!strlen(args) || strlen(args) > 32 || strlen(split) < 8 || strlen(split) > 63) {
        return -EINVAL;
    }
    strcpy(ssid, args);
    strcpy(password, split);
    return 0;
}

static int configure_receiver(char *args)
{
    char *fields[4], *cursor = args;
    for (int i = 0; i < 4; i++) { fields[i] = split_field(&cursor); }
    if (cursor || !fields[3] || !valid_id(fields[2], 32) ||
        !valid_id(fields[3], 96) || strlen(fields[3]) < 32 ||
        !fields[0][0] || strlen(fields[0]) >= sizeof(host)) { return -EINVAL; }
    for (char *p = fields[0]; *p; p++) {
        if (!isalnum((unsigned char)*p) && *p != '.' && *p != '-') { return -EINVAL; }
    }
    char *end;
    unsigned long n = strtoul(fields[1], &end, 10);
    if (!fields[1][0] || *end || n < 1 || n > 65535 || strlen(fields[1]) > 5) {
        return -EINVAL;
    }
    strcpy(host, fields[0]);
    strcpy(port, fields[1]);
    strcpy(device_id, fields[2]);
    strcpy(token, fields[3]);
    return 0;
}

static int set_upload_time(const char *args)
{
    char *end;
    long long timestamp = strtoll(args, &end, 10);
    if (*end || timestamp < 1700000000LL || timestamp > 4102444800LL) { return -EINVAL; }
    struct timespec ts = {.tv_sec = timestamp, .tv_nsec = 0};
    int ret = clock_settime(CLOCK_REALTIME, &ts) ? -errno : 0;
    atomic_set(&time_ready, ret == 0);
    if (!ret) { time_synced_at = k_uptime_get(); }
    return ret;
}

static int start_upload(char *args)
{
    char *split = strchr(args, ':');
    if (!split) { return -EINVAL; }
    *split++ = 0;
    char *end;
    unsigned long n = strtoul(split, &end, 10);
    if (!valid_session(args) || !*split || *end || !n || n > 999999999 ||
        !ssid[0] || !host[0]) { return -EINVAL; }
    struct clip_event_result_info result;
    int ret = clip_post_event_sync(CLIP_EVENT_UPLOAD_BEGIN, &result);
    if (ret || result.result != CLIP_EVENT_OK) { return -EBUSY; }
    atomic_clear(&auto_job);
    strcpy(session, args);
    segment = n;
    wifi_diag_reset();
    k_sem_give(&start_job);
    return 0;
}

static int command_locked(struct at_cmd_ctx *ctx, char *response, size_t length)
{
    int ret = 0;
    char args[256] = {0};
    if (ctx->transport_type != TRANSPORT_TYPE_USB) { ret = -EACCES; goto reply; }
#ifdef CONFIG_CLIP_TCP_TRACE
    if (!strcmp(ctx->name, "TCPTRACE")) {
        ret = clip_tcp_trace_command(ctx->args, https_poc_active() || https_poc_record_pending(), response, length);
        if (ret >= 0) { return ret; }
        goto reply;
    }
#endif
#ifdef CONFIG_CLIP_HTTPS_PROFILE
    if (!strcmp(ctx->name, "UPLOADPERF")) {
        ret = upload_perf_command(ctx->args, https_poc_active() || https_poc_record_pending(), response, length);
        if (ret >= 0) { return ret; }
        goto reply;
    }
#endif
#ifdef CONFIG_CLIP_DTLS_UPLOAD
    if (!strcmp(ctx->name, "UPLOADNET")) {
        return snprintf(response, length,
            "{\"ok\":true,\"data\":{\"protocol\":3,\"elapsed_ms\":%u,\"packets\":%u,"
            "\"retransmissions\":%u,\"receipts\":%u,\"received\":%u,\"durable\":%u,"
            "\"resumed\":%u,\"rtt_ms\":%u,\"rto_ms\":%u,\"window_packets\":%u}}\n",
            last_datagram.elapsed_ms, last_datagram.sent_packets, last_datagram.retransmissions,
            last_datagram.receipts, last_datagram.received, last_datagram.durable,
            last_datagram.resume_from, last_datagram.rtt_ms, last_datagram.rto_ms, last_datagram.window_packets);
    }
#endif
    if (!strcmp(ctx->name, "UPLOAD") && ctx->type != AT_CMD_TYPE_SET) { goto reply; }
    if (!strcmp(ctx->name, "UPLOADAUTO") && ctx->type != AT_CMD_TYPE_SET) {
        return snprintf(response, length,
            "{\"ok\":true,\"data\":{\"enabled\":%s,\"paused\":%s,\"config_saved\":%s,"
            "\"config_error\":%d,\"scan_error\":%d,\"time_ready\":%s,\"auto_active\":%s,"
            "\"failures\":%u,\"next_attempt_ms\":%u}}\n",
            settings.enabled ? "true" : "false", atomic_get(&auto_paused) ? "true" : "false",
            config_saved ? "true" : "false", config_error, scan_error,
            atomic_get(&time_ready) ? "true" : "false", atomic_get(&auto_job) ? "true" : "false",
            (unsigned int)failures, (unsigned int)MAX(0, next_auto - k_uptime_get()));
    }
    if (!strcmp(ctx->name, "UPLOADAUTO") && ctx->args && !strcmp(ctx->args, "off")) {
        settings.enabled = 0; atomic_set(&auto_paused, 1);
        if (atomic_get(&auto_job)) { request_cancel(); }
        ret = upload_config_save(&settings); config_error = ret; config_saved = ret == 0;
        goto reply;
    }
    if (!strcmp(ctx->name, "UPLOADCANCEL")) {
        atomic_set(&auto_paused, 1); request_cancel(); goto reply;
    }
    if (https_poc_active() || https_poc_record_pending()) { ret = -EBUSY; goto reply; }
    if (!strcmp(ctx->name, "UPLOADSAVE")) {
        ret = upload_config_save(&settings); config_error = ret; config_saved = ret == 0; goto reply;
    }
    if (!strcmp(ctx->name, "UPLOADCLEAR")) {
        atomic_set(&auto_paused, 1); settings.enabled = 0;
        ret = upload_config_clear(); config_error = ret; config_saved = false;
        if (!ret) {
            memset(&settings, 0, sizeof(settings));
            strcpy(settings.clock_host, "time.cloudflare.com"); strcpy(settings.clock_port, "123");
            cursor_session[0] = 0; cursor_segment = 0; atomic_clear(&time_ready);
        }
        goto reply;
    }
    if (!ctx->args) { ret = -EINVAL; goto reply; }
    if (strlen(ctx->args) >= sizeof(args)) { ret = -EINVAL; goto reply; }
    strcpy(args, ctx->args);
    if ((!strcmp(ctx->name, "NETCFG") || !strcmp(ctx->name, "UPLOADCFG") ||
         !strcmp(ctx->name, "UPLOADCLOCK")) && settings.enabled) { ret = -EBUSY; goto reply; }
    if (!strcmp(ctx->name, "UPLOADAUTO")) {
        if (strcmp(args, "on")) { ret = -EINVAL; goto reply; }
        settings.enabled = 1; ret = upload_config_save(&settings); config_error = ret;
        if (ret) { settings.enabled = 0; config_saved = false; }
        else {
            config_saved = true; atomic_clear(&auto_paused); failures = 0;
            next_auto = k_uptime_get() + 5000; cursor_session[0] = 0; cursor_segment = 0;
        }
    } else if (!strcmp(ctx->name, "UPLOADCLOCK")) {
        char *split = strchr(args, ',');
        if (!split) { ret = -EINVAL; goto reply; }
        *split++ = 0;
        struct upload_config candidate = settings;
        if (strlen(args) >= sizeof(candidate.clock_host) || strlen(split) >= sizeof(candidate.clock_port)) {
            ret = -EINVAL; goto reply;
        }
        strcpy(candidate.clock_host, args); strcpy(candidate.clock_port, split);
        if (!upload_config_valid(&candidate)) { ret = -EINVAL; }
        else {
            strcpy(settings.clock_host, args); strcpy(settings.clock_port, split);
            config_saved = false; atomic_clear(&time_ready);
        }
        memset(&candidate, 0, sizeof(candidate));
    } else if (!strcmp(ctx->name, "NETCFG")) {
        ret = configure_network(args); if (!ret) { config_saved = false; }
    } else if (!strcmp(ctx->name, "UPLOADCFG")) {
        ret = configure_receiver(args); if (!ret) { config_saved = false; }
    } else if (!strcmp(ctx->name, "UPTIME")) {
        ret = set_upload_time(args);
    } else if (!strcmp(ctx->name, "UPLOAD")) {
        ret = start_upload(args);
    } else if (!strcmp(ctx->name, "UPLOADLIST")) {
        ret = list_states(args, response, length);
        if (ret >= 0) { memset(args, 0, sizeof(args)); return ret; }
    } else if (!strcmp(ctx->name, "UPLOADSTATE")) {
        ret = state_query(args, response, length);
        if (ret >= 0) { memset(args, 0, sizeof(args)); return ret; }
    } else { ret = -ENOTSUP; }
reply:
    memset(args, 0, sizeof(args));
    struct wifi_station_network network;
    wifi_station_get_network(&network);
    return snprintf(response, length,
        "{\"ok\":%s,\"error\":%d,\"stage\":\"%s\",\"active\":%s,\"last_error\":%d,\"sent\":%d,\"confirmed\":%d,\"resume_from\":%d,\"size\":%d,\"state_error\":%d,"
#ifdef CONFIG_CLIP_DTLS_UPLOAD
        "\"protocol\":3,\"received\":%d,"
#else
        "\"protocol\":2,"
#endif
        "\"wifi_step\":\"%s\",\"tls_step\":\"%s\",\"wifi_ip\":\"%s\",\"wifi_netmask\":\"%s\",\"wifi_gateway\":\"%s\"}\n",
        ret ? "false" : "true", ret, names[atomic_get(&stage)],
        https_poc_active() ? "true" : "false", (int)atomic_get(&last_error), (int)atomic_get(&bytes_sent),
        (int)atomic_get(&confirmed), (int)atomic_get(&resume_from), (int)atomic_get(&total_bytes),
        (int)atomic_get(&state_error),
#ifdef CONFIG_CLIP_DTLS_UPLOAD
        (int)atomic_get(&datagram_received),
#endif
        wifi_station_last_step(), tls_steps[atomic_get(&tls_step)],
        network.ip, network.netmask, network.gateway);
}

static int command(struct at_cmd_ctx *ctx, char *response, size_t length)
{
    k_mutex_lock(&control_lock, K_FOREVER);
    int ret = command_locked(ctx, response, length);
    k_mutex_unlock(&control_lock);
    return ret;
}

int https_poc_register(void)
{
    configuration = k_calloc(1, sizeof(*configuration));
    if (!configuration) { return -ENOMEM; }
    int ret;
    static const struct at_command commands[] = {
        {"NETCFG", AT_CMD_SET, command}, {"UPLOADCFG", AT_CMD_SET, command},
        {"UPTIME", AT_CMD_SET, command}, {"UPLOAD", AT_CMD_SET | AT_CMD_QUERY, command},
        {"UPLOADCANCEL", AT_CMD_EXEC, command},
        {"UPLOADSTATE", AT_CMD_SET, command}, {"UPLOADLIST", AT_CMD_SET, command},
        {"UPLOADAUTO", AT_CMD_SET | AT_CMD_QUERY, command},
        {"UPLOADSAVE", AT_CMD_EXEC, command}, {"UPLOADCLEAR", AT_CMD_EXEC, command},
        {"UPLOADCLOCK", AT_CMD_SET, command},
#ifdef CONFIG_CLIP_DTLS_UPLOAD
        {"UPLOADNET", AT_CMD_QUERY, command},
#endif
#ifdef CONFIG_CLIP_TCP_TRACE
        {"TCPTRACE", AT_CMD_SET, command},
#endif
#ifdef CONFIG_CLIP_HTTPS_PROFILE
        {"UPLOADPERF", AT_CMD_SET, command},
#endif
    };
    for (size_t i = 0; i < ARRAY_SIZE(commands); i++) {
        ret = at_server_register_cmd(&commands[i]);
        if (ret) { return ret; }
    }
    ret = upload_config_init(&settings);
    config_error = ret; config_saved = !ret && ssid[0];
    if (!settings.clock_host[0]) {
        strcpy(settings.clock_host, "time.cloudflare.com"); strcpy(settings.clock_port, "123");
    }
    next_auto = k_uptime_get() + 10000;
    atomic_set(&initialized, 1);
    return wifi_diag_register();
}
