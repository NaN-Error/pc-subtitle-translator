from __future__ import annotations

import numpy as np
import pytest

from pc_subtitle_translator.segmentation import (
    AppendAudio, CommitAudio, EnergySegmenter, FRAME_BYTES, BYTES_PER_SECOND, MIN_TURN_BYTES,
)


def audio_bytes(frames=1, value=1000):
    return np.full(frames * FRAME_BYTES // 2, value, dtype="<i2").tobytes()


def sent(actions):
    return b"".join(item.data for item in actions if isinstance(item, AppendAudio))


def commits(actions):
    return [item for item in actions if isinstance(item, CommitAudio)]


def test_silence_does_not_create_an_audio_request():
    segmenter = EnergySegmenter()
    assert segmenter.feed(bytes(BYTES_PER_SECOND), 1) == []
    assert segmenter.idle(2) == []
    assert not segmenter.active


def test_continuous_speech_is_bounded_and_all_bytes_are_preserved():
    segmenter = EnergySegmenter(max_seconds=1.0)
    raw = audio_bytes(125)
    actions = segmenter.feed(raw, 2.5)
    actions += segmenter.idle(3.0)
    assert len(commits(actions)) == 3
    assert sent(actions) == raw
    assert [(round(c.start_seconds, 2), round(c.end_seconds, 2)) for c in commits(actions)] == [
        (0, 1), (1, 2), (2, 2.5),
    ]


def test_irregular_input_chunks_preserve_pcm_without_extra_duplicates():
    segmenter = EnergySegmenter(max_seconds=1.0)
    raw = np.arange(BYTES_PER_SECOND, dtype="<i2").tobytes()
    # Avoid energy falling below the gate at the very beginning.
    segmenter.threshold = 0
    actions = []
    pos = 0
    while pos < len(raw):
        end = min(pos + 1514, len(raw))
        actions += segmenter.feed(raw[pos:end], end / BYTES_PER_SECOND)
        pos = end
    actions += segmenter.idle(3)
    assert sent(actions) == raw
    assert len(commits(actions)) == 2


def test_pre_roll_is_bounded_and_included_once_with_speech():
    segmenter = EnergySegmenter(pre_roll_ms=60, max_seconds=1)
    silence = bytes(5 * FRAME_BYTES)
    assert segmenter.feed(silence, 0.1) == []
    raw = audio_bytes(20)
    actions = segmenter.feed(raw, 0.5) + segmenter.idle(1)
    assert sent(actions) == bytes(3 * FRAME_BYTES) + raw
    assert len(commits(actions)) == 1
    again = segmenter.feed(audio_bytes(20, 2000), 1.4) + segmenter.idle(2)
    assert sent(again) == audio_bytes(20, 2000)


def test_idle_device_flushes_tail_samples_instead_of_dropping_them():
    segmenter = EnergySegmenter(pause_ms=250)
    raw = audio_bytes(20) + np.full(137, 2222, dtype="<i2").tobytes()
    actions = segmenter.feed(raw, 1)
    assert segmenter.active
    actions += segmenter.idle(1.251)
    assert len(commits(actions)) == 1
    assert sent(actions) == raw
    assert not segmenter.active
    assert segmenter.idle(2) == []


def test_brief_sound_idle_commit_meets_minimum_input_without_losing_sound():
    segmenter = EnergySegmenter(pause_ms=250)
    raw = audio_bytes(2)
    actions = segmenter.feed(raw, 1) + segmenter.idle(1.251)
    transmitted = sent(actions)
    assert transmitted.startswith(raw)
    assert len(transmitted) == MIN_TURN_BYTES
    assert transmitted[len(raw):] == bytes(MIN_TURN_BYTES - len(raw))
    assert len(commits(actions)) == 1


def test_pause_after_speech_commits_once_without_reusing_sent_silence():
    segmenter = EnergySegmenter(pause_ms=300)
    raw = audio_bytes(20)
    quiet = bytes(15 * FRAME_BYTES)
    actions = segmenter.feed(raw, 0.4) + segmenter.feed(quiet, 0.7)
    assert len(commits(actions)) == 1
    assert sent(actions) == raw + quiet
    assert segmenter.idle(1.1) == []


def test_long_device_idle_does_not_carry_old_preroll_into_new_playback():
    segmenter = EnergySegmenter(pre_roll_ms=300, pause_ms=250)
    assert segmenter.feed(bytes(3 * FRAME_BYTES + 100), 1) == []
    assert segmenter.idle(2) == []
    raw = audio_bytes(20)
    assert sent(segmenter.feed(raw, 2.4)) == raw


def test_incomplete_pcm_sample_is_rejected():
    with pytest.raises(ValueError, match="16-bit"):
        EnergySegmenter().feed(b"\x01", 1)
