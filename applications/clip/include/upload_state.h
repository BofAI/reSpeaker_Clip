/* Independent HTTPS metadata; never modifies session.json's phone sync flag. */
#ifndef CLIP_UPLOAD_STATE_H
#define CLIP_UPLOAD_STATE_H
#include <stdint.h>

enum upload_file_state { UPLOAD_PENDING, UPLOAD_PARTIAL, UPLOAD_STORED, UPLOAD_FAILED };
struct upload_file_record {
    uint32_t magic, version, generation, size, confirmed, state;
    int32_t last_error;
    uint8_t content_sha256[32], destination_sha256[32];
    uint32_t crc;
};

int upload_state_load(const char *session, uint32_t segment, struct upload_file_record *out);
int upload_state_save(const char *session, uint32_t segment, const struct upload_file_record *record);
const char *upload_state_name(uint32_t state);
#endif
