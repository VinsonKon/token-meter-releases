import asyncio
import json
import unittest
from unittest import mock

import sync


class DiagnosticTests(unittest.TestCase):
    def test_empty_timeout_keeps_stage_and_type(self):
        self.assertEqual("BLE connect/service discovery: TimeoutError",
                         sync.connection_error_detail("BLE connect/service discovery", TimeoutError()))

    def test_descriptive_error_is_preserved(self):
        self.assertEqual("device discovery: TimeoutError: Bound device unavailable",
                         sync.connection_error_detail("device discovery", TimeoutError("Bound device unavailable")))


class AckTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_reports_expected_sequence(self):
        client = mock.Mock()
        client.write_gatt_char = mock.AsyncMock()
        with self.assertRaisesRegex(TimeoutError, "Device ACK timeout for sequence 42"):
            await sync.write_packet_with_ack(client, json.dumps({"n": 42}).encode(), asyncio.Queue(), timeout=0.01)

    async def test_ignores_other_ack_and_accepts_matching_ack(self):
        queue = asyncio.Queue()
        async def write(*args, **kwargs):
            queue.put_nowait(9)
            queue.put_nowait(42)
        client = mock.Mock()
        client.write_gatt_char = mock.AsyncMock(side_effect=write)
        await sync.write_packet_with_ack(client, json.dumps({"n": 42}).encode(), queue, timeout=0.1)

    async def test_cancellation_is_not_wrapped_as_timeout(self):
        client = mock.Mock()
        client.write_gatt_char = mock.AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await sync.write_packet_with_ack(client, b'{"n":42}', asyncio.Queue())
