#include "upload_tune.h"
#include <errno.h>

bool upload_tune_valid(const struct upload_tuning *t)
{
    return t && (t->chunk_size == 16384 || t->chunk_size == 32768 ||
                 t->chunk_size == 65536 || t->chunk_size == 131072
#ifdef CONFIG_CLIP_UPLOAD_BENCH_WHOLE
                 || t->chunk_size == 8388608
#endif
                ) &&
           (t->write_size == 2048 || t->write_size == 4096) &&
           (t->send_window == 4096 || t->send_window == 5461 || t->send_window == 8192 ||
            t->send_window == 10240 || t->send_window == 12288 ||
            t->send_window == 16384
#ifdef CONFIG_CLIP_UPLOAD_BENCH
            || t->send_window == 32768 || t->send_window == 49152 ||
            t->send_window == 65535
#endif
           ) && t->nodelay <= 1;
}

int upload_tune_parse(const char *args, struct upload_tuning *out)
{
    if (!args || !out) { return -EINVAL; }
    uint32_t values[4] = {0};
    const char *p = args;
    for (unsigned int i = 0; i < 4; i++) {
        unsigned int digits = 0;
        while (*p >= '0' && *p <= '9') {
            if (++digits >
#ifdef CONFIG_CLIP_UPLOAD_BENCH_WHOLE
                7
#else
                6
#endif
               ) { return -EINVAL; }
            values[i] = values[i] * 10 + (unsigned int)(*p++ - '0');
        }
        if (!digits || (i == 3 ? *p != 0 : *p != ',')) { return -EINVAL; }
        if (i != 3) { p++; }
    }
    struct upload_tuning candidate = {values[0], values[1], values[2], values[3]};
    if (!upload_tune_valid(&candidate)) { return -EINVAL; }
    *out = candidate;
    return 0;
}

int upload_tune_server_limit(uint32_t requested, int advertised, bool present)
{
    int limit = present ? advertised : 16384; /* Older receivers' fixed bound. */
    if (limit != 16384 && limit != 32768 && limit != 65536 && limit != 131072
#ifdef CONFIG_CLIP_UPLOAD_BENCH_WHOLE
        && limit != 8388608
#endif
       ) {
        return -EBADMSG;
    }
    return requested <= (uint32_t)limit ? limit : -EMSGSIZE;
}
