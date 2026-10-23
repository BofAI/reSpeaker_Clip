/* SPDX-License-Identifier: Apache-2.0 */
#include <zephyr/kernel.h>
#include <zephyr/init.h>
#include <stdio.h>
#include <string.h>
#include "config.h"
#include "storage.h"
#include "wifi.h"
#include "https_poc.h"
#include "at_server.h"
#include "resource_stats.h"
#include "usb_cdc.h"

static atomic_t init_error=ATOMIC_INIT(-EAGAIN);
static int status(struct at_cmd_ctx *ctx,char *out,size_t length)
{
    ARG_UNUSED(ctx);
    return snprintf(out,length,"{\"ok\":true,\"data\":{\"state\":\"%s\",\"recording\":false,"
        "\"device\":\"Clip Upload Bench\",\"upload_only\":true,\"init_error\":%d}}\n",
        atomic_get(&init_error) ? "ERROR" : "IDLE",(int)atomic_get(&init_error));
}
static int version(struct at_cmd_ctx *ctx,char *out,size_t length)
{
    ARG_UNUSED(ctx);
    return snprintf(out,length,"{\"ok\":true,\"data\":{\"variant\":\"upload-only\",\"base\":\"0.1.2\"}}\n");
}
static int usb(struct at_cmd_ctx *ctx,char *out,size_t length)
{
    if (ctx->type==AT_CMD_TYPE_SET && strcmp(ctx->args,"on")) {
        return snprintf(out,length,"{\"ok\":false,\"msg\":\"USB remains on for recovery\"}\n");
    }
    return snprintf(out,length,"{\"ok\":true,\"data\":{\"status\":\"on\"}}\n");
}
static int crystal_init(void)
{
    NRF_OSCILLATORS->XOSC32MCAPS=
        (OSCILLATORS_XOSC32MCAPS_ENABLE_Enabled<<OSCILLATORS_XOSC32MCAPS_ENABLE_Pos)|
        (CONFIG_CLIP_HFXO_CAPVALUE<<OSCILLATORS_XOSC32MCAPS_CAPVALUE_Pos);
    NRF_OSCILLATORS->XOSC32KI.INTCAP=CONFIG_CLIP_LFXO_INTCAP;
    return 0;
}
SYS_INIT(crystal_init,POST_KERNEL,0);
int main(void)
{
    static const struct at_command commands[]={
        {"GSTAT",AT_CMD_EXEC|AT_CMD_QUERY,status},
        {"VERSION",AT_CMD_EXEC|AT_CMD_QUERY,version},
        {"USB",AT_CMD_SET|AT_CMD_QUERY,usb},
    };
    for (size_t i=0;i<ARRAY_SIZE(commands);i++) {
        int ret=at_server_register_cmd(&commands[i]);if(ret){return ret;}
    }
    int ret=resource_stats_register();
    if (!ret) { ret=at_server_init(); }
    if (!ret) { ret=usb_cdc_init(); }
    if (!ret) { ret=usb_cdc_enable(); }
    if (ret) { return ret; }
    ret=config_init();
    if (!ret) { ret=storage_init(); }
    if (!ret) { ret=wifi_init(); }
    if (!ret) { ret=https_poc_register(); }
    atomic_set(&init_error,ret);
    /* USB stays available even when mounting or network initialization fails. */
    k_sleep(K_FOREVER);
    return ret;
}
