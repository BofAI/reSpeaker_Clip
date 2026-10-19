# Runtime measurements on the 0.1.2 baseline

This experiment starts at `845713be7d595c0c7d40a16cab677a6ea74aa805`.
Keep `baseline/0.1.2-production` and its signed application as the recovery
reference. It does not include the HTTPS or external-Wi-Fi-patch experiments.

Build the application with the production snippet and the app-only fragment:

```sh
west build --build-dir build-clip-runtime --board clip/nrf5340/cpuapp \
  applications/clip -- -DSNIPPET_ROOT="$PWD/applications/clip" \
  -DSNIPPET=production -Dclip_EXTRA_CONF_FILE="$PWD/applications/clip/runtime.conf"
```

The normal image leaves `CLIP_RESOURCE_STATS` disabled. The instrumented image
adds one AT command and enables names, thread monitoring, stack initialization
and heap accounting. It adds no thread or timer. Compare its map/config with
the baseline before flashing; application Flash has very little spare space.
The small SpeexDSP macro guards avoid existing compiler redefinition warnings.

The first production measurement build links successfully with the same
partition layout and internal Wi-Fi patch as the baseline:

| Application | Baseline | Instrumented | Increase |
|---|---:|---:|---:|
| Flash / 933376 B | 930756 B | 932644 B | 1888 B |
| Static RAM / 450560 B | 430930 B | 432642 B | 1712 B |

Only 732 Flash bytes remain. The RAM linker gap is not a general-purpose free
RAM measurement: libc uses an independent arena in that region. Compiler
warnings are zero after the macro guards; existing SDK Kconfig deprecation,
experimental/assignment warnings and empty display-library CMake warnings
remain. Runtime values and hardware acceptance are pending until this signed
application is flashed and tested; a successful link alone is insufficient.

Run the handler tests on macOS with Clang, ASan and UBSan:

```sh
python3 -m unittest discover -s tools/runtime_resources/tests -v
```

Collect from a device already running the instrumented image:

```sh
python3 -m pip install bleak
python3 tools/runtime_resources/collect.py --address DEVICE_UUID \
  --output /tmp/clip-idle.jsonl --label idle-ble-connected \
  --samples 10 --interval 2 --stacks
```

The collector only queries state. Set USB and SD logging off separately and
wait at least 45 seconds for SD idle power gating before an idle measurement.
Its BLE connection, query processing and radio interrupts affect results; this
is not an unconnected sleep/current measurement. Use an external power meter
for battery current and energy. The recorder produces local audio during
recording scenarios; retain it outside Git.

For an authorized workload test, `scenarios.py` creates normal and enhanced
recordings, samples their resources, downloads both over BLE with size/CRC32
checks, then measures AP idle. It temporarily enables the AP, restores the
original recording mode, and leaves recordings on the SD card. USB and the AP
must initially be off. Run only when creating local test audio is intended:

```sh
python3 tools/runtime_resources/scenarios.py --address DEVICE_UUID \
  --output /tmp/clip-workloads --record-seconds 30
```

The output directory must not exist. CPU windows that cross transfer start/end
must be excluded when describing sustained transfer load; timestamps are
retained for this purpose. Very short BLE downloads may have no full interval.
AP startup has a 60-second response timeout. If any timeout disconnects BLE,
automatic cleanup cannot run: reconnect and explicitly check/stop recording
and the AP before starting another test. A timed-out command may have succeeded.

Device measurements and the observed AP-start failure after enhanced recording
are documented in `docs/runtime-resources-0.1.2-results.md`.

CPU is scheduler busy time divided by busy plus idle time over an interval,
in basis points (10000 = 100%). It depends on the SDK's cycle accounting and
is not a per-interrupt profiler. The first query, subsecond intervals and
counter resets produce `cpu_bp=-1`. Stack scanning runs after CPU samples.

Heap allocated/free/peak refer only to Zephyr's system heap. They exclude the
independent libc malloc arena and dedicated network/audio pools. Heap peak
and thread stack peak are cumulative since boot/thread creation, so later
phases can inherit earlier peaks. Stack fill-pattern scanning estimates high
water, not live stack pointer usage. An all-zero unused region is not a
guarantee against future stack overflow. Thread IDs can be reused and indexes
can change if threads start or exit; enumeration is not an atomic snapshot.

Record the image hash, source commit and dirty patch/new-source snapshot,
toolchain/dependencies, configs/maps, BLE connection state, recording mode,
USB/log/Wi-Fi state and sample duration alongside each result. Do not describe
AP-idle measurements as Wi-Fi upload measurements.
