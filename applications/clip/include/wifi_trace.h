#ifndef CLIP_WIFI_TRACE_H
#define CLIP_WIFI_TRACE_H
#include <stdint.h>
struct net_if;
struct net_pkt;

/* Cumulative software observations. No frame payloads or credentials stored. */
struct wifi_trace_stats {
    uint32_t driver_rx, driver_nomem, driver_reject;
    uint32_t rx_peer, rx_valid, arp_input, arp_accept, arp_pending, arp_no_pending;
    uint32_t arp_release, arp_release_error, tx_syn, tx_syn_error, tx_arp, tx_arp_error;
    uint32_t rx_ms, syn_ms;
    /* Per connection, reset by wifi_trace_peer(). Bits 0..3 indicate first
     * ARP TX, second ARP TX, first valid ARP RX, first SYN TX. TX is driver
     * handoff, not proof of transmission/ACK on the wireless medium. */
    uint32_t timeline_valid, arp_first_ms, arp_second_ms, arp_last_ms;
    uint32_t rx_first_ms, syn_first_ms;
    int release_result, syn_result;
};
enum wifi_trace_event {
    WIFI_TRACE_DRIVER_RX, WIFI_TRACE_DRIVER_NOMEM, WIFI_TRACE_DRIVER_REJECT,
    WIFI_TRACE_ARP_INPUT, WIFI_TRACE_ARP_ACCEPT, WIFI_TRACE_ARP_PENDING,
    WIFI_TRACE_ARP_NO_PENDING, WIFI_TRACE_ARP_RELEASE,
};
#ifdef CONFIG_CLIP_WIFI_DIAG
void wifi_trace_peer(uint32_t peer, uint32_t local);
void wifi_trace_get(struct wifi_trace_stats *out);
void wifi_trace_rx(struct net_if *iface, struct net_pkt *pkt);
void wifi_trace_event(enum wifi_trace_event event, struct net_if *iface,
                      const void *source_ip, int result);
int wifi_trace_tx_begin(struct net_if *iface, struct net_pkt *pkt);
void wifi_trace_tx_end(int kind, int result);
#else
static inline void wifi_trace_peer(uint32_t peer, uint32_t local)
{ (void)peer; (void)local; }
#endif
#endif
