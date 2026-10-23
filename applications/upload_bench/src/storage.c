/* SPDX-License-Identifier: Apache-2.0 */
/* Read existing recordings; upload_state owns durable progress writes. */
#include "storage.h"
#include <zephyr/device.h>
#include <zephyr/drivers/regulator.h>
#include <zephyr/storage/disk_access.h>
#include <ff.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>

static FATFS fat;
static struct fs_mount_t mount = {.type=FS_FATFS,.fs_data=&fat,.mnt_point="/SD:"};
static bool mounted;
static K_MUTEX_DEFINE(lifecycle);
static K_MUTEX_DEFINE(metadata);

int storage_ensure_mounted(void)
{
    k_mutex_lock(&lifecycle, K_FOREVER);
    int ret = 0;
    if (!mounted) {
        const struct device *rail = DEVICE_DT_GET(DT_NODELABEL(npm1300_ldo2));
        if (!device_is_ready(rail)) { ret = -ENODEV; goto done; }
        ret = regulator_enable(rail);
        if (ret) { goto done; }
        k_msleep(10);
        ret = disk_access_init("SD");
        if (!ret) { ret = fs_mount(&mount); }
        mounted = ret == 0;
    }
done:
    k_mutex_unlock(&lifecycle);
    return ret;
}
int storage_init(void) { return storage_ensure_mounted(); }
void storage_session_json_lock(void) { k_mutex_lock(&metadata, K_FOREVER); }
void storage_session_json_unlock(void) { k_mutex_unlock(&metadata); }

static int session_path(const char *session, char *path, size_t length)
{
    if (!session || strlen(session) != 14 || !path || !length) { return -EINVAL; }
    for (const char *p=session; *p; p++) {
        if (*p<'0' || *p>'9') { return -EINVAL; }
    }
    int n=snprintf(path,length,"/SD:/REC/%.8s/%.2s/%.2s/%.2s",session,session+8,session+10,session+12);
    if (n<0 || (size_t)n>=length) { path[0]=0; return -ENAMETOOLONG; }
    return 0;
}
static int append_path(const char *session, char *path, size_t length,
                       uint32_t segment, bool metadata_path)
{
    char directory[STORAGE_PATH_MAX];
    int ret=session_path(session,directory,sizeof(directory));
    if (ret || !path || !length) { return ret ? ret : -EINVAL; }
    int n=metadata_path ? snprintf(path,length,"%s/session.json",directory) :
        snprintf(path,length,"%s/%u/%04u.opus",directory,
                 (unsigned)((segment-1)/CONFIG_CLIP_STORAGE_FILES_PER_GROUP),(unsigned)segment);
    if (n<0 || (size_t)n>=length) { path[0]=0; return -ENAMETOOLONG; }
    return 0;
}
int storage_build_chunk_path(const char *session, uint32_t segment, char *path, size_t length)
{
    return segment ? append_path(session,path,length,segment,false) : -EINVAL;
}
int storage_build_session_metadata_path(const char *session, char *path, size_t length)
{
    return append_path(session,path,length,1,true);
}
