"""Generate opt-in network fixes against the exact, pinned NCS v3.3.0 sources.

The shared SDK is never edited. See docs/network-backports-0.1.2.md for the
upstream commits and the single application-owned Kconfig adaptation.
"""
import argparse
import hashlib
import json
from pathlib import Path

SOURCES = {
    "dhcpv4.c": ("subsys/net/lib/dhcpv4/dhcpv4.c", "4ea62e8b4cd0692db0086d5e64a9b0e953d6ab84e6125d74793d3a8f7923ab59"),
    "arp.c": ("subsys/net/l2/ethernet/arp.c", "0e1f28c3a4ec46b28e5406e5822e6f8f19601f4206ee71a60041930f72d4b920"),
}
COMMITS = {
    "dhcpv4.c": ["dde93af825008bdd07ef466fb9f588339fe9912f",
                 "5264d536c697232845f5e185919b871aabcbb815",
                 "4f8f5a1baf63d8e2c30ca2bdf9c74091d282ad42",
                 "b2f6db70b7d33e37b0db82446b6161d2f5aef5a0"],
    "arp.c": ["7ae34e80271646ad31351c5dfb261fd63598be1d",
              "8ecc88f7042a28c37e5acd9b8b29bdbbb5074e2a",
              "baca7b10a8cc4f778210d1e2faf8140ef6259508"],
}


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise ValueError("Pinned SDK backport insertion point changed")
    return source.replace(old, new)


def dhcp(source):
    start = source.index("static void dhcpv4_handle_msg_nak(")
    stop = source.index("/* Takes and releases lock */", start)
    block = source[start:stop]
    block = replace_once(block, "\tcase NET_DHCPV4_INIT_REBOOT:\n", "")
    block = replace_once(block, "\tswitch (iface->config.dhcpv4.state) {", """\tswitch (iface->config.dhcpv4.state) {
\tcase NET_DHCPV4_INIT_REBOOT:
\t\tLOG_DBG("NAK during INIT-REBOOT, restart config");
\t\tdhcpv4_enter_selecting(iface);
\t\tdhcpv4_immediate_timeout(&iface->config.dhcpv4);
\t\tbreak;""")
    source = source[:start] + block + source[stop:]

    start = source.index("static void dhcpv4_start_internal(")
    stop = source.index("\n}\n", start) + 3
    block = source[start:stop]
    block = replace_once(block, "\tcase NET_DHCPV4_DISABLED:\n", "\tcase NET_DHCPV4_DISABLED:\n\t\tiface->config.dhcpv4.attempts = 0U;\n")
    block = replace_once(block, "\t\tif (first_start) {", "\t\tif (first_start && iface->config.dhcpv4.state == NET_DHCPV4_INIT) {")
    source = source[:start] + block + source[stop:]

    start = source.index("static uint32_t dhcpv4_manage_timers(")
    stop = source.index("static void dhcpv4_timeout(", start)
    block = source[start:stop]
    block = replace_once(block, "\tcase NET_DHCPV4_INIT_REBOOT:\n", """\tcase NET_DHCPV4_INIT_REBOOT:
\t\tif (iface->config.dhcpv4.attempts >= CONFIG_CLIP_DHCP_REBOOT_ATTEMPTS) {
\t\t\tdhcpv4_enter_selecting(iface);
\t\t\treturn dhcpv4_send_discover(iface);
\t\t}
\t\treturn dhcpv4_send_request(iface);
""")
    return source[:start] + block + source[stop:]


def arp(source):
    start = source.index("int net_arp_clear_pending(")
    stop = source.index("int net_arp_foreach(", start)
    block = source[start:stop]
    block = replace_once(block,
        "\tstruct arp_entry *entry = arp_entry_find_pending(iface, dst);",
        "\tstruct arp_entry *entry;\n\n\tk_mutex_lock(&arp_mutex, K_FOREVER);\n"
        "\tentry = arp_entry_get_pending(iface, dst);")
    block = replace_once(block, "\t\treturn -ENOENT;", "\t\tk_mutex_unlock(&arp_mutex);\n\t\treturn -ENOENT;")
    block = replace_once(block, "\tarp_entry_cleanup(entry, true);",
        "\tarp_entry_cleanup(entry, true);\n\tsys_slist_prepend(&arp_free_entries, &entry->node);\n"
        "\tk_mutex_unlock(&arp_mutex);")
    source = source[:start] + block + source[stop:]
    return replace_once(source, "(const uint8_t *)&NET_ETH_HDR(pkt)->dst.addr,",
                         "(const uint8_t *)&entry->eth,")


def generate(zephyr, output, dhcp_enabled=False, arp_enabled=False):
    generated = {}
    for name, enabled, transform in (("dhcpv4.c", dhcp_enabled, dhcp), ("arp.c", arp_enabled, arp)):
        if not enabled:
            continue
        relative, expected = SOURCES[name]
        raw = (zephyr / relative).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError(f"Pinned SDK source changed: {relative}; review backport")
        generated[name] = transform(raw.decode())
    if not generated:
        raise ValueError("Select at least one backport")
    output.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for name, source in generated.items():
        (output / name).write_text(source)
        manifest[name] = {"original": SOURCES[name][1], "commits": COMMITS[name],
                          "generated": hashlib.sha256(source.encode()).hexdigest()}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zephyr", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dhcp", action="store_true")
    parser.add_argument("--arp", action="store_true")
    args = parser.parse_args()
    generate(args.zephyr, args.output, args.dhcp, args.arp)
