"""Signal correctness and driver-boundary tests; no Windows hardware required."""

from __future__ import annotations

import threading
import types

import numpy as np
import pytest

from pc_subtitle_translator import audio


def pcm(values):
    return np.asarray(values, dtype="<i2").tobytes()


def test_mono_conversion_preserves_full_int16_range_and_byte_order():
    values = [-32768, -12345, -1, 0, 1, 12345, 32767]
    converter = audio.Pcm16Converter(24_000, 1)
    result, level = converter.process(pcm(values), last=True)
    assert result == pcm(values)
    assert 0 < level <= 1


def test_stereo_downmix_does_not_overflow_and_reports_silence():
    converter = audio.Pcm16Converter(24_000, 2)
    result, _ = converter.process(pcm([[32767, 32767], [-32768, -32768]]))
    assert np.frombuffer(result, "<i2").tolist() == [32767, -32768]
    result, level = converter.process(pcm([[10000, -10000], [0, 0]]), last=True)
    assert result == bytes(4)
    assert level == 0.0


@pytest.mark.parametrize("rate", [44_100, 48_000, 96_000])
def test_streaming_resampling_matches_continuous_processing_and_duration(rate):
    soxr = pytest.importorskip("soxr")
    samples = np.rint(np.sin(2 * np.pi * 700 * np.arange(rate) / rate) * 18000).astype("<i2")
    converter = audio.Pcm16Converter(rate, 2)
    pieces = []
    # Irregular boundaries expose a stateless-per-block resampling bug.
    for start in range(0, rate, 713):
        mono = samples[start : start + 713]
        block = np.stack((mono, mono), axis=1)
        result, _ = converter.process(block.tobytes())
        pieces.append(result)
    tail, _ = converter.process(b"", last=True)
    pieces.append(tail)
    output = np.frombuffer(b"".join(pieces), dtype="<i2")
    expected = soxr.resample(samples.astype(np.float32) / 32768.0, rate, 24_000, quality="HQ")
    expected = np.clip(np.rint(expected * 32768), -32768, 32767).astype("<i2")
    assert len(output) == 24_000
    np.testing.assert_allclose(output.astype(np.int32), expected.astype(np.int32), atol=1)


def test_resampler_removes_above_nyquist_energy():
    pytest.importorskip("soxr")
    rate = 48_000
    samples = np.rint(np.sin(2 * np.pi * 18_000 * np.arange(rate) / rate) * 20000).astype("<i2")
    result, _ = audio.Pcm16Converter(rate, 1).process(samples.tobytes(), last=True)
    output = np.frombuffer(result, "<i2").astype(np.float64)
    # Skip filter start/end transients; a naive decimator would retain a strong tone.
    assert np.sqrt(np.mean(output[1000:-1000] ** 2)) < 10


def test_resampler_rejects_incomplete_frames_and_reuse_after_finalization():
    converter = audio.Pcm16Converter(24_000, 2)
    with pytest.raises(ValueError, match="incomplete"):
        converter.process(bytes(2))
    converter.process(b"", last=True)
    with pytest.raises(ValueError, match="finalized"):
        converter.process(b"")


@pytest.mark.parametrize("rate,channels", [(0, 1), (24_000, 0), (24_000, 99)])
def test_resampler_rejects_invalid_device_format(rate, channels):
    with pytest.raises(ValueError):
        audio.Pcm16Converter(rate, channels)


def fake_driver(*, loopback=True, callback_status=0):
    info = {
        "index": 7, "name": "Test speakers [Loopback]", "defaultSampleRate": 24_000,
        "maxInputChannels": 2, "isLoopbackDevice": loopback,
    }
    state = types.SimpleNamespace(closed=False, terminated=False, opened=False, active=False)

    class Stream:
        def __init__(self, callback):
            self.callback = callback

        def start_stream(self):
            state.active = True
            _, status = self.callback(pcm([[4000, 4000]] * 480), 480, {}, callback_status)
            if status != driver.paContinue:
                state.active = False

        def is_active(self):
            return state.active

        def stop_stream(self):
            state.active = False

        def close(self):
            state.closed = True

    class Manager:
        def get_default_wasapi_loopback(self):
            return info

        def get_device_info_by_index(self, index):
            assert index == 7
            return info

        def get_loopback_device_info_generator(self):
            yield info

        def open(self, **kwargs):
            state.opened = True
            assert kwargs["input_device_index"] == 7
            assert kwargs["input"] and not kwargs["start"]
            return Stream(kwargs["stream_callback"])

        def terminate(self):
            state.terminated = True

    driver = types.SimpleNamespace(PyAudio=Manager, paInt16=8, paContinue=0, paComplete=1, paAbort=2)
    return driver, state


def test_device_listing_only_returns_loopback_and_marks_default(monkeypatch):
    driver, state = fake_driver()
    monkeypatch.setattr(audio, "_load_pyaudio", lambda: driver)
    devices = audio.list_loopback_devices()
    assert devices == [audio.AudioDevice(7, "Test speakers [Loopback]", 24_000, 2, True)]
    assert state.terminated


def test_capture_outputs_mono_and_releases_resources_on_stop(monkeypatch):
    driver, state = fake_driver()
    monkeypatch.setattr(audio, "_load_pyaudio", lambda: driver)
    received = []
    errors = []
    delivered = threading.Event()

    def receive(data):
        received.append(data)
        delivered.set()

    capture = audio.LoopbackCapture(None, receive, lambda _: None, errors.append)
    capture.start()
    assert delivered.wait(timeout=2)
    capture.stop()
    assert not capture.running
    assert state.closed and state.terminated
    assert received == [pcm([4000] * 480)]
    assert not errors
    capture.stop()  # Idempotent.


def test_capture_refuses_microphone_without_opening_stream(monkeypatch):
    driver, state = fake_driver(loopback=False)
    monkeypatch.setattr(audio, "_load_pyaudio", lambda: driver)
    errors = []
    failed = threading.Event()

    def fail(message):
        errors.append(message)
        failed.set()

    capture = audio.LoopbackCapture(7, lambda _: None, lambda _: None, fail)
    capture.start()
    assert failed.wait(timeout=2)
    capture.stop()
    assert not state.opened
    assert state.terminated
    assert "not a speaker loopback" in errors[0]


def test_driver_discontinuity_is_reported_and_closed(monkeypatch):
    driver, state = fake_driver(callback_status=2)
    monkeypatch.setattr(audio, "_load_pyaudio", lambda: driver)
    errors = []
    failed = threading.Event()

    def fail(message):
        errors.append(message)
        failed.set()

    capture = audio.LoopbackCapture(None, lambda _: None, lambda _: None, fail)
    capture.start()
    assert failed.wait(timeout=2)
    capture.stop()
    assert state.closed and state.terminated
    assert "discontinuity" in errors[0]


def test_platform_error_is_explicit(monkeypatch):
    monkeypatch.setattr(audio.sys, "platform", "linux")
    with pytest.raises(audio.AudioCaptureError, match="requires Windows"):
        audio.list_loopback_devices()
