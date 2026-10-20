# Pinned NCS network backport experiments

These are optional experiments on `experiment/https-runtime-0.1.2`, not an SDK
upgrade or a claim that the device's network delays are fixed. The baseline is
NCS v3.3.0: sdk-nrf `ba167d9f3db4abbdc9b67887ca3ea66c64f2d956`, sdk-zephyr
`fd9204a02d52630660ce8d729945a4dd743feabf`.

## Upstream provenance

All commit links below are in <https://github.com/nrfconnect/sdk-zephyr>.

| Group | Commit | Behavior |
| --- | --- | --- |
| DHCP | [dde93af](https://github.com/nrfconnect/sdk-zephyr/commit/dde93af825008bdd07ef466fb9f588339fe9912f) | Restart discovery immediately on an INIT-REBOOT NAK, without matching the old requesting server. |
| DHCP | [5264d53](https://github.com/nrfconnect/sdk-zephyr/commit/5264d536c697232845f5e185919b871aabcbb815) | Reset retransmission attempts on every start. |
| DHCP | [4f8f5a1](https://github.com/nrfconnect/sdk-zephyr/commit/4f8f5a1baf63d8e2c30ca2bdf9c74091d282ad42) | Keep initial discovery jitter, skip it when probing a remembered lease. |
| DHCP | [b2f6db7](https://github.com/nrfconnect/sdk-zephyr/commit/b2f6db70b7d33e37b0db82446b6161d2f5aef5a0) | Bound the remembered-lease probe separately from ordinary DHCP requests. |
| ARP | [7ae34e8](https://github.com/nrfconnect/sdk-zephyr/commit/7ae34e80271646ad31351c5dfb261fd63598be1d) | Lock pending-entry cleanup. |
| ARP | [8ecc88f](https://github.com/nrfconnect/sdk-zephyr/commit/8ecc88f7042a28c37e5acd9b8b29bdbbb5074e2a) | Unlink cleared pending entries and return them to the free list immediately. |
| ARP | [baca7b1](https://github.com/nrfconnect/sdk-zephyr/commit/baca7b10a8cc4f778210d1e2faf8140ef6259508) | Take a released packet's destination MAC from the ARP entry, not its IP-header bytes. |

The implementation preserves these source-level changes against the older
pinned functions. The retry-cap adaptation uses application-owned
`CONFIG_CLIP_DHCP_REBOOT_ATTEMPTS` (default 1), equivalent to upstream's new
`CONFIG_NET_DHCPV4_INIT_REBOOT_ATTEMPTS`; no shared internal header or Kconfig
is edited. The newer optional reboot-hint API is not needed by this experiment
and is not backported. There is no new on-device AT API or persistent setting.

## Separate comparisons

Keep `https.conf`, `runtime.conf`, `netdiag.conf`,
`https-connect-budget.conf`, and `https-wifi-no-stbc.conf` in that order.
Append exactly one of these configurations in an independent build directory:

* `https-dhcp-backport.conf`: restore DHCP lease reuse and apply only the four
  DHCP fixes. Keep the original ARP implementation and diagnostics.
* `https-arp-backport.conf`: retain fresh DHCP, apply only the three ARP fixes.
  Compare with the existing fresh-DHCP baseline; do not enable both groups when
  attributing a result to one group.

TCP connect remains 10 seconds and TLS handshake 10 seconds; Wi-Fi power save,
RX STBC, buffers, stacks, ARP table size, and external flash layout are not
changed. Inspect the final `.config` and generated manifest for each build.

`backport_network.py` verifies complete original-file SHA-256 hashes and unique
insertion points before writing generated SDK copies under the build directory.
DHCP replaces exactly one source in the DHCP library. ARP fixes are applied
before the existing diagnostic hooks, so their observations remain available.
Any SDK drift aborts configuration. A build without these flags retains the
original SDK sources. The shared NCS workspace is never modified.

## Validation

Set `CLIP_TEST_SDK` to a read-only pinned Zephyr tree (the original DHCP and ARP
files at their normal relative paths suffice), then run:

```sh
CLIP_TEST_SDK=/absolute/pinned/zephyr python3 -m unittest discover \
  -s tools/https_poc -p 'test_*.py' -v
```

The backport tests compile actual transformed functions/branches with
ASan/UBSan and check new-server NAK acceptance, preserved REQUESTING filtering,
one-probe fallback, locked/free-list cleanup, destination MAC selection,
diagnostic compatibility, and rejection of altered SDK inputs. Without this
explicit fixture the SDK-specific test class is skipped, not counted as passed.

Build each image with zero C warnings, preserve signatures, ELF/map/config,
source snapshot and dependency versions, and verify partition and Wi-Fi patch
identity. Compile and flash are separate operations. Hardware comparison uses
the same recording and receiver, full DHCP/ARP/TCP capture, explicit no-sleep
assertions and clock references, file byte comparisons, and post-cleanup heap
and stack observations. A missing slow sample is not proof that the RF/AP path
has been fixed; firmware TX success is not independent proof of over-air receipt.
