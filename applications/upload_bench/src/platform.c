/* SPDX-License-Identifier: Apache-2.0 */
#include "clip_event.h"
#include "https_poc.h"
#include "config.h"
#include <zephyr/fs/fs.h>
#include <zephyr/fs/littlefs.h>
#include <zephyr/storage/flash_map.h>
#include <zephyr/settings/settings.h>
#include <zephyr/sys/reboot.h>
#include <errno.h>
#include <nrfx_clock.h>
#include "upload_bench_cpu.h"

unsigned int upload_bench_cpu_mhz(void)
{
#ifdef CLOCK_FEATURE_HFCLK_DIVIDE_PRESENT
    nrf_clock_hfclk_div_t divider = nrf_clock_hfclk_div_get(NRF_CLOCK);
    if (divider == NRF_CLOCK_HFCLK_DIV_1) { return 128; }
    if (divider == NRF_CLOCK_HFCLK_DIV_2) { return 64; }
#endif
    return 0;
}
int upload_bench_cpu_set(unsigned int mhz)
{
    if (mhz != 64 && mhz != 128) { return -EINVAL; }
#ifdef CLOCK_FEATURE_HFCLK_DIVIDE_PRESENT
    nrfx_clock_divider_set(NRF_CLOCK_DOMAIN_HFCLK,
                           mhz == 128 ? NRF_CLOCK_HFCLK_DIV_1 : NRF_CLOCK_HFCLK_DIV_2);
    k_busy_wait(20);
    return upload_bench_cpu_mhz() == mhz ? 0 : -EIO;
#else
    return -ENOTSUP;
#endif
}

static K_MUTEX_DEFINE(events);
FS_LITTLEFS_DECLARE_DEFAULT_CONFIG(lfs);
static struct fs_mount_t mount = {
    .type=FS_LITTLEFS,.fs_data=&lfs,.mnt_point="/lfs",
    .storage_dev=(void *)FIXED_PARTITION_ID(lfs_storage),.flags=FS_MOUNT_FLAG_NO_FORMAT,
};
static atomic_t settings_loading;
static void timeout(struct k_work *work)
{
    ARG_UNUSED(work);
    /* Recover from a stuck read without erasing credentials or recordings. */
    if (atomic_get(&settings_loading)) { sys_reboot(SYS_REBOOT_COLD); }
}
static K_WORK_DELAYABLE_DEFINE(watchdog,timeout);
void settings_load_watchdog_arm(void)
{
    atomic_set(&settings_loading,1);
    k_work_reschedule(&watchdog,K_MSEC(CONFIG_CLIP_SETTINGS_LOAD_TIMEOUT_MS));
}
void settings_load_watchdog_disarm(void)
{
    atomic_clear(&settings_loading);
    k_work_cancel_delayable(&watchdog);
}
int config_init(void)
{
    int ret=fs_mount(&mount);
    if (ret && ret!=-EBUSY) { return ret; }
    ret=fs_mkdir("/lfs/settings");
    if (ret && ret!=-EEXIST) { return ret; }
    return settings_subsys_init();
}
int config_set_time(int64_t timestamp)
{
    return settings_save_one("time/unix_timestamp",&timestamp,sizeof(timestamp));
}
const char *config_get_wifi_reg_domain(void) { return CONFIG_CLIP_WIFI_AP_REG_DOMAIN; }
enum clip_state clip_event_get_state(void) { return CLIP_STATE_IDLE; }
int clip_post_event_sync(enum clip_event event,struct clip_event_result_info *result)
{
    if (!result) { return -EINVAL; }
    if (event!=CLIP_EVENT_UPLOAD_BEGIN && event!=CLIP_EVENT_UPLOAD_END) {
        result->result=CLIP_EVENT_INVALID;result->error_code=-ENOTSUP;
        return -ENOTSUP;
    }
    k_mutex_lock(&events,K_FOREVER);
    bool handled=https_poc_event(event,result,true);
    k_mutex_unlock(&events);
    return handled ? 0 : -ENOTSUP;
}
