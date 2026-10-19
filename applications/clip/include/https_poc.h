#ifndef CLIP_HTTPS_POC_H
#define CLIP_HTTPS_POC_H
#include <stdbool.h>
#include <zephyr/kernel.h>
#include "clip_event.h"
int https_poc_register(void);
bool https_poc_active(void);
bool https_poc_command_allowed(const char *name);
bool https_poc_event(enum clip_event event, struct clip_event_result_info *result,
                     bool device_idle);
int wifi_station_connect(const char *ssid, const char *password, atomic_t *cancel);
const char *wifi_station_last_step(void);
struct wifi_station_network {
    char ip[16], netmask[16], gateway[16];
};
void wifi_station_get_network(struct wifi_station_network *network);
#endif
