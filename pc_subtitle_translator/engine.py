"""Cancellable realtime audio pipeline with bounded queues and ordered subtitles."""

from __future__ import annotations

import asyncio
import base64
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
import json
import queue
import threading
import time
from typing import Callable

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from .audio import LoopbackCapture
from .models import Caption
from .protocol import (
    ApiError, PendingTurn, REALTIME_URL, Transcript, TranscriptOrder,
    api_error, redact, session_update,
)
from .segmentation import AppendAudio, CommitAudio, EnergySegmenter, BYTES_PER_SECOND
from .settings import Settings
from .translation import TextTranslator


@dataclass
class Callbacks:
    caption: Callable[[Caption], None] = lambda _: None
    source_preview: Callable[[str], None] = lambda _: None
    status: Callable[[str], None] = lambda _: None
    level: Callable[[float], None] = lambda _: None
    stats: Callable[[dict], None] = lambda _: None


class LiveSession:
    """One user-initiated run. Stop cancels in-flight work and discards unsent audio."""

    def __init__(self, settings: Settings, api_key: str, device_id: int | None,
                 stop_event: threading.Event, callbacks: Callbacks,
                 *, capture_factory=LoopbackCapture, connector=connect,
                 translator_factory=TextTranslator, client_factory=httpx.AsyncClient):
        self.settings = settings
        self.api_key = api_key
        self.device_id = device_id
        self.stop_event = stop_event
        self.callbacks = callbacks
        self.capture_factory = capture_factory
        self.connector = connector
        self.translator_factory = translator_factory
        self.client_factory = client_factory
        self.sequence = 0
        self.started = time.monotonic()
        self.stats = {"audio_seconds": 0.0, "translated_segments": 0,
                      "dropped_segments": 0, "translation_ms": 0.0}
        self._last_stats = 0.0
        self._capture = None
        self._ever_connected = False

    async def run(self) -> None:
        failures = 0
        while not self.stop_event.is_set():
            try:
                await self._connection()
                return
            except asyncio.CancelledError:
                raise
            except ApiError as exc:
                if exc.fatal:
                    raise
                message = str(exc)
            except InvalidStatus as exc:
                code = exc.response.status_code
                if code < 500:
                    raise api_error({}, self.api_key, code) from None
                message = f"The realtime service returned HTTP {code}."
            except (OSError, TimeoutError, httpx.HTTPError) as exc:
                message = "The connection was interrupted."
            except Exception as exc:
                # ConnectionClosed is intentionally handled without exposing handshake headers.
                from websockets.exceptions import ConnectionClosed
                if not isinstance(exc, ConnectionClosed):
                    raise
                message = "The realtime connection closed."
            if self.stop_event.is_set():
                return
            failures += 1
            if failures > 3:
                raise ApiError(f"{message} Automatic reconnection failed. Check your internet connection and press Start again.")
            self.callbacks.source_preview("")
            self.callbacks.status(
                f"{message} Reconnecting ({failures}/3). Audio during this gap is skipped."
            )
            deadline = time.monotonic() + min(2 ** (failures - 1), 4)
            while not self.stop_event.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.05)

    async def _connection(self) -> None:
        self.callbacks.status("Connecting to OpenAI…")
        async with self.connector(
            REALTIME_URL,
            additional_headers={"Authorization": f"Bearer {self.api_key}"},
            compression=None, open_timeout=12, close_timeout=2,
            ping_interval=20, ping_timeout=20, max_size=1_048_576, max_queue=16,
        ) as ws:
            await ws.send(json.dumps(session_update(self.settings)))
            async with asyncio.timeout(12):
                while True:
                    event = json.loads(await ws.recv())
                    if event.get("type") == "error":
                        raise api_error(event, self.api_key)
                    if event.get("type") == "session.updated":
                        break
            if self.stop_event.is_set():
                return
            self._ever_connected = True
            self._audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=100)
            self._capture_issue: queue.Queue[str] = queue.Queue(maxsize=1)
            self._pending_commits: deque[PendingTurn] = deque()
            self._order = TranscriptOrder()
            self._translations: asyncio.Queue[Transcript] = asyncio.Queue(maxsize=3)
            self._segmenter = EnergySegmenter(
                threshold_db=self.settings.silence_threshold_db,
                pause_ms=self.settings.pause_ms,
                max_seconds=self.settings.max_segment_seconds,
            )
            self._capture = self.capture_factory(
                self.device_id, self._on_audio, self.callbacks.level, self._on_capture_error,
            )
            timeout = httpx.Timeout(connect=10, read=12, write=10, pool=5)
            async with self.client_factory(timeout=timeout, follow_redirects=False) as client:
                translator = self.translator_factory(self.api_key, self.settings, client)
                tasks = [asyncio.create_task(self._send(ws)),
                         asyncio.create_task(self._receive(ws)),
                         asyncio.create_task(self._translate(translator))]
                try:
                    self._capture.start()
                    self.callbacks.status("Listening to PC audio · " + self.settings.target_language)
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    # Mark capture callbacks inactive before joining the native worker.
                    capture = self._capture
                    self._capture = None
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await asyncio.to_thread(capture.stop)
                    if capture.running:
                        raise ApiError("The Windows audio driver is still stopping. Close the app and check your output device before restarting.")
                    self.callbacks.level(0.0)

    def _on_audio(self, pcm: bytes) -> None:
        if self.stop_event.is_set() or self._capture is None:
            return
        try:
            self._audio_queue.put_nowait(pcm)
        except queue.Full:
            self._on_capture_error("Audio could not be sent fast enough. Check the connection and restart translation.")

    def _on_capture_error(self, message: str) -> None:
        with suppress(queue.Full):
            self._capture_issue.put_nowait(message)

    async def _send(self, ws) -> None:
        while not self.stop_event.is_set():
            try:
                problem = self._capture_issue.get_nowait()
            except queue.Empty:
                pass
            else:
                raise ApiError(problem)
            now = time.monotonic()
            actions = []
            for _ in range(10):
                try:
                    pcm = self._audio_queue.get_nowait()
                except queue.Empty:
                    break
                actions.extend(self._segmenter.feed(pcm, now - self.started))
            actions.extend(self._segmenter.idle(now - self.started))
            # Combine adjacent 20ms frames into fewer network messages, preserving commits.
            audio = bytearray()
            for action in actions:
                if isinstance(action, AppendAudio):
                    audio.extend(action.data)
                else:
                    if audio:
                        await self._append(ws, bytes(audio))
                        audio.clear()
                    self.sequence += 1
                    self._pending_commits.append(PendingTurn(
                        self.sequence, action.start_seconds, action.end_seconds, now,
                    ))
                    if len(self._pending_commits) > 32:
                        raise ApiError("OpenAI stopped acknowledging audio segments. Restart translation.")
                    await asyncio.wait_for(ws.send(json.dumps({"type": "input_audio_buffer.commit"})), 5)
            if audio:
                await self._append(ws, bytes(audio))
            if self._pending_commits and now - self._pending_commits[0].created_at > 12:
                raise ApiError("OpenAI did not acknowledge an audio segment in time.", fatal=False)
            self._drain_transcripts(now)
            self._emit_stats(now)
            await asyncio.sleep(0.015)

    async def _append(self, ws, pcm: bytes) -> None:
        if self.stop_event.is_set():
            return
        await asyncio.wait_for(ws.send(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm).decode("ascii"),
        })), 5)
        self.stats["audio_seconds"] += len(pcm) / BYTES_PER_SECOND

    async def _receive(self, ws) -> None:
        async for raw in ws:
            event = json.loads(raw)
            kind = event.get("type", "")
            item_id = str(event.get("item_id", ""))
            if kind == "error":
                raise api_error(event, self.api_key)
            if kind == "input_audio_buffer.committed":
                if self._order.acknowledged(item_id):
                    continue
                if not item_id or not self._pending_commits:
                    raise ApiError("The service returned an unexpected audio segment acknowledgment.")
                self._order.commit(item_id, self._pending_commits.popleft())
            elif kind == "conversation.item.input_audio_transcription.delta" and item_id:
                partial = self._order.partial(item_id, str(event.get("delta", "")))
                if partial:
                    self.callbacks.source_preview(partial)
            elif kind == "conversation.item.input_audio_transcription.completed" and item_id:
                self._order.complete(item_id, str(event.get("transcript", "")))
            elif kind == "conversation.item.input_audio_transcription.failed" and item_id:
                failure = api_error(event, self.api_key)
                self._order.complete(item_id, "", str(failure))
                error_code = event.get("error", {}).get("code", "")
                if error_code in {"invalid_api_key", "insufficient_quota", "rate_limit_exceeded", "model_not_found"}:
                    raise failure
            self._drain_transcripts(time.monotonic())
        if not self.stop_event.is_set():
            raise ApiError("The realtime audio connection ended.", fatal=False)

    def _drain_transcripts(self, now: float) -> None:
        for transcript in self._order.ready(now):
            if transcript.error:
                self.stats["dropped_segments"] += 1
                self.callbacks.status(transcript.error)
                continue
            if not transcript.text:
                continue
            if self._translations.full():
                self._translations.get_nowait()
                self._translations.task_done()
                self.stats["dropped_segments"] += 1
                self.callbacks.status("A delayed subtitle was skipped to keep up with live audio.")
            self._translations.put_nowait(transcript)

    async def _translate(self, translator: TextTranslator) -> None:
        context: deque[dict] = deque(maxlen=2)
        failures = 0
        while not self.stop_event.is_set():
            transcript = await self._translations.get()
            turn = transcript.turn
            try:
                if time.monotonic() - turn.created_at > 12:
                    self.stats["dropped_segments"] += 1
                    self.callbacks.status("A late subtitle was skipped to keep up with playback.")
                    continue
                started = time.monotonic()

                def update(text: str) -> None:
                    if not self.stop_event.is_set():
                        self.callbacks.caption(Caption(turn.sequence, transcript.text, text, False,
                                                       turn.start_seconds, turn.end_seconds))

                try:
                    # A slow in-flight response must not bypass the queue's age limit.
                    remaining = max(0.001, 12 - (started - turn.created_at))
                    async with asyncio.timeout(remaining):
                        target = await translator.translate(transcript.text, list(context), update)
                except asyncio.CancelledError:
                    raise
                except (ApiError, httpx.HTTPError, TimeoutError) as exc:
                    if isinstance(exc, ApiError) and exc.fatal:
                        raise
                    failures += 1
                    self.stats["dropped_segments"] += 1
                    self.callbacks.status("A translation was interrupted; waiting for the next segment.")
                    # Clear a partial result instead of leaving it looking finalized.
                    self.callbacks.caption(Caption(turn.sequence, "", "", True))
                    if failures >= 3:
                        raise ApiError("Translation failed repeatedly. Check the connection and selected text model, then restart.") from None
                    continue
                failures = 0
                self.stats["translation_ms"] = (time.monotonic() - started) * 1000
                self.stats["translated_segments"] += 1
                if not self.stop_event.is_set():
                    self.callbacks.caption(Caption(turn.sequence, transcript.text, target, True,
                                                   turn.start_seconds, turn.end_seconds))
                context.append({"source": transcript.text, "translation": target})
                self._emit_stats(time.monotonic(), force=True)
            finally:
                self._translations.task_done()

    def _emit_stats(self, now: float, *, force: bool = False) -> None:
        if force or now - self._last_stats >= 0.5:
            self._last_stats = now
            self.callbacks.stats(dict(self.stats))
