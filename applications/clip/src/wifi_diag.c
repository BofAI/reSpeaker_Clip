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
#include "wifi_trace.h"
#include "wifi_tx_trace.h"

struct snapshot {
    struct wifi_trace_stats trace;
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

/* Keep first failure as well as the final result: a later successful event
 * must not erase the event that set the station's failure flag. An attempt
 * labels the receive epoch, not an SDK-provided association transaction ID. */
static struct join_trace {
    uint32_t attempt, started_ms, seen;
    int initial_ready;
    uint32_t request_ms, request_end_ms;
    int request_result;
    uint32_t ready_count, ready_ms;
    int ready_value;
    uint32_t connect_count, first_ms, connect_ms, error_ms;
    int first_status, connect_status, error_status;
    uint32_t dhcp_count, dhcp_ms;
    uint32_t disconnect_ms, disconnect_end_ms, disconnect_count, disconnected_ms;
    int disconnect_result, disconnect_reason, disconnect_valid;
    uint32_t down_ms;
    int down_result;
} join;
static struct k_spinlock join_lock;

void wifi_diag_join_begin(bool initial_ready)
{
    uint32_t now = k_uptime_get_32();
    k_spinlock_key_t key = k_spin_lock(&join_lock);
    uint32_t attempt = join.attempt + 1U;
    memset(&join, 0, sizeof(join));
    join.attempt = attempt ? attempt : 1U;
    join.started_ms = now;
    join.initial_ready = initial_ready;
    k_spin_unlock(&join_lock, key);
}

void wifi_diag_join_event(enum wifi_join_event event, int status)
{
    if ((unsigned int)event >= WIFI_JOIN_EVENTS) { return; }
    uint32_t now = k_uptime_get_32();
    k_spinlock_key_t key = k_spin_lock(&join_lock);
    if (!join.attempt) { k_spin_unlock(&join_lock, key); return; }
    switch (event) {
    case WIFI_JOIN_READY:
        join.ready_count++; join.ready_ms = now; join.ready_value = status; break;
    case WIFI_JOIN_REQUEST: join.request_ms = now; break;
    case WIFI_JOIN_REQUEST_RETURN:
        join.request_end_ms = now; join.request_result = status; break;
    case WIFI_JOIN_RESULT:
        if (!join.connect_count) { join.first_ms = now; join.first_status = status; }
        join.connect_count++; join.connect_ms = now; join.connect_status = status;
        if (status && !join.error_status) { join.error_ms = now; join.error_status = status; }
        break;
    case WIFI_JOIN_DHCP: join.dhcp_count++; join.dhcp_ms = now; break;
    case WIFI_JOIN_DISCONNECT: join.disconnect_ms = now; break;
    case WIFI_JOIN_DISCONNECT_RETURN:
        join.disconnect_end_ms = now; join.disconnect_result = status; break;
    case WIFI_JOIN_DISCONNECTED:
    case WIFI_JOIN_DISCONNECTED_NO_INFO:
        join.disconnect_count++; join.disconnected_ms = now; join.disconnect_reason = status;
        join.disconnect_valid = event == WIFI_JOIN_DISCONNECTED; break;
    case WIFI_JOIN_DOWN: join.down_ms = now; join.down_result = status; break;
    default: break;
    }
    join.seen |= 1U << event;
    k_spin_unlock(&join_lock, key);
}

static int join_query(char *out, size_t size)
{
    k_spinlock_key_t key = k_spin_lock(&join_lock);
    struct join_trace j = join;
    k_spin_unlock(&join_lock, key);
    int n = snprintf(out, size,
        "{\"ok\":true,\"data\":{\"schema\":1,\"type\":\"wifi_join\",\"valid\":%s,"
        "\"attempt\":%u,\"started_ms\":%u,\"initial_ready\":%d,\"seen\":%u,"
        "\"request\":{\"ms\":%u,\"end_ms\":%u,\"result\":%d},"
        "\"ready\":{\"count\":%u,\"ms\":%u,\"value\":%d},"
        "\"connect\":{\"count\":%u,\"first_ms\":%u,\"first_status\":%d,\"ms\":%u,\"status\":%d,\"error_ms\":%u,\"error_status\":%d},"
        "\"dhcp\":{\"count\":%u,\"ms\":%u},"
        "\"disconnect\":{\"ms\":%u,\"end_ms\":%u,\"result\":%d,\"count\":%u,\"event_ms\":%u,\"reason\":%d,\"reason_valid\":%s},"
        "\"down\":{\"ms\":%u,\"result\":%d}}}\n",
        j.attempt ? "true" : "false", j.attempt, j.started_ms, j.initial_ready, j.seen,
        j.request_ms, j.request_end_ms, j.request_result, j.ready_count, j.ready_ms, j.ready_value,
        j.connect_count, j.first_ms, j.first_status, j.connect_ms, j.connect_status, j.error_ms, j.error_status,
        j.dhcp_count, j.dhcp_ms, j.disconnect_ms, j.disconnect_end_ms, j.disconnect_result,
        j.disconnect_count, j.disconnected_ms, j.disconnect_reason,
        j.disconnect_valid ? "true" : "false", j.down_ms, j.down_result);
    if (n < 0 || (size_t)n >= size) { if (size) { out[0] = 0; } return -AT_ERR_NOMEM; }
    return n;
}

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
    uint32_t job = generation;
    k_spin_unlock(&sample_lock, key);
    wifi_tx_trace_reset(job);
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
    wifi_trace_get(&s.trace);
    /* Software counters are approximate concurrent observations, not an atomic
     * cross-layer transaction. Only publication to the AT reader is locked. */
    k_spinlock_key_t key = k_spin_lock(&sample_lock);
    samples[point] = s;
    k_spin_unlock(&sample_lock, key);

}

static int diagnostic(struct at_cmd_ctx *ctx, char *out, size_t size)
{
    bool tx = !strcmp(ctx->name, "NETTX");
    bool joining = !strcmp(ctx->name, "NETDIAG") && ctx->args && !strcmp(ctx->args, "4");
    int limit = tx ? WIFI_TX_TRACE_SLOTS : WIFI_DIAG_POINTS + (joining ? 1 : 0);
    if (ctx->transport_type != TRANSPORT_TYPE_USB || !ctx->args ||
        ctx->args[0] < '0' || ctx->args[0] >= '0' + limit || ctx->args[1]) {
        return snprintf(out, size, "{\"ok\":false,\"msg\":\"USB diagnostic index required\"}\n");
    }
    unsigned int point = ctx->args[0] - '0';
    if (joining) { return join_query(out, size); }
    if (tx) {
        struct wifi_tx_trace r;
        uint32_t job, total;
        char phy_tx[13], phy_ack[13];
        static const char hex[] = "0123456789abcdef";
        wifi_tx_trace_get(point, &r, &job, &total);
        for (unsigned int i = 0; i < 6; i++) {
            phy_tx[i * 2] = hex[r.phy_tx[i] >> 4];
            phy_tx[i * 2 + 1] = hex[r.phy_tx[i] & 15];
            phy_ack[i * 2] = hex[r.phy_ack[i] >> 4];
            phy_ack[i * 2 + 1] = hex[r.phy_ack[i] & 15];
        }
        phy_tx[12] = phy_ack[12] = 0;
        int n = snprintf(out, size,
            "{\"ok\":true,\"data\":{\"schema\":1,\"job\":%u,\"slot\":%u,\"total\":%u,"
            "\"valid\":%s,\"flags\":%u,\"descriptor\":%u,\"frame\":%u,"
            "\"prepared_ms\":%u,\"submit_ms\":%u,\"submit_result\":%d,\"done_ms\":%u,"
            "\"status\":%u,\"status_count\":%u,\"phy_tx\":\"%s\",\"phy_ack\":\"%s\"}}\n",
            job, point, total, r.flags & WIFI_TX_PREPARED ? "true" : "false",
            r.flags, r.descriptor, r.frame, r.prepared_ms, r.submit_ms,
            r.submit_result, r.done_ms, r.status, r.status_count, phy_tx, phy_ack);
        if (n < 0 || (size_t)n >= size) { if (size) { out[0] = 0; } return -AT_ERR_NOMEM; }
        return n;
    }
    k_spinlock_key_t key = k_spin_lock(&sample_lock);
    struct snapshot s = samples[point];
    unsigned int job = generation;
    k_spin_unlock(&sample_lock, key);
    if (!strcmp(ctx->name, "NETTRACE")) {
        const struct wifi_trace_stats *t = &s.trace;
        int n = snprintf(out, size,
            "{\"ok\":true,\"data\":{\"schema\":2,\"job\":%u,\"point\":%u,\"valid\":%s,\"ms\":%u,"
            "\"driver_rx\":%u,\"driver_nomem\":%u,\"driver_reject\":%u,\"rx_peer\":%u,\"rx_valid\":%u,"
            "\"arp_input\":%u,\"arp_accept\":%u,\"arp_pending\":%u,\"arp_no_pending\":%u,"
            "\"arp_release\":%u,\"arp_release_error\":%u,\"tx_syn\":%u,\"tx_syn_error\":%u,"
            "\"tx_arp\":%u,\"tx_arp_error\":%u,\"rx_ms\":%u,\"syn_ms\":%u,"
            "\"timeline_valid\":%u,\"arp_first_ms\":%u,\"arp_second_ms\":%u,\"arp_last_ms\":%u,"
            "\"rx_first_ms\":%u,\"syn_first_ms\":%u,"
            "\"release_result\":%d,\"syn_result\":%d}}\n",
            job, point, s.valid ? "true" : "false", s.ms,
            t->driver_rx, t->driver_nomem, t->driver_reject, t->rx_peer, t->rx_valid,
            t->arp_input, t->arp_accept, t->arp_pending, t->arp_no_pending,
            t->arp_release, t->arp_release_error, t->tx_syn, t->tx_syn_error,
            t->tx_arp, t->tx_arp_error, t->rx_ms, t->syn_ms,
            t->timeline_valid, t->arp_first_ms, t->arp_second_ms, t->arp_last_ms,
            t->rx_first_ms, t->syn_first_ms, t->release_result, t->syn_result);
        if (n < 0 || (size_t)n >= size) { if (size) { out[0] = 0; } return -AT_ERR_NOMEM; }
        return n;
    }
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
    static const struct at_command trace = {"NETTRACE", AT_CMD_SET, diagnostic};
    static const struct at_command tx = {"NETTX", AT_CMD_SET, diagnostic};
    int ret = at_server_register_cmd(&command);
    if (!ret) { ret = at_server_register_cmd(&trace); }
    if (!ret) { ret = at_server_register_cmd(&tx); }
    if (!ret) { net_if_register_link_cb(&link_callback, link_done); }
    return ret;
}
