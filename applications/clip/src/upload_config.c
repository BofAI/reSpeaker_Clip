#include "upload_config.h"
#include "config.h"
#include <zephyr/settings/settings.h>
#include <zephyr/sys/crc.h>
#include <stddef.h>
#include <string.h>
#include <stdlib.h>
#include <ctype.h>
#include <errno.h>

static struct upload_config *loading;
static int load_error;
static bool bounded(const char *s, size_t size) { return memchr(s, 0, size) != NULL; }
static bool identifier(const char *s, size_t capacity, size_t minimum)
{
    if (!bounded(s, capacity) || strlen(s) < minimum) { return false; }
    for (; *s; s++) {
        if (!isalnum((unsigned char)*s) && *s != '-' && *s != '_') { return false; }
    }
    return true;
}
static bool endpoint(const char *host, size_t capacity, const char *port)
{
    if (!bounded(host, capacity) || !*host || !bounded(port, 6) || !*port) { return false; }
    for (const char *p = host; *p; p++) {
        if (!isalnum((unsigned char)*p) && *p != '.' && *p != '-') { return false; }
    }
    for (const char *p = port; *p; p++) { if (!isdigit((unsigned char)*p)) { return false; } }
    unsigned long n = strtoul(port, NULL, 10);
    return n > 0 && n <= 65535;
}
int upload_config_valid(const struct upload_config *v)
{
    return v->enabled <= 1 && bounded(v->ssid, sizeof(v->ssid)) && v->ssid[0] &&
        bounded(v->password, sizeof(v->password)) && strlen(v->password) >= 8 &&
        endpoint(v->host, sizeof(v->host), v->port) &&
        endpoint(v->clock_host, sizeof(v->clock_host), v->clock_port) &&
        identifier(v->device, sizeof(v->device), 1) && identifier(v->token, sizeof(v->token), 32);
}
static int load(const char *key, size_t length, settings_read_cb read, void *arg)
{
    if (strcmp(key, "config")) { return -ENOENT; }
    if (!loading) { return 0; } /* A later unrelated settings_load must not reset live jobs. */
    struct upload_config candidate;
    int n = length == sizeof(candidate) ? read(arg, &candidate, sizeof(candidate)) : -EINVAL;
    if (n != (int)sizeof(candidate) || candidate.version != 1 || !upload_config_valid(&candidate) ||
        candidate.crc != crc32_ieee((const uint8_t *)&candidate, offsetof(struct upload_config, crc))) {
        load_error = -EBADMSG;
    } else { *loading = candidate; }
    memset(&candidate, 0, sizeof(candidate));
    return 0;
}
static struct settings_handler handler = {.name = "https", .h_set = load};
int upload_config_init(struct upload_config *out)
{
    int ret = settings_register(&handler);
    if (ret && ret != -EEXIST) { return ret; }
    loading = out; load_error = 0;
    settings_load_watchdog_arm();
    ret = settings_load_subtree("https");
    settings_load_watchdog_disarm();
    loading = NULL;
    if (ret || load_error) { memset(out, 0, sizeof(*out)); }
    return ret ? ret : load_error;
}
int upload_config_save(struct upload_config *v)
{
    if (!upload_config_valid(v)) { return -EINVAL; }
    v->version = 1; v->reserved = 0;
    v->crc = crc32_ieee((const uint8_t *)v, offsetof(struct upload_config, crc));
    return settings_save_one("https/config", v, sizeof(*v));
}
int upload_config_clear(void) { return settings_delete("https/config"); }
