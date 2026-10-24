/* One recording writer, one incremental SHA context, no payload allocation.
 * A sidecar is published only after successful data sync and close. It is a
 * disposable cache: missing/torn/mismatched records require a full file hash.
 * CRC detects torn metadata, not malicious media edits. Server SHA is final.
 */
#include "recording_hash.h"
#include "storage.h"
#include <mbedtls/sha256.h>
#include <zephyr/sys/crc.h>
#include <stddef.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

#define HASH_MAGIC 0x31485352U
#define HASH_MAX_SIZE (8U * 1024U * 1024U)
struct hash_record {
    uint32_t magic, version, segment, size;
    char session[14];
    uint8_t reserved[2], digest[32];
    uint32_t crc;
};
BUILD_ASSERT(sizeof(struct hash_record) == 68 && offsetof(struct hash_record, crc) == 64,
             "Recording hash v1 layout changed");
BUILD_ASSERT(__BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__, "Recording hash v1 is little endian");
static struct {
    mbedtls_sha256_context sha;
    char session[15];
    uint32_t segment, size;
    bool active;
} writer;

static bool identity_valid(const char *session, uint32_t segment)
{
    if (!session || strlen(session) != 14 || !segment || segment > 999999999) { return false; }
    for (unsigned int i = 0; i < 14; i++) {
        if (session[i] < '0' || session[i] > '9') { return false; }
    }
    return true;
}

static int path_for(const char *session, uint32_t segment, char path[STORAGE_PATH_MAX])
{
    if (!identity_valid(session, segment)) { return -EINVAL; }
    int ret = storage_build_session_metadata_path(session, path, STORAGE_PATH_MAX);
    if (ret) { return ret; }
    char *tail = strrchr(path, '/');
    if (!tail) { return -EINVAL; }
    tail++;
    size_t remaining = STORAGE_PATH_MAX - (tail - path);
    int n = snprintf(tail, remaining, "sha-%u", (unsigned int)segment);
    return n < 0 || (size_t)n >= remaining ? -ENAMETOOLONG : 0;
}

int recording_hash_load(const char *session, uint32_t segment, uint32_t size, uint8_t digest[32])
{
    char path[STORAGE_PATH_MAX], filename[32];
    if (!digest || !size || size > HASH_MAX_SIZE) { return -EINVAL; }
    int ret = path_for(session, segment, path);
    if (ret) { return ret; }
    snprintf(filename, sizeof(filename), "%04u.opus", (unsigned int)segment);
    if (storage_file_is_writing(session, filename)) { return -EBUSY; }
    struct hash_record record;
    struct fs_file_t file;
    fs_file_t_init(&file);
    storage_session_json_lock();
    ret = fs_open(&file, path, FS_O_READ);
    if (ret) { goto done; }
    ssize_t n = fs_read(&file, &record, sizeof(record));
    uint8_t extra;
    ssize_t tail = n == (ssize_t)sizeof(record) ? fs_read(&file, &extra, 1) : 0;
    int closed = fs_close(&file);
    if (n < 0 || tail < 0 || closed) { ret = n < 0 ? (int)n : tail < 0 ? (int)tail : closed; goto done; }
    if (n != (ssize_t)sizeof(record) || tail || record.magic != HASH_MAGIC || record.version != 1 ||
        record.segment != segment || record.size != size || memcmp(record.session, session, 14) ||
        record.reserved[0] || record.reserved[1] ||
        record.crc != crc32_ieee((const uint8_t *)&record, offsetof(struct hash_record, crc))) {
        ret = -EBADMSG; goto done;
    }
    memcpy(digest, record.digest, 32);
    ret = 0;
done:
    storage_session_json_unlock();
    return ret;
}

int recording_hash_save(const char *session, uint32_t segment, uint32_t size, const uint8_t digest[32])
{
    if (!digest || !size || size > HASH_MAX_SIZE) { return -EINVAL; }
    char path[STORAGE_PATH_MAX], data_path[STORAGE_PATH_MAX];
    int ret = path_for(session, segment, path);
    if (ret) { return ret; }
    ret = storage_build_chunk_path(session, segment, data_path, sizeof(data_path));
    if (ret) { return ret; }
    struct fs_dirent entry;
    ret = fs_stat(data_path, &entry);
    if (ret) { return ret; }
    if (entry.type != FS_DIR_ENTRY_FILE || entry.size != size) { return -EINVAL; }
    struct hash_record record = {.magic = HASH_MAGIC, .version = 1, .segment = segment, .size = size};
    memcpy(record.session, session, 14);
    memcpy(record.digest, digest, 32);
    record.crc = crc32_ieee((const uint8_t *)&record, offsetof(struct hash_record, crc));
    struct fs_file_t file;
    fs_file_t_init(&file);
    storage_session_json_lock();
    ret = fs_open(&file, path, FS_O_CREATE | FS_O_WRITE | FS_O_TRUNC);
    if (!ret) {
        ssize_t n = fs_write(&file, &record, sizeof(record));
        ret = n < 0 ? (int)n : n != (ssize_t)sizeof(record) ? -EIO : fs_sync(&file);
        int closed = fs_close(&file);
        if (!ret) { ret = closed; }
    }
    storage_session_json_unlock();
    return ret;
}

void recording_hash_abort(void)
{
    if (writer.active) { mbedtls_sha256_free(&writer.sha); }
    memset(&writer, 0, sizeof(writer));
}

int recording_hash_begin(const char *session, uint32_t segment)
{
    if (!identity_valid(session, segment)) { return -EINVAL; }
    if (writer.active) { return -EBUSY; }
    memset(&writer, 0, sizeof(writer));
    mbedtls_sha256_init(&writer.sha);
    int ret = mbedtls_sha256_starts(&writer.sha, 0);
    if (ret) { mbedtls_sha256_free(&writer.sha); return ret; }
    strcpy(writer.session, session);
    writer.segment = segment;
    writer.active = true;
    return 0;
}

int recording_hash_update(const uint8_t *bytes, size_t length)
{
    if (!writer.active) { return 0; }
    if ((!bytes && length) || length > HASH_MAX_SIZE - writer.size) {
        recording_hash_abort(); return -EINVAL;
    }
    int ret = mbedtls_sha256_update(&writer.sha, bytes, length);
    if (ret) { recording_hash_abort(); return ret; }
    writer.size += length;
    return 0;
}

int recording_hash_end(const char *session, uint32_t segment, uint32_t size)
{
    if (!writer.active) { return -ENOENT; }
    if (!identity_valid(session, segment) || strcmp(session, writer.session) ||
        segment != writer.segment || size != writer.size || !size) {
        recording_hash_abort(); return -EINVAL;
    }
    uint8_t digest[32];
    int ret = mbedtls_sha256_finish(&writer.sha, digest);
    recording_hash_abort();
    return ret ? ret : recording_hash_save(session, segment, size, digest);
}
