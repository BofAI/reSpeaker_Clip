# Single-variable TCP RTO experiment

`applications/clip/https-tcp-rto-1000.conf` is an optional experiment overlay.
Append it after the same configuration used for the [passive TCP trace](TCP_TRACE.md).
It changes only `CONFIG_NET_TCP_INIT_RETRANSMISSION_TIMEOUT` from 200 to 1000 ms.
Randomization remains enabled, producing base waits of 1000–1498 ms. This SDK
uses the parameter for data and connection-control retries. Standard builds do
not include this overlay automatically.

Both images used the same pinned SDK, trace hooks, receiver, 16 KiB chunks,
transport buffers, windows, and a 574179-byte recording. Each transfer started
at server offset zero and required 36 successful PUTs. The receiver independently
verified the full recording SHA-256. Trace cursors were continuous without loss.

| Metric | Base 200 ms, 2 transfers | Base 1000 ms, 3 transfers |
| --- | ---: | ---: |
| Weighted recording payload throughput during PUT | 8.51 KiB/s | 8.61 KiB/s |
| Average cumulative PUT duration | 65.88 s | 65.13 s |
| Data timeout callbacks per transfer | 168–199 | 1–6 |
| Repeated sequence bytes / successful queued TCP payload | 47.15% | 0.61% |

The 1.14% throughput difference does not demonstrate a meaningful speed gain.
This is a sequential, small-sample experiment: the server-observed round-trip
median was about 256 ms in the earlier captured control and about 290 ms in the
experimental transfers. The second control had device tracing but no packet
capture. Neither link conditions nor ACK processing behavior are fully isolated.

The experiment confirms a large reduction in repeated data, but did not show
that removing these timeouts alone yields a large throughput increase.
Remaining confirmation stalls and ordinary write/read waits require
separate analysis. A retransmission timeout is not proof of packet loss.

The experimental capture contained 2834 packets with zero kernel drops. All
three flows included connection establishment and closure; successful device
TX segment multiplicities and server-captured data segments matched exactly.
All three recording transfers completed. This does not qualify the overlay as
a production throughput fix or validate recording interruption under this RTO.

The instrumented application used 830732 bytes Flash and 448222 bytes static RAM:
16 bytes more Flash than the 200 ms trace image, with unchanged static RAM.
The same bounded trace allocations and threads were retained. Local firmware
and raw measurements are stored outside Git in the versioned firmware library.
