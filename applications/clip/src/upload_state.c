/* Two fixed-size CRC slots on SD. No rename or new RAM reservation required.
 * Reuses the storage session-metadata lock; never holds it across network I/O.
 * This detects torn records, not malicious SD modification or physical media
 * guarantees beyond fs_sync(). The next upload always queries server offset. */
#include "upload_state.h"
#include "storage.h"
#include <zephyr/sys/crc.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>

#define STATE_MAGIC 0x31505548U
#define STATE_VERSION 1U
BUILD_ASSERT(sizeof(struct upload_file_record) == 96 &&
             offsetof(struct upload_file_record, crc) == 92, "Upload state layout changed");
BUILD_ASSERT(__BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__, "Upload state v1 uses little endian");

const char *upload_state_name(uint32_t state)
{
    static const char *const names[] = {"pending", "partial", "uploaded", "failed"};
    return state <= UPLOAD_FAILED ? names[state] : "pending";
}

static int slot_path(const char *session, uint32_t segment, unsigned int slot,
                      char path[STORAGE_PATH_MAX])
{
    if (!segment || segment > 999999999 || slot > 1) { return -EINVAL; }
    int ret = storage_build_session_metadata_path(session, path, STORAGE_PATH_MAX);
    if (ret) { return ret; }
    char *tail = strrchr(path, '/');
    if (!tail) { return -EINVAL; }
    tail++;
    size_t remaining = STORAGE_PATH_MAX - (tail - path);
    int n = snprintf(tail, remaining, "http-%u.%c", (unsigned int)segment, slot ? 'b' : 'a');
    return n < 0 || (size_t)n >= remaining ? -ENAMETOOLONG : 0;
}

static bool valid_record(const struct upload_file_record *r)
{
    if (r->magic != STATE_MAGIC || r->version != STATE_VERSION ||
        !r->size || r->size > 8U * 1024U * 1024U || r->confirmed > r->size ||
        r->state > UPLOAD_FAILED ||
        (r->state == UPLOAD_STORED && (r->confirmed != r->size || r->last_error)) ||
        (r->state == UPLOAD_PARTIAL && (!r->confirmed || r->confirmed == r->size)) ||
        ((r->state == UPLOAD_PENDING || r->state == UPLOAD_FAILED) && r->confirmed)) { return false; }
    return r->crc == crc32_ieee((const uint8_t *)r, offsetof(struct upload_file_record, crc));
}

static int read_slot(const char *session, uint32_t segment, unsigned int slot,
                      struct upload_file_record *out)
{
    char path[STORAGE_PATH_MAX];
    int ret = slot_path(session, segment, slot, path);
    if (ret) { return ret; }
    struct fs_file_t file;
    fs_file_t_init(&file);
    ret = fs_open(&file, path, FS_O_READ);
    if (ret) { return ret; }
    ssize_t n = fs_read(&file, out, sizeof(*out));
    uint8_t extra;
    ssize_t tail = n == (ssize_t)sizeof(*out) ? fs_read(&file, &extra, 1) : 0;
    int closed = fs_close(&file);
    if (n < 0) { return n; }
    if (tail < 0) { return tail; }
    if (closed) { return closed; }
    return n == (ssize_t)sizeof(*out) && !tail && valid_record(out) ? 0 : -EBADMSG;
}

static int load_locked(const char *session, uint32_t segment,
                        struct upload_file_record *out, unsigned int *selected)
{
    struct upload_file_record a, b;
    int ra = read_slot(session, segment, 0, &a);
    int rb = read_slot(session, segment, 1, &b);
    if (!ra && !rb && a.generation == b.generation && memcmp(&a, &b, sizeof(a))) {
        return -EIO;
    }
    if (!ra && (rb || (int32_t)(a.generation - b.generation) >= 0)) {
        *out = a; *selected = 0; return 0;
    }
    if (!rb) { *out = b; *selected = 1; return 0; }
    if (ra == -ENOENT && rb == -ENOENT) { return -ENOENT; }
    if (ra != -ENOENT && ra != -EBADMSG) { return ra; }
    if (rb != -ENOENT && rb != -EBADMSG) { return rb; }
    return -EBADMSG;
}

int upload_state_load(const char *session, uint32_t segment, struct upload_file_record *out)
{
    int ret = storage_ensure_mounted();
    if (ret) { return ret; }
    unsigned int selected;
    storage_session_json_lock();
    ret = load_locked(session, segment, out, &selected);
    storage_session_json_unlock();
    return ret;
}

int upload_state_save(const char *session, uint32_t segment, const struct upload_file_record *record)
{
    int ret = storage_ensure_mounted();
    if (ret) { return ret; }
    struct upload_file_record previous, next = *record;
    unsigned int selected = 1;
    storage_session_json_lock();
    ret = load_locked(session, segment, &previous, &selected);
    if (ret && ret != -ENOENT && ret != -EBADMSG) { goto done; }
    next.magic = STATE_MAGIC; next.version = STATE_VERSION;
    next.generation = ret ? 1 : previous.generation + 1;
    next.crc = crc32_ieee((const uint8_t *)&next, offsetof(struct upload_file_record, crc));
    if (!valid_record(&next)) { ret = -EINVAL; goto done; }
    char path[STORAGE_PATH_MAX];
    ret = slot_path(session, segment, selected ^ 1U, path);
    if (ret) { goto done; }
    struct fs_file_t file;
    fs_file_t_init(&file);
    ret = fs_open(&file, path, FS_O_CREATE | FS_O_WRITE | FS_O_TRUNC);
    if (ret) { goto done; }
    ssize_t n = fs_write(&file, &next, sizeof(next));
    ret = n < 0 ? (int)n : n != (ssize_t)sizeof(next) ? -EIO : fs_sync(&file);
    int closed = fs_close(&file);
    if (!ret) { ret = closed; }
done:
    storage_session_json_unlock();
    return ret;
}
