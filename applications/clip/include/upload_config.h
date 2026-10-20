/* One versioned settings value; credentials never live on removable SD. */
#ifndef CLIP_UPLOAD_CONFIG_H
#define CLIP_UPLOAD_CONFIG_H
#include <stdint.h>
struct upload_config {
    uint32_t version, enabled;
    char ssid[33], password[64], host[96], port[6], device[33], token[97];
    char clock_host[96], clock_port[6];
    uint8_t reserved;
    uint32_t crc;
};
int upload_config_init(struct upload_config *out);
int upload_config_save(struct upload_config *value);
int upload_config_clear(void);
int upload_config_valid(const struct upload_config *value);
#endif
