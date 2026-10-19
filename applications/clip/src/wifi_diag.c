/* Passive software counters only: never request radio/firmware statistics. */
#include <zephyr/kernel.h>
#include <zephyr/net/net_if.h>
#include <zephyr/net/net_mgmt.h>
#include <zephyr/net/net_stats.h>
#include <stdio.h>
#include <string.h>
#include "at_server.h"
#include "transport.h"
#include "wifi_diag.h"

struct snapshot {
    uint32_t ms, flags, tx_bytes, rx_bytes, ip_tx, ip_rx, ip_drop;
    uint32_t tcp_tx, tcp_rx, tcp_drop, processing_error, link_ok, link_error;
    int link_status, result, stats_error;
    bool valid;
};
static struct snapshot samples[WIFI_DIAG_POINTS];
static struct k_spinlock sample_lock;
static uint32_t generation;
static atomic_t link_ok, link_error, link_status;
static struct net_if_link_cb link_callback;

static void link_done(struct net_if *iface, struct net_linkaddr *dst, int status)
{
    ARG_UNUSED(dst);
    if (iface != net_if_get_first_wifi()) { return; }
    atomic_inc(status < 0 ? &link_error : &link_ok);
    atomic_set(&link_status, status);
}

void wifi_diag_reset(void)
{
    k_spinlock_key_t key = k_spin_lock(&sample_lock);
    memset(samples, 0, sizeof(samples));
    generation++;
    k_spin_unlock(&sample_lock, key);
}

void wifi_diag_capture(enum wifi_diag_point point, int result)
{
    if ((unsigned int)point >= WIFI_DIAG_POINTS) { return; }
    struct net_if *iface = net_if_get_first_wifi();
    if (!iface) { return; }
    struct net_stats stats = {0};
    int ret = net_mgmt(NET_REQUEST_STATS_GET_ALL, iface, &stats, sizeof(stats));
    struct snapshot s = {
        .ms = k_uptime_get_32(),
        /* bit 0 admin up, 1 carrier, 2 dormant, 3 operational up */
        .flags = net_if_is_admin_up(iface) | (net_if_is_carrier_ok(iface) << 1) |
                 (net_if_is_dormant(iface) << 2) | (net_if_is_up(iface) << 3),
        /* Low 32 bits; compare modulo 2^32. Counters are cumulative since boot. */
        .tx_bytes = (uint32_t)stats.bytes.sent, .rx_bytes = (uint32_t)stats.bytes.received,
        .ip_tx = stats.ipv4.sent, .ip_rx = stats.ipv4.recv, .ip_drop = stats.ipv4.drop,
        .tcp_tx = stats.tcp.sent, .tcp_rx = stats.tcp.recv, .tcp_drop = stats.tcp.drop,
        .processing_error = stats.processing_error,
        .link_ok = (uint32_t)atomic_get(&link_ok), .link_error = (uint32_t)atomic_get(&link_error),
        .link_status = (int)atomic_get(&link_status), .result = result,
        .stats_error = ret, .valid = true,
    };
    /* Software counters are approximate concurrent observations, not an atomic
     * cross-layer transaction. Only publication to the AT reader is locked. */
    k_spinlock_key_t key = k_spin_lock(&sample_lock);
    samples[point] = s;
    k_spin_unlock(&sample_lock, key);
}

static int diagnostic(struct at_cmd_ctx *ctx, char *out, size_t size)
{
    if (ctx->transport_type != TRANSPORT_TYPE_USB || !ctx->args ||
        ctx->args[0] < '0' || ctx->args[0] >= '0' + WIFI_DIAG_POINTS || ctx->args[1]) {
        return snprintf(out, size, "{\"ok\":false,\"msg\":\"USB NETDIAG=0..3 required\"}\n");
    }
    unsigned int point = ctx->args[0] - '0';
    k_spinlock_key_t key = k_spin_lock(&sample_lock);
    struct snapshot s = samples[point];
    unsigned int job = generation;
    k_spin_unlock(&sample_lock, key);
    int n = snprintf(out, size,
        "{\"ok\":true,\"data\":{\"schema\":1,\"job\":%u,\"point\":%u,\"valid\":%s,"
        "\"ms\":%u,\"flags\":%u,\"tx_bytes\":%u,\"rx_bytes\":%u,\"ip_tx\":%u,\"ip_rx\":%u,"
        "\"ip_drop\":%u,\"tcp_tx\":%u,\"tcp_rx\":%u,\"tcp_drop\":%u,\"processing_error\":%u,"
        "\"link_ok\":%u,\"link_error\":%u,\"link_status\":%d,\"result\":%d,\"stats_error\":%d}}\n",
        job, point, s.valid ? "true" : "false", s.ms, s.flags, s.tx_bytes, s.rx_bytes,
        s.ip_tx, s.ip_rx, s.ip_drop, s.tcp_tx, s.tcp_rx, s.tcp_drop, s.processing_error,
        s.link_ok, s.link_error, s.link_status, s.result, s.stats_error);
    if (n < 0 || (size_t)n >= size) { if (size) { out[0] = 0; } return -AT_ERR_NOMEM; }
    return n;
}

int wifi_diag_register(void)
{
    static const struct at_command command = {"NETDIAG", AT_CMD_SET, diagnostic};
    int ret = at_server_register_cmd(&command);
    if (!ret) { net_if_register_link_cb(&link_callback, link_done); }
    return ret;
}
