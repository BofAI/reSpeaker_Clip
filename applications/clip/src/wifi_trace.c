/* Read header bytes without changing packet cursors or ownership. */
#include <zephyr/kernel.h>
#include <zephyr/net/net_if.h>
#include <zephyr/net/net_pkt.h>
#include <zephyr/net_buf.h>
#include <string.h>
#include "wifi_trace.h"

static struct wifi_trace_stats stats;
static struct k_spinlock lock;
static atomic_t peer_ip, local_ip;

void wifi_trace_peer(uint32_t peer, uint32_t local)
{
    atomic_set(&local_ip, local);
    atomic_set(&peer_ip, peer);
}

void wifi_trace_get(struct wifi_trace_stats *out)
{
    k_spinlock_key_t key = k_spin_lock(&lock);
    *out = stats;
    k_spin_unlock(&lock, key);
}

static bool peer_matches(const void *source)
{
    uint32_t ip;
    if (!source || !atomic_get(&peer_ip)) { return false; }
    memcpy(&ip, source, sizeof(ip));
    return ip == (uint32_t)atomic_get(&peer_ip);
}

void wifi_trace_event(enum wifi_trace_event event, struct net_if *iface,
                      const void *source_ip, int result)
{
    if (iface != net_if_get_first_wifi()) { return; }
    if (event >= WIFI_TRACE_ARP_INPUT && !peer_matches(source_ip)) { return; }
    k_spinlock_key_t key = k_spin_lock(&lock);
    switch (event) {
    case WIFI_TRACE_DRIVER_RX: stats.driver_rx++; break;
    case WIFI_TRACE_DRIVER_NOMEM: stats.driver_nomem++; break;
    case WIFI_TRACE_DRIVER_REJECT: stats.driver_reject++; break;
    case WIFI_TRACE_ARP_INPUT: stats.arp_input++; break;
    case WIFI_TRACE_ARP_ACCEPT: stats.arp_accept++; break;
    case WIFI_TRACE_ARP_PENDING: stats.arp_pending++; break;
    case WIFI_TRACE_ARP_NO_PENDING: stats.arp_no_pending++; break;
    case WIFI_TRACE_ARP_RELEASE:
        stats.arp_release++; stats.release_result = result;
        if (result < 0) { stats.arp_release_error++; }
        break;
    }
    k_spin_unlock(&lock, key);
}

void wifi_trace_rx(struct net_if *iface, struct net_pkt *pkt)
{
    if (!iface || !pkt || iface != net_if_get_first_wifi()) { return; }
    uint8_t h[42];
    if (net_buf_linearize(h, sizeof(h), pkt->buffer, 0, sizeof(h)) != sizeof(h) ||
        h[12] != 8 || h[13] != 6 || h[20] != 0 || h[21] != 2 ||
        !peer_matches(h + 28)) { return; }
    uint32_t target;
    memcpy(&target, h + 38, sizeof(target));
    const struct net_linkaddr *addr = net_if_get_link_addr(iface);
    bool valid = h[14] == 0 && h[15] == 1 && h[16] == 8 && h[17] == 0 &&
                 h[18] == 6 && h[19] == 4 && addr->len == 6 &&
                 !memcmp(h, addr->addr, 6) && !memcmp(h + 32, addr->addr, 6) &&
                 target == (uint32_t)atomic_get(&local_ip);
    k_spinlock_key_t key = k_spin_lock(&lock);
    stats.rx_peer++; stats.rx_ms = k_uptime_get_32();
    if (valid) { stats.rx_valid++; }
    k_spin_unlock(&lock, key);
}

int wifi_trace_tx_begin(struct net_if *iface, struct net_pkt *pkt)
{
    if (!iface || !pkt || iface != net_if_get_first_wifi()) { return 0; }
    uint8_t h[42], flags;
    size_t n = net_buf_linearize(h, sizeof(h), pkt->buffer, 0, sizeof(h));
    int kind = 0;
    if (n >= 42 && h[12] == 8 && h[13] == 6 && peer_matches(h + 38)) {
        kind = 1; /* ARP directed to the configured receiver. */
    } else if (n >= 34 && h[12] == 8 && h[13] == 0 && (h[14] >> 4) == 4 &&
               (h[14] & 15) >= 5 && h[23] == 6 && !(h[20] & 0x1f) && !h[21] &&
               peer_matches(h + 30) &&
               net_buf_linearize(&flags, 1, pkt->buffer, 14 + (h[14] & 15) * 4 + 13, 1) == 1 &&
               (flags & 0x12) == 0x02) {
        kind = 2; /* TCP SYN without ACK; no TLS/application bytes read. */
    }
    if (kind) {
        k_spinlock_key_t key = k_spin_lock(&lock);
        if (kind == 1) { stats.tx_arp++; }
        else { stats.tx_syn++; stats.syn_ms = k_uptime_get_32(); }
        k_spin_unlock(&lock, key);
    }
    return kind;
}

void wifi_trace_tx_end(int kind, int result)
{
    if (!kind) { return; }
    k_spinlock_key_t key = k_spin_lock(&lock);
    if (kind == 2) {
        stats.syn_result = result;
        if (result < 0) { stats.tx_syn_error++; }
    } else if (result < 0) { stats.tx_arp_error++; }
    k_spin_unlock(&lock, key);
}
