"""Generate fail-closed, build-local SDK observations for the pinned NCS tree."""
import argparse
import hashlib
import json
from pathlib import Path


SOURCES = {
    "net_core.c": ("subsys/net/ip/net_core.c", "ecf837485667a956362dcc5806f56e9949327386b629a20d01fc41bea19f0c56"),
    "arp.c": ("subsys/net/l2/ethernet/arp.c", "0e1f28c3a4ec46b28e5406e5822e6f8f19601f4206ee71a60041930f72d4b920"),
    "ethernet.c": ("subsys/net/l2/ethernet/ethernet.c", "061b5e4ba395212ff4664bf6932c0a67d2d7c799a028758e7c3386af8616f613"),
    "net_if.c": ("drivers/wifi/nrf_wifi/src/net_if.c", "3b1633e2bb30ca947eb993513efca0f99519e5dc7e3b9cb9483be07f023ecfcb"),
}


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError("SDK diagnostic insertion point changed")
    return text.replace(old, new)


def transform(name, text):
    if name == "net_core.c":
        old = "int net_recv_data(struct net_if *iface, struct net_pkt *pkt)\n{\n\tint ret;"
        text = replace_once(text, old, old + "\n\twifi_trace_rx(iface, pkt);")
    elif name == "arp.c":
        old = "case NET_ARP_REPLY:\n\t\tif (net_ipv4_is_my_addr_raw(arp_hdr->dst_ipaddr)) {"
        new = """case NET_ARP_REPLY:
		wifi_trace_event(WIFI_TRACE_ARP_INPUT, net_pkt_iface(pkt), arp_hdr->src_ipaddr, 0);
		if (net_ipv4_is_my_addr_raw(arp_hdr->dst_ipaddr)) {
			wifi_trace_event(WIFI_TRACE_ARP_ACCEPT, net_pkt_iface(pkt), arp_hdr->src_ipaddr, 0);"""
        text = replace_once(text, old, new)
        old = "\tentry = arp_entry_get_pending(iface, src);"
        text = replace_once(text, old, old + "\n\twifi_trace_event(entry ? WIFI_TRACE_ARP_PENDING : WIFI_TRACE_ARP_NO_PENDING, iface, src, 0);")
        old = "\t\tret = net_if_l2(iface)->send(iface, pkt);"
        text = replace_once(text, old, old + "\n\t\twifi_trace_event(WIFI_TRACE_ARP_RELEASE, iface, src, ret);")
    elif name == "ethernet.c":
        old = "\tret = net_l2_send(api->send, net_if_get_device(iface), iface, pkt);"
        new = """	{
		int clip_trace_kind = wifi_trace_tx_begin(iface, pkt);
		ret = net_l2_send(api->send, net_if_get_device(iface), iface, pkt);
		wifi_trace_tx_end(clip_trace_kind, ret);
	}"""
        text = replace_once(text, old, new)
    elif name == "net_if.c":
        old = "\thost_stats->total_rx_pkts++;"
        text = replace_once(text, old, old + "\n\twifi_trace_event(WIFI_TRACE_DRIVER_RX, iface, NULL, 0);")
        old = '\tpkt = net_pkt_from_nbuf(iface, frm);\n\tif (!pkt) {'
        text = replace_once(text, old, old + "\n\t\twifi_trace_event(WIFI_TRACE_DRIVER_NOMEM, iface, NULL, 0);")
        old = '\t\tLOG_DBG("RCV Packet dropped by NET stack: %d", status);'
        text = replace_once(text, old, "\t\twifi_trace_event(WIFI_TRACE_DRIVER_REJECT, iface, NULL, status);\n" + old)
    else:
        raise ValueError("Unknown SDK source")
    return '#include "wifi_trace.h"\n' + text


def generate(zephyr, output):
    generated = {}
    # Check all originals and insertion points before writing any output.
    for name, (relative, expected) in SOURCES.items():
        raw = (zephyr / relative).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError(f"Pinned SDK source changed: {relative}; review instrumentation")
        generated[name] = transform(name, raw.decode())
    output.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for name, text in generated.items():
        (output / name).write_text(text)
        manifest[name] = {"original": SOURCES[name][1],
                          "instrumented": hashlib.sha256(text.encode()).hexdigest()}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zephyr", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    generate(args.zephyr, args.output)
