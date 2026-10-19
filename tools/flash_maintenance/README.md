# External Wi-Fi patch experiment

This temporary USB application accesses the SPI NOR directly without mounting
LittleFS, starting Bluetooth/Wi-Fi, or opening recording files. It is an
application-only replacement: preserve the installed bootloader and network core.
Opening its CDC port at 1200 baud enters the existing USB recovery bootloader.

The writable range is strictly `0x130000..0x800000`; both OTA slots below it are
protected by the firmware. Writes start locked. The host verifies the device ID,
backup, migration plan, unchanged source image, per-chunk CRC, and final complete
readback. Keep power connected throughout a write.

Install the pinned requirements in a separate virtual environment. Backups and
migration output contain Bluetooth keys and configuration: keep them private,
outside Git, with directory mode 0700 and file mode 0600. Never upload them.

After flashing the maintenance application and identifying the CDC port:

```sh
python flash_tool.py --port /dev/cu.usbmodem11301 info
python flash_tool.py --port /dev/cu.usbmodem11301 backup --output /private/tmp/clip-backup
python migrate.py --backup /private/tmp/clip-backup --patch /path/to/nrf70.bin --output /private/tmp/clip-migration
python flash_tool.py --port /dev/cu.usbmodem11301 apply --backup /private/tmp/clip-backup --image /private/tmp/clip-migration/migrated.bin --plan /private/tmp/clip-migration/migration.json --report /private/tmp/clip-migration/apply.json --allow-write
```

Backup reads the entire 109-block region twice and compares the results. Migration
opens the source read-only without automatic formatting, creates a 107-block
LittleFS 2.1 filesystem, and verifies every directory, file, and user attribute
after remounting. The last two 64-KiB blocks hold the exact SDK patch plus erased
padding. The old filesystem remains unchanged in the backup.

Only after successful full readback should the external-patch application be
uploaded through USB recovery. Validate Bluetooth identity/settings, recording,
downloads, and Wi-Fi startup on the device.

For rollback, return to the maintenance application, restore the original region,
then upload the known-good internal-patch application:

```sh
python flash_tool.py --port /dev/cu.usbmodem11301 restore --backup /private/tmp/clip-backup --report /private/tmp/clip-backup/restore.json --allow-write
```

An interrupted write must be restored before attempting normal application boot.
Restore compares against the current flash and repairs changed blocks; apply
deliberately refuses a device that no longer matches the original backup. Do not
reuse an existing report filename. Recovery does not depend on LittleFS or Wi-Fi.

Run host verification with:

```sh
python -m unittest discover -s tools/flash_maintenance -v
```
