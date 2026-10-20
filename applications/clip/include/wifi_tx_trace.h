#ifndef CLIP_WIFI_TX_TRACE_H
#define CLIP_WIFI_TX_TRACE_H
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>

#define WIFI_TX_TRACE_SLOTS 3
/* Prepared, submission returned, HAL accepted, completion received,
 * preparation failed, HAL called, per-frame status available. */
enum wifi_tx_trace_flags {
    WIFI_TX_PREPARED = 1, WIFI_TX_RETURNED = 2, WIFI_TX_ACCEPTED = 4,
    WIFI_TX_DONE = 8, WIFI_TX_PREP_FAILED = 16, WIFI_TX_HAL_CALLED = 32,
    WIFI_TX_STATUS_VALID = 64,
};
struct wifi_tx_trace {
    uint32_t prepared_ms, submit_ms, done_ms;
    uint16_t descriptor;
    uint8_t frame, flags, status, status_count;
    uint8_t phy_tx[6], phy_ack[6]; /* Opaque firmware values, not host uptime. */
    int16_t submit_result;
};
void wifi_tx_trace_reset(uint32_t job);
void wifi_tx_trace_prepare(unsigned int descriptor, unsigned int frame,
                           const void *header, size_t size);
void wifi_tx_trace_submit(unsigned int descriptor, int result, bool hal_called);
void wifi_tx_trace_done(unsigned int descriptor, unsigned int count,
                        const uint8_t *status, const uint8_t *phy_tx,
                        const uint8_t *phy_ack);
void wifi_tx_trace_get(unsigned int slot, struct wifi_tx_trace *out,
                       uint32_t *job, uint32_t *total);
#endif
