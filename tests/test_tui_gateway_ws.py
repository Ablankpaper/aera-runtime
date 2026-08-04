import asyncio
import json
import threading
import time

from hermes_cli import mcp_startup
from tui_gateway import server
from tui_gateway import ws as ws_mod


def test_ws_startup_starts_background_mcp_discovery(monkeypatch):
    """The desktop app and dashboard chat reach the agent through this WS
    sidecar, not through tui_gateway.entry.main() (which spawns the discovery
    thread for the stdio TUI). handle_ws must start discovery itself, otherwise
    _make_agent's wait_for_mcp_discovery no-ops and the agent snapshots an
    MCP-less tool list. Regression test for #38945."""
    calls = []
    monkeypatch.setattr(
        mcp_startup,
        "start_background_mcp_discovery",
        lambda **kw: calls.append(kw),
    )

    class FakeWS:
        async def accept(self):
            pass

        async def send_text(self, line):
            pass

        async def receive_text(self):
            raise ws_mod._WebSocketDisconnect()

        async def close(self):
            pass

    server._sessions.clear()
    try:
        asyncio.run(ws_mod.handle_ws(FakeWS()))
    finally:
        server._sessions.clear()

    assert calls == [{"logger": ws_mod._log, "thread_name": "tui-ws-mcp-discovery"}]


def _run_disconnect(monkeypatch, seed):
    """Drive handle_ws to its disconnect `finally`, seeding sessions against the
    live WSTransport the moment it exists. Returns nothing; inspect _sessions."""
    # Disable the grace-reap Timer: detached sessions normally schedule a
    # threading.Timer via _schedule_ws_orphan_reap, which would outlive the test
    # and fire _reap during interpreter teardown — touching _sessions/DB and
    # producing spurious post-run errors under the per-file CI runner. Grace=0
    # short-circuits the Timer (see _schedule_ws_orphan_reap) so the test leaves
    # no lingering thread.
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0)

    # Mirror the real _finalize_session chokepoint: it is the single place that
    # closes the slash-worker (#38095). Stub it but keep that behavior so the
    # disconnect-reap path still exercises worker teardown.
    def _fake_finalize(s, end_reason="tui_close"):
        w = s.get("slash_worker")
        if w:
            w.close()

    monkeypatch.setattr(server, "_finalize_session", _fake_finalize)

    created = []
    real_transport = ws_mod.WSTransport
    monkeypatch.setattr(
        ws_mod, "WSTransport",
        lambda ws, loop, **kw: created.append(real_transport(ws, loop, **kw)) or created[-1],
    )

    class FakeWS:
        async def accept(self):
            pass

        async def send_text(self, line):
            pass

        async def receive_text(self):
            seed(created[0])  # transport now exists; attach it to sessions
            raise ws_mod._WebSocketDisconnect()

        async def close(self):
            pass

    asyncio.run(ws_mod.handle_ws(FakeWS()))


def test_ws_disconnect_reaps_flagged_session_and_closes_worker(monkeypatch):
    closed = []

    class FakeWorker:
        def close(self):
            closed.append(True)

    server._sessions.clear()
    try:
        _run_disconnect(
            monkeypatch,
            lambda t: server._sessions.update(
                flagged={
                    "transport": t,
                    "close_on_disconnect": True,
                    "slash_worker": FakeWorker(),
                    "session_key": "k",
                }
            ),
        )
        assert "flagged" not in server._sessions
        assert closed == [True]
    finally:
        server._sessions.clear()


def test_ws_disconnect_preserves_and_repoints_reconnectable_session(monkeypatch):
    server._sessions.clear()
    try:
        _run_disconnect(
            monkeypatch,
            lambda t: server._sessions.update(
                plain={"transport": t, "close_on_disconnect": False, "session_key": "k"}
            ),
        )
        assert server._sessions["plain"]["transport"] is server._detached_ws_transport
    finally:
        server._sessions.clear()


def test_ws_write_loop_stall_does_not_latch_transport(monkeypatch):
    """A write that times out because the event loop is stalled (GIL-heavy
    agent turn) must NOT latch the transport closed — the frame is already
    scheduled and flushes when the loop recovers. Latching here permanently
    silenced live watch windows after one slow write."""
    monkeypatch.setattr(ws_mod, "_WS_WRITE_TIMEOUT_S", 0.05)
    sent = []

    class FakeWS:
        async def send_text(self, line):
            sent.append(line)

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        transport = ws_mod.WSTransport(FakeWS(), loop, peer="stall-test")
        # Stall the loop well past the write timeout, then write from this
        # (non-loop) thread: the wait times out but the send stays in flight.
        loop.call_soon_threadsafe(time.sleep, 0.3)
        assert transport.write({"a": 1}) is True
        assert transport._closed is False

        # Once the loop breathes again, both the stalled frame and new writes
        # must reach the socket.
        assert transport.write({"b": 2}) is True
        deadline = time.time() + 2
        while len(sent) < 2 and time.time() < deadline:
            time.sleep(0.01)
        assert len(sent) == 2
        assert transport._closed is False
        transport.close()
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_ws_single_writer_orders_coalesced_frames_before_completion(monkeypatch):
    monkeypatch.setattr(ws_mod, "_TOKEN_COALESCE_S", 0)

    async def run():
        class YieldingWS:
            def __init__(self):
                self.active = 0
                self.max_active = 0
                self.first_send_started = asyncio.Event()
                self.release_first_send = asyncio.Event()
                self.sent: list[str] = []

            async def send_text(self, line):
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                if not self.first_send_started.is_set():
                    self.first_send_started.set()
                    await self.release_first_send.wait()
                await asyncio.sleep(0)
                self.sent.append(line)
                self.active -= 1

        ws = YieldingWS()
        transport = ws_mod.WSTransport(
            ws, asyncio.get_running_loop(), peer="single-writer-order"
        )

        for seq in range(1, 1001):
            assert transport.write(
                {
                    "jsonrpc": "2.0",
                    "method": "event",
                    "params": {
                        "type": "message.delta",
                        "payload": {"seq": seq, "text": str(seq)},
                    },
                }
            )

        await asyncio.wait_for(ws.first_send_started.wait(), timeout=1)
        assert transport.write(
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "type": "message.complete",
                    "payload": {"final_seq": 1000, "text": "complete"},
                },
            }
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        ws.release_first_send.set()

        deadline = asyncio.get_running_loop().time() + 5
        while len(ws.sent) < 1001 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.001)

        decoded = [json.loads(line) for line in ws.sent]
        decoded_seq = [
            frame["params"]["payload"]["seq"]
            for frame in decoded
            if frame["params"]["type"] == "message.delta"
        ]
        assert decoded_seq == list(range(1, 1001))
        assert decoded[-1]["params"]["type"] == "message.complete"
        assert ws.max_active == 1
        transport.close()

    asyncio.run(run())


def test_ws_single_writer_drops_completion_after_earlier_socket_failure(monkeypatch):
    monkeypatch.setattr(ws_mod, "_TOKEN_COALESCE_S", 0)

    async def run():
        class FailingWS:
            def __init__(self):
                self.calls = 0
                self.first_send_started = asyncio.Event()
                self.release_failure = asyncio.Event()
                self.sent: list[str] = []

            async def send_text(self, line):
                self.calls += 1
                if self.calls == 1:
                    self.first_send_started.set()
                    await self.release_failure.wait()
                    raise OSError("socket failed")
                self.sent.append(line)

        ws = FailingWS()
        transport = ws_mod.WSTransport(
            ws, asyncio.get_running_loop(), peer="single-writer-failure"
        )
        assert transport.write(
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "type": "message.delta",
                    "payload": {"seq": 1, "text": "一"},
                },
            }
        )

        await asyncio.wait_for(ws.first_send_started.wait(), timeout=1)
        assert transport.write(
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "type": "message.complete",
                    "payload": {"final_seq": 1, "text": "一"},
                },
            }
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        ws.release_failure.set()

        deadline = asyncio.get_running_loop().time() + 1
        while not transport._closed and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.001)

        sent_types = [json.loads(line)["params"]["type"] for line in ws.sent]
        assert transport._closed is True
        assert "message.complete" not in sent_types
        transport.close()

    asyncio.run(run())
