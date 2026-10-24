from __future__ import annotations

import asyncio
import json
import threading
import time

import numpy as np
import pytest

from pc_subtitle_translator.engine import Callbacks, LiveSession
from pc_subtitle_translator.protocol import ApiError, PendingTurn, Transcript, TranscriptOrder
from pc_subtitle_translator.settings import Settings


class FakeWebSocket:
    def __init__(self, *, auto_ack=True, complete_pairs=False):
        self.auto_ack = auto_ack
        self.complete_pairs = complete_pairs
        self.incoming = asyncio.Queue()
        self.session_requested = asyncio.Event()
        self.sent = []
        self.commits = 0
        self.closed = False

    def push(self, value):
        self.incoming.put_nowait(json.dumps(value))

    async def send(self, raw):
        event = json.loads(raw)
        self.sent.append(event)
        if event["type"] == "session.update":
            self.session_requested.set()
            self.push({"type": "session.created"})
            if self.auto_ack:
                self.push({"type": "session.updated"})
        elif event["type"] == "input_audio_buffer.commit":
            self.commits += 1
            item_id = f"item-{self.commits}"
            # A duplicate acknowledgment must not consume the next pending turn.
            if self.commits == 2:
                self.push({"type": "input_audio_buffer.committed", "item_id": "item-1"})
            self.push({"type": "input_audio_buffer.committed", "item_id": item_id})
            if self.complete_pairs and self.commits == 2:
                self.push({"type": "conversation.item.input_audio_transcription.completed", "item_id": "item-2", "transcript": "second"})
                self.push({"type": "conversation.item.input_audio_transcription.completed", "item_id": "item-1", "transcript": "first"})
                self.push({"type": "conversation.item.input_audio_transcription.completed", "item_id": "item-1", "transcript": "stale duplicate"})
            elif not self.complete_pairs:
                self.push({"type": "conversation.item.input_audio_transcription.completed", "item_id": item_id, "transcript": "hello"})

    async def recv(self):
        return await self.incoming.get()

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.recv()


class Connection:
    def __init__(self, ws):
        self.ws = ws

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, *args):
        self.ws.closed = True


class FakeClient:
    def __init__(self, **kwargs):
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True


class Harness:
    def __init__(self, *, ws=None, seconds=0, block_translation=False):
        self.ws = ws or FakeWebSocket()
        self.stop = threading.Event()
        self.captures = []
        self.clients = []
        self.caption_events = []
        self.statuses = []
        self.translated = []
        self.capture_started = asyncio.Event()
        self.translation_started = asyncio.Event()
        self.translation_cancelled = asyncio.Event()
        self.two_final = asyncio.Event()
        self.block_translation = block_translation
        owner = self

        class Capture:
            def __init__(self, device_id, on_audio, on_level, on_error):
                self.on_audio = on_audio
                self.running = False
                self.stopped = False
                owner.captures.append(self)

            def start(self):
                self.running = True
                owner.capture_started.set()
                if seconds:
                    self.on_audio(np.full(int(seconds * 24_000), 6000, dtype="<i2").tobytes())

            def stop(self):
                self.stopped = True
                self.running = False

        class Translator:
            def __init__(self, *args):
                pass

            async def translate(self, text, context, on_delta):
                owner.translated.append((text, context))
                owner.translation_started.set()
                on_delta("partial " + text)
                if owner.block_translation:
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        owner.translation_cancelled.set()
                        raise
                await asyncio.sleep(0)
                return "translated " + text

        def client_factory(**kwargs):
            client = FakeClient(**kwargs)
            owner.clients.append(client)
            return client

        def caption(value):
            owner.caption_events.append(value)
            if sum(bool(item.final) for item in owner.caption_events) >= 2:
                owner.two_final.set()

        self.session = LiveSession(
            Settings(max_segment_seconds=1.0, pause_ms=250),
            "fake-api-key", None, self.stop,
            Callbacks(caption=caption, status=self.statuses.append),
            capture_factory=Capture, connector=lambda *args, **kwargs: Connection(self.ws),
            translator_factory=Translator, client_factory=client_factory,
        )
        self.session.started = time.monotonic() - 3

    async def cancel(self, task):
        self.stop.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_capture_waits_until_session_updated_and_stop_closes_every_resource():
    h = Harness(ws=FakeWebSocket(auto_ack=False))
    task = asyncio.create_task(h.session.run())
    await asyncio.wait_for(h.ws.session_requested.wait(), 1)
    assert not h.captures
    assert all(item["type"] != "input_audio_buffer.append" for item in h.ws.sent)
    h.ws.push({"type": "session.updated"})
    await asyncio.wait_for(h.capture_started.wait(), 1)
    await h.cancel(task)
    assert h.captures[0].stopped and not h.captures[0].running
    assert h.clients[0].closed and h.ws.closed


@pytest.mark.asyncio
async def test_stop_during_handshake_never_opens_audio_device():
    h = Harness(ws=FakeWebSocket(auto_ack=False))
    task = asyncio.create_task(h.session.run())
    await asyncio.wait_for(h.ws.session_requested.wait(), 1)
    await h.cancel(task)
    assert not h.captures
    assert h.ws.closed


@pytest.mark.asyncio
async def test_end_to_end_reorders_final_transcripts_ignores_duplicates_and_preserves_context():
    h = Harness(ws=FakeWebSocket(complete_pairs=True), seconds=2)
    task = asyncio.create_task(h.session.run())
    try:
        await asyncio.wait_for(h.two_final.wait(), 2)
        finals = [item for item in h.caption_events if item.final]
        assert [(item.sequence, item.source, item.target) for item in finals] == [
            (1, "first", "translated first"), (2, "second", "translated second"),
        ]
        assert h.translated == [
            ("first", []),
            ("second", [{"source": "first", "translation": "translated first"}]),
        ]
        assert h.session.stats["translated_segments"] == 2
        assert h.session.stats["audio_seconds"] == pytest.approx(2)
    finally:
        await h.cancel(task)


@pytest.mark.asyncio
async def test_stop_cancels_in_flight_translation_and_does_not_publish_a_stale_final():
    h = Harness(seconds=1, block_translation=True)
    task = asyncio.create_task(h.session.run())
    await asyncio.wait_for(h.translation_started.wait(), 1)
    await h.cancel(task)
    assert h.translation_cancelled.is_set()
    assert h.captures[0].stopped
    assert not any(item.final for item in h.caption_events)
    before = len(h.caption_events)
    h.captures[0].on_audio(bytes(960))
    assert len(h.caption_events) == before


@pytest.mark.asyncio
async def test_backlog_keeps_latest_three_transcripts_instead_of_growing_unbounded():
    h = Harness()
    session = h.session
    session._order = TranscriptOrder()
    session._translations = asyncio.Queue(maxsize=3)
    now = time.monotonic()
    for i in range(1, 7):
        session._order.commit(str(i), PendingTurn(i, i - 1, i, now))
        session._order.complete(str(i), f"segment-{i}")
    session._drain_transcripts(now)
    assert session.stats["dropped_segments"] == 3
    assert [session._translations.get_nowait().turn.sequence for _ in range(3)] == [4, 5, 6]


@pytest.mark.asyncio
async def test_expired_transcript_is_skipped_before_charging_for_translation():
    h = Harness()
    session = h.session
    session._translations = asyncio.Queue()
    now = time.monotonic()
    session._translations.put_nowait(Transcript(PendingTurn(1, 0, 1, now - 13), "expired"))
    called = []

    class Translator:
        async def translate(self, *args):
            called.append(args)
            return "unexpected"

    task = asyncio.create_task(session._translate(Translator()))
    await asyncio.wait_for(session._translations.join(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not called
    assert session.stats["dropped_segments"] == 1


@pytest.mark.asyncio
async def test_translation_is_cancelled_at_remaining_freshness_budget_and_partial_is_cleared():
    h = Harness()
    session = h.session
    session._translations = asyncio.Queue()
    now = time.monotonic()
    # Only about 40 ms of the commit's 12-second freshness budget remains.
    session._translations.put_nowait(Transcript(PendingTurn(8, 0, 1, now - 11.96), "old speech"))
    cancelled = asyncio.Event()

    class Translator:
        async def translate(self, text, context, on_delta):
            on_delta("unfinished subtitle")
            try:
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return "this final would be too late"

    task = asyncio.create_task(session._translate(Translator()))
    try:
        await asyncio.wait_for(session._translations.join(), 1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert cancelled.is_set()
    assert session.stats["dropped_segments"] == 1
    assert session.stats["translated_segments"] == 0
    assert [(item.target, item.final) for item in h.caption_events] == [
        ("unfinished subtitle", False), ("", True),
    ]


@pytest.mark.asyncio
async def test_account_error_before_handshake_never_starts_audio():
    h = Harness(ws=FakeWebSocket(auto_ack=False))
    task = asyncio.create_task(h.session.run())
    await asyncio.wait_for(h.ws.session_requested.wait(), 1)
    h.ws.push({"type": "error", "error": {"code": "invalid_api_key", "message": "wrong fake-api-key"}})
    with pytest.raises(ApiError, match="rejected"):
        await asyncio.wait_for(task, 1)
    assert not h.captures
    assert h.ws.closed


def test_repeated_controller_stop_does_not_cancel_resource_cleanup_twice(monkeypatch):
    from pc_subtitle_translator import controller as module

    started = threading.Event()
    cleanup_started = threading.Event()
    allow_cleanup = threading.Event()
    cleanup_finished = threading.Event()

    class Session:
        def __init__(self, *args):
            pass

        async def run(self):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await asyncio.to_thread(allow_cleanup.wait, 2)
                cleanup_finished.set()

    monkeypatch.setattr(module, "LiveSession", Session)
    controller = module.Controller()
    controller.start(Settings(), "fake-api-key")
    try:
        assert started.wait(1)
        controller.stop()
        assert cleanup_started.wait(1)
        controller.stop()
    finally:
        allow_cleanup.set()
        assert controller.shutdown(timeout=3)
    assert cleanup_finished.is_set()
