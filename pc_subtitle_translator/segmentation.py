"""Bounded client-side audio turns, including devices that go idle without frames.

This is an energy gate, not a semantic speech detector. Music/noise can open it.
The maximum turn length bounds delay during continuous speech or background music.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math

import numpy as np

SAMPLE_RATE = 24_000
BYTES_PER_SECOND = SAMPLE_RATE * 2
FRAME_MS = 20
FRAME_BYTES = BYTES_PER_SECOND * FRAME_MS // 1000
MIN_TURN_BYTES = BYTES_PER_SECOND // 4


@dataclass(frozen=True)
class AppendAudio:
    data: bytes


@dataclass(frozen=True)
class CommitAudio:
    start_seconds: float
    end_seconds: float


class EnergySegmenter:
    def __init__(self, *, threshold_db: float = -50, pause_ms: int = 450,
                 max_seconds: float = 3.0, pre_roll_ms: int = 300):
        self.threshold = 10 ** (threshold_db / 20)
        self.pause_frames = math.ceil(pause_ms / FRAME_MS)
        self.max_frames = math.ceil(max_seconds * 1000 / FRAME_MS)
        self.pause_seconds = pause_ms / 1000
        self._pre_roll: deque[bytes] = deque(maxlen=math.ceil(pre_roll_ms / FRAME_MS))
        self._pending = bytearray()
        self._active = False
        self._turn_frames = 0
        self._quiet_frames = 0
        self._start = 0.0
        self._end = 0.0
        self._last_feed = 0.0

    @property
    def active(self) -> bool:
        return self._active

    def feed(self, pcm: bytes, now: float) -> list[AppendAudio | CommitAudio]:
        if len(pcm) % 2:
            raise ValueError("PCM input must contain complete 16-bit samples.")
        if not pcm:
            return []
        self._last_feed = now
        self._pending.extend(pcm)
        messages: list[AppendAudio | CommitAudio] = []
        while len(self._pending) >= FRAME_BYTES:
            frame = bytes(self._pending[:FRAME_BYTES])
            del self._pending[:FRAME_BYTES]
            end_time = now - len(self._pending) / BYTES_PER_SECOND
            samples = np.frombuffer(frame, dtype="<i2").astype(np.float32) / 32768.0
            audible = float(np.sqrt(np.mean(samples * samples))) >= self.threshold
            if not self._active:
                if not audible:
                    self._pre_roll.append(frame)
                    continue
                prefix = b"".join(self._pre_roll)
                self._turn_frames = len(self._pre_roll) + 1
                self._pre_roll.clear()
                self._quiet_frames = 0
                self._active = True
                self._start = max(0, end_time - self._turn_frames * FRAME_MS / 1000)
                messages.append(AppendAudio(prefix + frame))
            else:
                messages.append(AppendAudio(frame))
                self._turn_frames += 1
                self._quiet_frames = 0 if audible else self._quiet_frames + 1
            self._end = end_time
            if self._turn_frames >= self.max_frames or self._quiet_frames >= self.pause_frames:
                messages.extend(self._commit())
        return messages

    def idle(self, now: float) -> list[AppendAudio | CommitAudio]:
        """Commit when WASAPI stops producing frames after playback is paused."""
        if self._active and now - self._last_feed >= self.pause_seconds:
            tail = bytes(self._pending)
            self._pending.clear()
            return self._commit(tail=tail)
        if not self._active and now - self._last_feed >= self.pause_seconds:
            # Do not carry old pre-roll across a long, device-level audio gap.
            self._pre_roll.clear()
            self._pending.clear()
        return []

    def _commit(self, *, tail: bytes = b"") -> list[AppendAudio | CommitAudio]:
        if not self._active:
            return []
        messages: list[AppendAudio | CommitAudio] = []
        size = self._turn_frames * FRAME_BYTES + len(tail)
        if tail:
            messages.append(AppendAudio(tail))
        if size < MIN_TURN_BYTES:
            # A brief sound followed by an idle device still needs a nonempty turn.
            messages.append(AppendAudio(bytes(MIN_TURN_BYTES - size)))
        messages.append(CommitAudio(self._start, self._end))
        self._active = False
        self._turn_frames = 0
        self._quiet_frames = 0
        return messages
