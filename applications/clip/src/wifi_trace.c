/* Read header bytes without changing packet cursors or ownership. */
#include <zephyr/kernel.h>
#include <zephyr/net/net_if.h>
#include <zephyr/net/net_pkt.h>
#include <zephyr/net_buf.h>
#include <string.h>
#include "wifi_trace.h"
#include "wifi_tx_trace.h"

static struct wifi_trace_stats stats;
static struct k_spinlock lock;
static atomic_t peer_ip, local_ip;
static struct wifi_tx_trace tx_records[WIFI_TX_TRACE_SLOTS];
static uint32_t tx_job, tx_total;
static bool tx_armed;

void wifi_trace_peer(uint32_t peer, uint32_t local)
{
    k_spinlock_key_t key = k_spin_lock(&lock);
    tx_armed = true;
    stats.timeline_valid = 0;
    stats.arp_first_ms = stats.arp_second_ms = stats.arp_last_ms = 0;
    stats.rx_first_ms = stats.syn_first_ms = 0;
    atomic_set(&local_ip, local);
    atomic_set(&peer_ip, peer);
    k_spin_unlock(&lock, key);
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
    if (valid) {
        stats.rx_valid++;
        if (!(stats.timeline_valid & 4)) {
            stats.rx_first_ms = stats.rx_ms;
            stats.timeline_valid |= 4;
        }
    }
    k_spin_unlock(&lock, key);
}

int wifi_trace_tx_begin(struct net_if *iface, struct net_pkt *pkt)
{
    if (!iface || !pkt || iface != net_if_get_first_wifi()) { return 0; }
    uint8_t h[42], flags;
    size_t n = net_buf_linearize(h, sizeof(h), pkt->buffer, 0, sizeof(h));
    int kind = 0;
    if (n >= 42 && h[12] == 8 && h[13] == 6 && h[20] == 0 && h[21] == 1 &&
        peer_matches(h + 38)) {
        kind = 1; /* ARP request directed to the configured receiver. */
    } else if (n >= 34 && h[12] == 8 && h[13] == 0 && (h[14] >> 4) == 4 &&
               (h[14] & 15) >= 5 && h[23] == 6 && !(h[20] & 0x1f) && !h[21] &&
               peer_matches(h + 30) &&
               net_buf_linearize(&flags, 1, pkt->buffer, 14 + (h[14] & 15) * 4 + 13, 1) == 1 &&
               (flags & 0x12) == 0x02) {
        kind = 2; /* TCP SYN without ACK; no TLS/application bytes read. */
    }
    if (kind) {
        k_spinlock_key_t key = k_spin_lock(&lock);
        uint32_t now = k_uptime_get_32();
        if (kind == 1) {
            stats.tx_arp++;
            stats.arp_last_ms = now;
            if (!(stats.timeline_valid & 1)) {
                stats.arp_first_ms = now;
                stats.timeline_valid |= 1;
            } else if (!(stats.timeline_valid & 2)) {
                stats.arp_second_ms = now;
                stats.timeline_valid |= 2;
            }
        } else {
            stats.tx_syn++;
            stats.syn_ms = now;
            if (!(stats.timeline_valid & 8)) {
                stats.syn_first_ms = now;
                stats.timeline_valid |= 8;
            }
        }
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

/* No radio requests, packet mutation, allocation or callback-owned pointers. */
void wifi_tx_trace_reset(uint32_t job)
{
    k_spinlock_key_t key = k_spin_lock(&lock);
    memset(tx_records, 0, sizeof(tx_records));
    tx_job = job;
    tx_total = 0;
    tx_armed = false;
    k_spin_unlock(&lock, key);
}

void wifi_tx_trace_prepare(unsigned int descriptor, unsigned int frame,
                           const void *header, size_t size)
{
    const uint8_t *h = header;
    uint32_t source;
    if (!h || size < 42 || descriptor > UINT16_MAX || frame > UINT8_MAX ||
        h[12] != 8 || h[13] != 6 || h[14] != 0 || h[15] != 1 ||
        h[16] != 8 || h[17] != 0 || h[18] != 6 || h[19] != 4 ||
        h[20] != 0 || h[21] != 1 || !peer_matches(h + 38)) { return; }
    memcpy(&source, h + 28, sizeof(source));
    if (source != (uint32_t)atomic_get(&local_ip)) { return; }
    k_spinlock_key_t key = k_spin_lock(&lock);
    if (tx_armed) {
        if (tx_total < WIFI_TX_TRACE_SLOTS) {
            tx_records[tx_total] = (struct wifi_tx_trace){
                .prepared_ms = k_uptime_get_32(), .descriptor = descriptor,
                .frame = frame, .flags = WIFI_TX_PREPARED, .status = UINT8_MAX,
            };
        }
        tx_total++;
    }
    k_spin_unlock(&lock, key);
}

void wifi_tx_trace_submit(unsigned int descriptor, int result, bool hal_called)
{
    k_spinlock_key_t key = k_spin_lock(&lock);
    for (unsigned int i = 0; i < WIFI_TX_TRACE_SLOTS; i++) {
        struct wifi_tx_trace *r = &tx_records[i];
        if (!(r->flags & WIFI_TX_PREPARED) || (r->flags & WIFI_TX_RETURNED) ||
            r->descriptor != descriptor) { continue; }
        r->submit_ms = k_uptime_get_32();
        r->submit_result = result;
        r->flags |= WIFI_TX_RETURNED;
        if (hal_called) {
            r->flags |= WIFI_TX_HAL_CALLED;
            if (result == 0) { r->flags |= WIFI_TX_ACCEPTED; }
        } else { r->flags |= WIFI_TX_PREP_FAILED; }
    }
    k_spin_unlock(&lock, key);
}

void wifi_tx_trace_done(unsigned int descriptor, unsigned int count,
                        const uint8_t *status, const uint8_t *phy_tx,
                        const uint8_t *phy_ack)
{
    k_spinlock_key_t key = k_spin_lock(&lock);
    for (unsigned int i = 0; i < WIFI_TX_TRACE_SLOTS; i++) {
        struct wifi_tx_trace *r = &tx_records[i];
        if (!(r->flags & WIFI_TX_PREPARED) ||
            (r->flags & (WIFI_TX_DONE | WIFI_TX_PREP_FAILED)) ||
            ((r->flags & WIFI_TX_RETURNED) && !(r->flags & WIFI_TX_ACCEPTED)) ||
            r->descriptor != descriptor) { continue; }
        r->done_ms = k_uptime_get_32();
        r->flags |= WIFI_TX_DONE;
        r->status_count = count > UINT8_MAX ? UINT8_MAX : count;
        if (status && r->frame < count) {
            r->status = status[r->frame];
            r->flags |= WIFI_TX_STATUS_VALID;
        }
        if (phy_tx) { memcpy(r->phy_tx, phy_tx, sizeof(r->phy_tx)); }
        if (phy_ack) { memcpy(r->phy_ack, phy_ack, sizeof(r->phy_ack)); }
    }
    k_spin_unlock(&lock, key);
}

void wifi_tx_trace_get(unsigned int slot, struct wifi_tx_trace *out,
                       uint32_t *job, uint32_t *total)
{
    k_spinlock_key_t key = k_spin_lock(&lock);
    *out = slot < WIFI_TX_TRACE_SLOTS ? tx_records[slot] : (struct wifi_tx_trace){0};
    *job = tx_job;
    *total = tx_total;
    k_spin_unlock(&lock, key);
}
