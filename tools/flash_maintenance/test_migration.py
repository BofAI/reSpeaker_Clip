import json
import pathlib
import tempfile
import unittest
from littlefs import LittleFS
from migrate import GEOMETRY, migrate, open_image, inventory
from flash_tool import START, END, ERASE, CHUNK, apply_image, private_write, sha


class FakeDevice:
    def __init__(self, data):
        self.data = bytearray(data)
        self.info = {"device_id": "synthetic"}
        self.armed = False
        self.erased = []

    def region(self): return bytes(self.data)

    def command(self, command):
        if command.startswith("ARM "):
            self.armed = True
            return "OK ARM"
        op, offset, length = command.split()
        offset, length = int(offset, 16), int(length, 16)
        assert op == "ERASE" and self.armed and START <= offset < END
        assert length == ERASE and offset % ERASE == 0
        self.erased.append(offset)
        self.data[offset-START:offset-START+length] = b"\xff"*length
        return "OK ERASE"

    def write(self, offset, data):
        assert self.armed and START <= offset and offset+len(data) <= END
        assert len(data) <= CHUNK
        self.data[offset-START:offset-START+len(data)] = data


class MigrationTests(unittest.TestCase):
    def test_preserves_files_empty_directories_and_attributes(self):
        fs = LittleFS(block_count=109, **GEOMETRY)
        fs.makedirs("/settings/empty", exist_ok=True)
        with fs.open("/settings/run", "wb") as out: out.write(bytes(range(256))*80)
        with fs.open("/zero", "wb") as out: out.write(b"")
        fs.setattr("/settings/run", 7, b"fixture")
        fs.unmount()
        original = bytes(fs.context.buffer)
        smaller, before = migrate(original)
        self.assertEqual(len(smaller), 0x6b0000)
        check = open_image(smaller, True)
        self.assertEqual(inventory(check), before)
        self.assertEqual(original, bytes(fs.context.buffer))
        check.unmount()

    def test_corrupt_source_is_rejected_without_formatting(self):
        with self.assertRaises(Exception): migrate(b"\xff"*(END-START))

    def test_apply_checks_backup_and_restore_recovers_changed_blocks(self):
        before = b"\xff"*(END-START)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            private_write(root/"backup.bin", before)
            private_write(root/"backup.json", json.dumps({"device": {"device_id": "synthetic"},
                          "sha256": sha(before), "verified_read_twice": True}).encode())
            target = bytearray(before); target[3:10] = b"example"
            private_write(root/"target.bin", target)
            device = FakeDevice(before)
            plan = {"device": device.info, "before_sha256": sha(before), "after_sha256": sha(target),
                    "inventory_equal_after_remount": True, "old_blocks": 109, "new_blocks": 107,
                    "patch_offset": 0x7e0000}
            private_write(root/"plan.json", json.dumps(plan).encode())
            with self.assertRaises(ValueError):
                apply_image(device, root, root/"target.bin", root/"missing-plan.json")
            self.assertFalse(device.armed)
            apply_image(device, root, root/"target.bin", root/"apply.json", plan_path=root/"plan.json")
            self.assertEqual(bytes(device.data), bytes(target))
            self.assertEqual(device.erased, [START])
            with self.assertRaises(ValueError):
                apply_image(device, root, root/"target.bin", root/"unsafe.json", plan_path=root/"plan.json")
            apply_image(device, root, None, root/"restore.json", restore=True)
            self.assertEqual(device.region(), before)


if __name__ == "__main__": unittest.main()
