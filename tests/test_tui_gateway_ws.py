import asyncio
import concurrent.futures
import json
import threading
import time

from hermes_cli import mcp_startup
from tui_gateway import server
from tui_gateway import ws as ws_mod




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




def test_ws_connection_registers_then_disconnect_unregisters_live_transport(monkeypatch):
    """A connected client must be tracked in the live-transport registry so a
    session-less global broadcast (skin.changed from the background watcher)
    reaches it, and dropped on disconnect so no stale write targets a dead peer.
    This is the WS half of the cross-surface live-theme fix."""
    server._sessions.clear()
    server._live_transports.clear()
    seen = {}
    try:
        _run_disconnect(
            monkeypatch,
            lambda t: seen.__setitem__("registered", t in server._live_transports),
        )
        # Seeded at receive_text time — i.e. after gateway.ready registered it.
        assert seen["registered"] is True
        # handle_ws's finally must have unregistered it.
        assert not server._live_transports
    finally:
        server._sessions.clear()
        server._live_transports.clear()


def test_ws_disconnect_releases_wake_word_owner(monkeypatch):
    released = []
    created = []
    monkeypatch.setattr(
        server,
        "_release_wake_for_transport",
        lambda transport: released.append(transport) or True,
    )

    _run_disconnect(monkeypatch, lambda transport: created.append(transport))

    assert released == created




def test_ws_starts_mcp_discovery_before_ready(monkeypatch):
    import tui_gateway.entry as entry

    calls = []
    events = []

    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0)
    monkeypatch.setattr(entry, "ensure_mcp_discovery_started", lambda: calls.append("mcp"))

    class FakeWS:
        async def accept(self):
            events.append("accept")

        async def send_text(self, line):
            if '"gateway.ready"' in line:
                events.append(f"ready_after_{len(calls)}")

        async def receive_text(self):
            raise ws_mod._WebSocketDisconnect()

        async def close(self):
            pass

    asyncio.run(ws_mod.handle_ws(FakeWS()))

    # Discovery moved to profile-aware agent construction. WebSocket transport
    # should not start MCP discovery before a profile has been bound.
    assert calls == []
    assert events == ["accept", "ready_after_0"]


def test_ws_transport_serializes_concurrent_sends():
    active_sends = 0
    max_active_sends = 0
    sent = []

    class FakeWS:
        async def send_text(self, line):
            nonlocal active_sends, max_active_sends
            active_sends += 1
            max_active_sends = max(max_active_sends, active_sends)
            try:
                await asyncio.sleep(0.05)
                sent.append(line)
            finally:
                active_sends -= 1

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        transport = ws_mod.WSTransport(FakeWS(), loop, peer="serialize-test")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(transport.write, {"idx": 1}),
                pool.submit(transport.write, {"idx": 2}),
            ]
            assert [f.result(timeout=2) for f in futures] == [True, True]

        assert len(sent) == 2
        assert max_active_sends == 1
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


def test_handle_ws_disconnect_joins_blocked_writer(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setattr(
        mcp_startup,
        "start_background_mcp_discovery",
        lambda **_kwargs: None,
    )

    created: list[ws_mod.WSTransport] = []
    real_transport = ws_mod.WSTransport
    monkeypatch.setattr(
        ws_mod,
        "WSTransport",
        lambda ws, loop, **kwargs: created.append(
            real_transport(ws, loop, **kwargs)
        )
        or created[-1],
    )

    class BlockingWS:
        def __init__(self):
            self.calls = 0
            self.pending_send_started = asyncio.Event()
            self.never_release = asyncio.Event()
            self.closed = False

        async def accept(self):
            pass

        async def send_text(self, _line):
            self.calls += 1
            if self.calls > 1:
                self.pending_send_started.set()
                await self.never_release.wait()

        async def receive_text(self):
            transport = created[0]
            transport._enqueue_send([json.dumps({"queued": True})])
            await asyncio.wait_for(self.pending_send_started.wait(), timeout=1)
            raise ws_mod._WebSocketDisconnect()

        async def close(self):
            self.closed = True

    ws = BlockingWS()
    server._sessions.clear()
    try:
        asyncio.run(ws_mod.handle_ws(ws))
    finally:
        server._sessions.clear()

    assert created[0]._writer_task is not None
    assert created[0]._writer_task.done()
    assert ws.closed is True
def test_ws_transport_preserves_cross_batch_order():
    async def scenario():
        entered = []
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        second_started = asyncio.Event()

        class FakeWS:
            async def send_text(self, line):
                entered.append(line)
                if line == "A1":
                    first_entered.set()
                    await release_first.wait()

        transport = ws_mod.WSTransport(
            FakeWS(), asyncio.get_running_loop(), peer="batch-order-test"
        )
        first = asyncio.create_task(
            transport._enqueue_send_and_wait(["A1", "A2"])
        )
        await first_entered.wait()

        async def send_second():
            second_started.set()
            return await transport._enqueue_send_and_wait(["B1", "B2"])

        second = asyncio.create_task(send_second())
        await second_started.wait()

        # The second task has reached the transport. Without whole-batch
        # serialization it runs B1/B2 before this task can resume.
        assert entered == ["A1"]

        release_first.set()
        assert await asyncio.gather(first, second) == [True, True]
        assert entered == ["A1", "A2", "B1", "B2"]
        await transport.aclose()

    asyncio.run(scenario())
