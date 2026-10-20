"""Verify the host workflow without connecting to a Clip or opening a network."""
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import hardware_probe


class HardwareProbeTest(unittest.TestCase):
    def run_probe(self, failed_diagnostic=None, succeeded=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {"ssid": "Clip-Test", "password": "test-password", "port": 8443,
                      "host": "192.168.2.78", "device": "clip-test", "token": "a" * 40}
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config))
            config_path.chmod(0o600)
            session = "20261019070446"
            reference = root / "reference.opus"
            reference.write_bytes(b"known recording contents")
            received = root / "received"
            received.mkdir()
            identity = hashlib.sha256(f"clip-test/{session}/1".encode()).hexdigest()
            (received / (identity + ".bin")).write_bytes(reference.read_bytes())
            args = SimpleNamespace(config=config_path, session=session, reference=reference,
                                   received=received, output=root / "results", port="unused",
                                   repeat=1, netdiag=True, nettrace=True, nettx=True)
            commands = []

            def exchange(fd, command):
                commands.append(command)
                if command == failed_diagnostic:
                    raise RuntimeError("device rejected diagnostic")
                if command == "AT+GSTAT":
                    return {"ok": True, "data": {"state": "IDLE", "recording": False}}
                if command == "AT+UPLOAD?":
                    return {"ok": True, "active": False,
                            "stage": "succeeded" if succeeded else "failed"}
                if command.startswith("AT+RES="):
                    return {"ok": True, "data": {"total": 1}}
                return {"ok": True, "data": {"command": command.split("=", 1)[0]}}

            serial = MagicMock()
            serial.return_value.__enter__.return_value.fileno.return_value = 7
            with patch.dict("sys.modules", {"serial": SimpleNamespace(Serial=serial)}), \
                    patch("hardware_probe.exchange", exchange), \
                    redirect_stdout(io.StringIO()) as console:
                if failed_diagnostic or not succeeded:
                    with self.assertRaises(RuntimeError):
                        hardware_probe.run(args)
                else:
                    hardware_probe.run(args)
            log = (args.output / "measurements.jsonl").read_text()
            for secret in (config["password"], config["token"]):
                self.assertNotIn(secret, log)
                self.assertNotIn(secret, console.getvalue())
            summary = args.output / "summary.json"
            return commands, json.loads(summary.read_text()) if summary.exists() else None

    def test_all_diagnostic_groups_and_recording_comparison_are_preserved(self):
        commands, summary = self.run_probe()
        self.assertTrue(summary["all_succeeded"])
        result = summary["results"][0]
        self.assertTrue(result["byte_for_byte_match"])
        for name, count in (("netdiag", 4), ("nettrace", 4), ("nettx", 3)):
            self.assertEqual(len(result[name]), count)
            for index in range(count):
                self.assertEqual(commands.count(f"AT+{name.upper()}={index}"), 1)

    def test_diagnostic_rejection_aborts_without_claiming_success(self):
        commands, summary = self.run_probe(failed_diagnostic="AT+NETTRACE=1")
        self.assertIsNone(summary)
        self.assertNotIn("AT+NETTRACE=2", commands)
        self.assertNotIn("AT+NETTX=0", commands)

    def test_failed_upload_preserves_failure_summary(self):
        _, summary = self.run_probe(succeeded=False)
        self.assertFalse(summary["all_succeeded"])
        self.assertEqual(summary["results"][0]["status"]["stage"], "failed")


if __name__ == "__main__":
    unittest.main()
