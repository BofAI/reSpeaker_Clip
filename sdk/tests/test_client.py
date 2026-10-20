from __future__ import annotations

import asyncio

import pytest

from clip import ClipClient, CommandError, SessionDetails

from conftest import FakeTransport


@pytest.mark.asyncio
async def test_optional_resources_and_argument_validation() -> None:
    transport = FakeTransport([
        {"ok": True, "data": {"cpu_bp": -1}},
        {"ok": True, "data": {"index": 2, "stack_valid": False}},
        {"ok": False, "msg": "Unknown command"},
    ])
    async with ClipClient(transport) as client:
        assert await client.resource_stats() == {"cpu_bp": -1}
        assert (await client.resource_stats(2))["stack_valid"] is False
        for index in (-1, 256, True, "0", 1.5):
            with pytest.raises(ValueError):
                await client.resource_stats(index)
        with pytest.raises(CommandError, match="Unknown command"):
            await client.resource_stats()
    assert transport.commands == ["AT+RES", "AT+RES=2", "AT+RES"]


@pytest.mark.asyncio
async def test_current_firmware_status_and_storage_commands() -> None:
    transport = FakeTransport(
        [
            {"ok": True, "data": {"state": "IDLE", "recording": False, "session": None, "duration": 0,
                                  "battery": 78, "charging": False, "temp": 25, "voltage": 3890,
                                  "mode": "enhanced", "bitrate": 32000, "free_space": 1000, "device": "Clip 8673"}},
            {"ok": True, "data": {"mounted": True, "total_mb": 1000, "free_mb": 750,
                                  "used_mb": 250, "used_pct": 25, "recorded_mb": 200}},
        ]
    )
    async with ClipClient(transport) as client:
        status = await client.status()
        storage = await client.storage()
    assert status.battery_percent == 78
    assert status.record_pending is False
    assert status.record_error == 0
    assert storage.used_percent == 25
    assert transport.commands == ["AT+GSTAT", "AT+STORAGE?"]


@pytest.mark.asyncio
async def test_deferred_recording_is_not_reported_as_started() -> None:
    transport = FakeTransport([
        {"ok": True, "data": {"pending": True, "session": None}},
        {"ok": True, "data": {"state": "IDLE", "recording": False,
                              "record_pending": True, "record_error": 0}},
        {"ok": True, "data": {"state": "RECORDING", "recording": True,
                              "session": "20261019070446", "record_pending": False}},
        {"ok": True, "data": {"state": "IDLE", "recording": False,
                              "record_pending": False, "record_error": -5}},
    ])
    async with ClipClient(transport) as client:
        assert await client.start_recording("enhanced") is None
        pending = await client.status()
        assert pending.record_pending and not pending.recording
        started = await client.status()
        assert started.recording and not started.record_pending
        assert started.session_id == "20261019070446"
        failed = await client.status()
        assert not failed.record_pending and failed.record_error == -5


@pytest.mark.asyncio
async def test_device_error_uses_msg_not_legacy_error() -> None:
    transport = FakeTransport([{"ok": False, "msg": "Invalid session ID"}])
    async with ClipClient(transport) as client:
        with pytest.raises(CommandError, match="Invalid session ID") as caught:
            await client.request("AT+LIST=bad")
    assert caught.value.command == "AT+LIST=bad"


@pytest.mark.asyncio
async def test_pagination_uses_current_list_syntax() -> None:
    sid = "20260716022113"
    transport = FakeTransport(
        [
            {"ok": True, "data": {"total": 2, "page": 1, "per_page": 2,
                                  "sessions": [{"id": sid, "files": 1, "size": 1, "bookmarks": 0},
                                               {"id": "20260716022000", "files": 2, "size": 2, "bookmarks": 1}]}},
            {"ok": True, "data": {"files": 1, "size": 10, "synced": 0, "bookmarks": 0,
                                  "channels": 1, "sample_rate": 16000, "mode": "enhanced"}},
        ]
    )
    async with ClipClient(transport) as client:
        sessions = await client.list_all_sessions(per_page=2)
        details = await client.session_details(sid)
    assert [item.id for item in sessions] == [sid, "20260716022000"]
    assert details.sample_rate_hz == 16000
    assert transport.commands == ["AT+LIST?1&2", f"AT+LIST={sid}"]


@pytest.mark.asyncio
async def test_destructive_operations_require_local_confirmation() -> None:
    transport = FakeTransport()
    async with ClipClient(transport) as client:
        with pytest.raises(ValueError, match="confirm=True"):
            await client.delete_session("20260716022113")
        with pytest.raises(ValueError, match="confirm=True"):
            await client.format_storage()
    assert transport.commands == []


@pytest.mark.asyncio
async def test_client_serializes_concurrent_at_commands() -> None:
    class SlowTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__([{"ok": True}, {"ok": True}])
            self.in_flight = 0
            self.maximum_in_flight = 0

        async def send_command(self, command: str, *, timeout: float) -> dict:
            self.in_flight += 1
            self.maximum_in_flight = max(self.maximum_in_flight, self.in_flight)
            await asyncio.sleep(0)
            self.in_flight -= 1
            return await super().send_command(command, timeout=timeout)

    transport = SlowTransport()
    async with ClipClient(transport) as client:
        await asyncio.gather(client.request("AT+GSTAT"), client.request("AT+VERSION"))
    assert transport.maximum_in_flight == 1
