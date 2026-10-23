#ifndef CLIP_UPLOAD_TUNE_H
#define CLIP_UPLOAD_TUNE_H

#include <stdbool.h>
#include <stdint.h>

struct upload_tuning {
    uint32_t chunk_size;
    uint32_t write_size;
    uint32_t send_window;
    uint32_t nodelay;
};

/* Parse all four decimal fields; failure never modifies the destination. */
int upload_tune_parse(const char *args, struct upload_tuning *out);
bool upload_tune_valid(const struct upload_tuning *tune);
int upload_tune_server_limit(uint32_t requested, int advertised, bool present);

#endif
