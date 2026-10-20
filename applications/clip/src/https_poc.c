/* Single-job HTTPS experiment. No local deletion or persistent credentials. */
#include <zephyr/kernel.h>
#include <zephyr/net/socket.h>
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
#include "https_stream.h"
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
static atomic_t stalled;
static atomic_t record_pending, record_error, cleanup_error;
enum { TLS_SETUP_IDLE, TLS_SETUP_DNS, TLS_SETUP_SOCKET, TLS_SETUP_CERTIFICATE,
       TLS_SETUP_VERIFY, TLS_SETUP_HOSTNAME, TLS_SETUP_SEND_TIMEOUT,
       TLS_SETUP_RECEIVE_TIMEOUT, TLS_SETUP_HANDSHAKE, TLS_SETUP_HTTP };
static atomic_t tls_step;
static const char *const tls_steps[] = {"idle", "resolve", "socket", "certificate",
    "peer_verify", "hostname", "send_timeout", "receive_timeout", "handshake", "http"};
static K_SEM_DEFINE(start_job, 0, 1);
static char ssid[33], password[64], host[96], port[6], device_id[33], token[97];
static char session[32];
static uint32_t segment;
static bool time_ready;
static int64_t deadline;
static struct fs_file_t file;
/* Allocate only during a job, from the same heap as TLS. This saves static
 * reservation, not peak upload memory; allocation failure is a normal error. */
static struct upload_buffers {
    uint8_t file_io[2048], receive[2048];
    char body[1025];
} *buffers;
#define io (buffers->file_io)
#define recv_buffer (buffers->receive)
#define response_body (buffers->body)
static char expected_hash[65];
static size_t response_length, file_length, header_bytes;
static bool response_final;
static int response_status;

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
    if (IS_ENABLED(CONFIG_CLIP_WIFI_DIAG) &&
        (!strcmp(name, "NETDIAG") || !strcmp(name, "NETTRACE") || !strcmp(name, "NETTX"))) {
        return true;
    }
    /* A deliberately small control surface also excludes deferred erase/reboot
     * commands which could outlive the AT handler and race a subsequent job. */
    if (https_poc_active() || https_poc_record_pending()) {
        return !strcmp(name, "UPLOAD") || !strcmp(name, "UPLOADCANCEL") ||
               !strcmp(name, "START") || !strcmp(name, "STOP") ||
               !strcmp(name, "GSTAT") || !strcmp(name, "BATT") || !strcmp(name, "VERSION");
    }
    return !strcmp(name, "NETCFG") || !strcmp(name, "UPLOADCFG") ||
           !strcmp(name, "UPTIME") || !strcmp(name, "UPLOAD") ||
           !strcmp(name, "UPLOADCANCEL") || !strcmp(name, "USB") ||
           !strcmp(name, "GSTAT") || !strcmp(name, "BATT") ||
           !strcmp(name, "VERSION") || !strcmp(name, "LIST") ||
           !strcmp(name, "START") || !strcmp(name, "STOP") ||
           !strcmp(name, "TIME") || !strcmp(name, "STORAGE");
}

bool https_poc_event(enum clip_event event, struct clip_event_result_info *result,
                     bool device_idle)
{
    enum clip_event_result ret = CLIP_EVENT_OK;
    int error = 0;
    if (event == CLIP_EVENT_UPLOAD_BEGIN) {
        if (!device_idle || https_poc_record_pending() || !atomic_cas(&active, 0, 1)) {
            ret = CLIP_EVENT_BUSY;
        } else {
            /* Initialize in event order. Clearing after BEGIN returns could
             * erase a button cancellation arriving before the worker wakes. */
            atomic_clear(&cancelled); atomic_clear(&stalled);
            atomic_clear(&last_error); atomic_clear(&bytes_sent);
            atomic_clear(&confirmed); atomic_clear(&resume_from); atomic_clear(&total_bytes);
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
        ssize_t n = fs_read(&file, io, MIN(sizeof(io), length - total));
        if (n <= 0) { ret = n < 0 ? (int)n : -EIO; break; }
        total += n;
        ret = mbedtls_sha256_update(&hash, io, n);
    }
    if (!ret) { ret = mbedtls_sha256_finish(&hash, digest); }
    mbedtls_sha256_free(&hash);
    if (ret) { return ret; }
    for (int i = 0; i < 32; i++) { snprintf(output + i * 2, 3, "%02x", digest[i]); }
    return fs_seek(&file, offset, FS_SEEK_SET);
}

static int hash_file(void) { return hash_file_range(0, file_length, expected_hash); }

static int send_payload(struct https_stream *stream, size_t offset, size_t length)
{
    size_t total = 0;
    atomic_set(&stage, SENDING);
    while (total < length) {
        int ret = check_cancel();
        if (ret) { return ret; }
        ssize_t n = fs_read(&file, io, MIN(sizeof(io), length - total));
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
        if (!count && !response_final) { return -EBADMSG; }
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

static int request_chunk(struct https_stream *stream, bool query, size_t offset, size_t length)
{
    char chunk_header[90] = {0};
    if (!query) {
        char digest[65];
        int ret = hash_file_range(offset, length, digest);
        if (ret) { return ret; }
        snprintf(chunk_header, sizeof(chunk_header), "X-Chunk-SHA256: %s\r\n", digest);
    }
    int n = snprintf((char *)io, sizeof(io),
        "%s /v2/devices/%s/recordings/%s/%u HTTP/1.1\r\n"
        "Host: %s:%s\r\nAuthorization: Bearer %s\r\n"
        "X-Recording-Size: %u\r\nX-Content-SHA256: %s\r\n"
        "Upload-Offset: %u\r\n%sConnection: keep-alive\r\n"
        "Content-Type: application/octet-stream\r\nContent-Length: %u\r\n\r\n",
        query ? "GET" : "PUT", device_id, session, segment, host, port, token,
        (unsigned int)file_length, expected_hash, (unsigned int)offset, chunk_header,
        (unsigned int)length);
    if (n < 0 || (size_t)n >= sizeof(io)) { return -EMSGSIZE; }
    progress();
    int ret = https_stream_write(stream, io, n);
    memset(io, 0, sizeof(io));
    if (ret) { return ret; }
    if (!query) {
        /* Hashing rewound the file; request formatting only reused the buffer. */
        ret = send_payload(stream, offset, length);
        if (ret < 0) { return ret; }
    }
    return receive_receipt(stream, query ? -1 : (int)(offset + length));
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

static void worker(void *a, void *b, void *c)
{
    ARG_UNUSED(a); ARG_UNUSED(b); ARG_UNUSED(c);
    for (;;) {
        k_sem_take(&start_job, K_FOREVER);
        atomic_set(&tls_step, TLS_SETUP_IDLE);
        deadline = k_uptime_get() + 300000;
        atomic_set(&stage, PREPARING);
        char path[STORAGE_PATH_MAX];
        buffers = k_malloc(sizeof(*buffers));
        int ret = buffers ? storage_ensure_mounted() : -ENOMEM;
        bool opened = false;
        struct https_stream *stream = NULL;
        struct fs_dirent entry;
        if (!ret) { ret = storage_build_chunk_path(session, segment, path, sizeof(path)); }
        if (!ret) { ret = fs_stat(path, &entry); }
        if (!ret && (entry.type != FS_DIR_ENTRY_FILE || !entry.size || entry.size > MAX_FILE)) { ret = -EINVAL; }
        fs_file_t_init(&file);
        if (!ret) { ret = fs_open(&file, path, FS_O_READ); opened = ret == 0; }
        if (!ret) {
            file_length = entry.size;
            atomic_set(&total_bytes, file_length);
            ret = hash_file();
        }
        if (!ret) {
            atomic_set(&stage, CONNECTING);
            ret = wifi_station_connect(ssid, password, &cancelled);
        }
        if (!ret) {
            atomic_set(&stage, TLS);
            ret = tls_connect(&stream);
        }
        if (!ret) { atomic_set(&tls_step, TLS_SETUP_HTTP); ret = upload(stream); }
        struct k_work_sync sync;
        k_work_cancel_delayable_sync(&stall_work, &sync);
        https_stream_close(stream);
        atomic_set(&cleanup_error, wifi_off());
        if (opened) {
            int close_ret = fs_close(&file);
            if (!atomic_get(&cleanup_error)) { atomic_set(&cleanup_error, close_ret); }
        }
        k_free(buffers);
        buffers = NULL;
        if (atomic_get(&cancelled)) { ret = -ECANCELED; }
        else if (atomic_get(&stalled)) { ret = -ETIMEDOUT; }
        atomic_set(&last_error, ret);
        atomic_set(&stage, ret == -ECANCELED ? CANCELLED : ret ? FAILED : SUCCEEDED);
        struct clip_event_result_info result;
        /* A temporarily full event queue must not strand the storage claim. */
        while (clip_post_event_sync(CLIP_EVENT_UPLOAD_END, &result)) {
            k_sleep(K_MSEC(10));
        }
    }
}
/* Full ECDSA handshakes measured 5816 B; 6 KiB left only 328 B. */
K_THREAD_DEFINE(https_worker, 8192, worker, NULL, NULL, NULL, 7, 0, 0);

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
    time_ready = ret == 0;
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
        !ssid[0] || !host[0] || !time_ready) { return -EINVAL; }
    struct clip_event_result_info result;
    int ret = clip_post_event_sync(CLIP_EVENT_UPLOAD_BEGIN, &result);
    if (ret || result.result != CLIP_EVENT_OK) { return -EBUSY; }
    strcpy(session, args);
    segment = n;
    wifi_diag_reset();
    k_sem_give(&start_job);
    return 0;
}

static int command(struct at_cmd_ctx *ctx, char *response, size_t length)
{
    int ret = 0;
    char args[256] = {0};
    if (ctx->transport_type != TRANSPORT_TYPE_USB) { ret = -EACCES; goto reply; }
    if (!strcmp(ctx->name, "UPLOAD") && ctx->type != AT_CMD_TYPE_SET) { goto reply; }
    if (!strcmp(ctx->name, "UPLOADCANCEL")) {
        request_cancel(); goto reply;
    }
    if (https_poc_active() || https_poc_record_pending()) { ret = -EBUSY; goto reply; }
    if (!ctx->args) { ret = -EINVAL; goto reply; }
    if (strlen(ctx->args) >= sizeof(args)) { ret = -EINVAL; goto reply; }
    strcpy(args, ctx->args);
    if (!strcmp(ctx->name, "NETCFG")) {
        ret = configure_network(args);
    } else if (!strcmp(ctx->name, "UPLOADCFG")) {
        ret = configure_receiver(args);
    } else if (!strcmp(ctx->name, "UPTIME")) {
        ret = set_upload_time(args);
    } else if (!strcmp(ctx->name, "UPLOAD")) {
        ret = start_upload(args);
    } else { ret = -ENOTSUP; }
reply:
    memset(args, 0, sizeof(args));
    struct wifi_station_network network;
    wifi_station_get_network(&network);
    return snprintf(response, length,
        "{\"ok\":%s,\"error\":%d,\"stage\":\"%s\",\"active\":%s,\"last_error\":%d,\"sent\":%d,\"confirmed\":%d,\"resume_from\":%d,\"size\":%d,\"protocol\":2,\"wifi_step\":\"%s\",\"tls_step\":\"%s\",\"wifi_ip\":\"%s\",\"wifi_netmask\":\"%s\",\"wifi_gateway\":\"%s\"}\n",
        ret ? "false" : "true", ret, names[atomic_get(&stage)],
        https_poc_active() ? "true" : "false", (int)atomic_get(&last_error), (int)atomic_get(&bytes_sent),
        (int)atomic_get(&confirmed), (int)atomic_get(&resume_from), (int)atomic_get(&total_bytes),
        wifi_station_last_step(), tls_steps[atomic_get(&tls_step)],
        network.ip, network.netmask, network.gateway);
}

int https_poc_register(void)
{
    int ret;
    static const struct at_command commands[] = {
        {"NETCFG", AT_CMD_SET, command}, {"UPLOADCFG", AT_CMD_SET, command},
        {"UPTIME", AT_CMD_SET, command}, {"UPLOAD", AT_CMD_SET | AT_CMD_QUERY, command},
        {"UPLOADCANCEL", AT_CMD_EXEC, command},
    };
    for (size_t i = 0; i < ARRAY_SIZE(commands); i++) {
        ret = at_server_register_cmd(&commands[i]);
        if (ret) { return ret; }
    }
    return wifi_diag_register();
}
