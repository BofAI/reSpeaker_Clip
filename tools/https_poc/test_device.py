"""Exercise the USB response parser without a physical serial device."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from device import configure_upload, exchange, load_config, main


class ConfigurationTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "client.json"
        self.config = {"ssid": "Clip-Test", "password": "test-password",
                       "host": "192.168.2.78", "port": "8443", "device": "clip-test",
                       "token": "test_token_" + "a" * 32}
        self.write_config(self.config)

    def write_config(self, config):
        self.path.write_text(json.dumps(config))
        self.path.chmod(0o600)

    def test_private_config_and_configuration_commands(self):
        config = load_config(self.path)
        self.assertEqual(config["port"], 8443)
        with patch("device.time.time", return_value=1800000000), \
                patch("device.exchange", return_value={"ok": True}) as query:
            configure_upload(query, config)
        self.assertEqual([call.args[0] for call in query.call_args_list], [
            "AT+NETCFG=Clip-Test,test-password",
            f"AT+UPLOADCFG=192.168.2.78,8443,clip-test,{config['token']}",
            "AT+UPTIME=1800000000",
        ])

    def test_configuration_rejection_stops_later_commands(self):
        with patch("device.exchange", side_effect=RuntimeError("rejected")) as query:
            with self.assertRaisesRegex(RuntimeError, "rejected"):
                configure_upload(query, load_config(self.path))
        self.assertEqual(query.call_count, 1)

    def test_invalid_configuration_does_not_open_serial_or_expose_secrets(self):
        invalid = [("ssid", ""), ("ssid", "a,b"), ("ssid", "录" * 11),
                   ("password", "short"), ("host", "host,8443"),
                   ("device", "device/path"), ("token", "short"),
                   ("port", True), ("port", 0), ("port", 65536), ("port", 8443.5)]
        invalid += [(key, "injected\r\ncommand")
                    for key in ("ssid", "password", "host", "device", "token")]
        for key, value in invalid:
            with self.subTest(key=key, value=value):
                self.write_config(dict(self.config, **{key: value}))
                with patch("device.os.open") as serial_open, \
                        patch("sys.argv", ["device.py", "--port", "/dev/unused",
                                           "--config", str(self.path)]):
                    with self.assertRaises(ValueError) as error:
                        main()
                serial_open.assert_not_called()
                self.assertNotIn(self.config["token"], str(error.exception))
                self.assertNotIn(self.config["password"], str(error.exception))

    def test_missing_fields_and_non_object_config(self):
        for config in ([], None, {}, {k: v for k, v in self.config.items() if k != "port"}):
            with self.subTest(config=config):
                self.write_config(config)
                with self.assertRaises(ValueError):
                    load_config(self.path)

    def test_public_configuration_is_rejected(self):
        self.path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private"):
            load_config(self.path)

    def test_utf8_ssid_and_password_with_commas(self):
        self.write_config(dict(self.config, ssid="录音测试", password="password,with,commas"))
        self.assertEqual(load_config(self.path)["ssid"], "录音测试")


class ExchangeTest(unittest.TestCase):
    def exchange_bytes(self, chunks, **options):
        with patch("device.os.write", side_effect=lambda fd, data: len(data)), \
                patch("device.select.select", return_value=([7], [], [])), \
                patch("device.os.read", side_effect=chunks):
            return exchange(7, "AT+UPLOAD?", **options)

    def test_fragmented_reply(self):
        self.assertEqual(self.exchange_bytes([b'{"ok":tr', b'ue,"active":false}\n']),
                         {"ok": True, "active": False})

    def test_ignore_event_and_non_object_json(self):
        self.assertEqual(self.exchange_bytes([
            b'[]\nnull\n{"event":"state","state":"IDLE"}\n{"ok":true}\n'
        ]), {"ok": True})

    def test_non_boolean_ok_is_not_success(self):
        self.assertEqual(self.exchange_bytes([b'{"ok":"false"}\n{"ok":true}\n']),
                         {"ok": True})

    def test_rejection(self):
        with self.assertRaises(RuntimeError):
            self.exchange_bytes([b'{"ok":false,"error":-16}\n'])

    def test_explicit_rejection_observation_preserves_error(self):
        self.assertEqual(self.exchange_bytes([b'{"ok":false,"error":-16}\n'], allow_rejected=True),
                         {"ok": False, "error": -16})

    def test_disconnect(self):
        with self.assertRaisesRegex(RuntimeError, "disconnected"):
            self.exchange_bytes([b''])

    def test_oversized_response(self):
        with self.assertRaisesRegex(RuntimeError, "too large"):
            self.exchange_bytes([b'x' * 4097])

    def test_command_limit_before_write(self):
        with patch("device.os.write") as write:
            with self.assertRaises(ValueError):
                exchange(7, "X" * 254)
            write.assert_not_called()

    def test_timeout(self):
        with patch("device.os.write", return_value=12), \
                patch("device.time.monotonic", side_effect=[0, 11]):
            with self.assertRaises(TimeoutError):
                exchange(7, "AT+UPLOAD?")

    def test_partial_command_writes(self):
        with patch("device.os.write", side_effect=[3, 9]) as write, \
                patch("device.select.select", return_value=([7], [], [])), \
                patch("device.os.read", return_value=b'{"ok":true}\n'):
            self.assertEqual(exchange(7, "AT+UPLOAD?"), {"ok": True})
            self.assertEqual(write.call_args_list[0].args[1], b'AT+UPLOAD?\r\n')
            self.assertEqual(write.call_args_list[1].args[1], b'UPLOAD?\r\n')


if __name__ == "__main__":
    unittest.main()
