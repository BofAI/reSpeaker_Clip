/* Diagnostic two-file scheduler; no new thread, no automatic mode selection. */
#ifndef CLIP_UPLOAD_DUAL_H
#define CLIP_UPLOAD_DUAL_H
#include <zephyr/kernel.h>
#include "upload_config.h"
#include "https_stream.h"
struct dual_result {
    atomic_t size[2], sent[2], confirmed[2], resumed[2];
    atomic_t elapsed_ms, transfer_ms, error, cleanup, finished;
};
int upload_dual_run(const struct upload_config *config, const struct upload_tuning *tuning, bool parallel,
                    const char *first, uint32_t first_segment,
                    const char *second, uint32_t second_segment,
                    const uint8_t destination[32],
                    int (*connect)(struct https_stream **), int (*check)(void),
                    void (*pulse)(void), struct dual_result *result);
#endif
