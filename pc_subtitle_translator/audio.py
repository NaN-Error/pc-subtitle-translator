"""Windows speaker loopback capture and stateful, streaming PCM conversion.

No device is opened at import time. Only WASAPI *loopback* devices are accepted;
this module never falls back to a microphone. Audio stays in memory.

``LoopbackCapture.start()`` starts a daemon worker and returns immediately.
All three callbacks run on that worker (not on Qt's GUI thread). ``on_audio``
must enqueue its data and return promptly. The PortAudio callback does only a
bounded in-memory queue operation. If processing cannot keep up, capture stops
with an error instead of silently accumulating seconds of stale captions.

Driver calls and cleanup belong to one worker, so stop does not close a device
under an in-flight callback. A broken OS driver can outlive the two-second join;
``running`` remains true in that case and start refuses to open a second stream.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import math
import queue
import sys
import threading
import time
from typing import Callable

import numpy as np


OUTPUT_SAMPLE_RATE = 24_000
_CAPTURE_BLOCK_MS = 20
_QUEUE_CAPACITY = 50  # Approximately one second; bounded independently of network.


class AudioCaptureError(RuntimeError):
    """An audio device, driver, or capture-processing failure."""


@dataclass(frozen=True)
class AudioDevice:
    id: int
    name: str
    sample_rate: int
    channels: int
    is_default: bool = False


def _load_pyaudio():
    if sys.platform != "win32":
        raise AudioCaptureError(
            "PC audio capture requires Windows 10 or 11. "
            "The interface can be previewed here, but WASAPI is unavailable."
        )
    try:
        return importlib.import_module("pyaudiowpatch")
    except ImportError as exc:
        raise AudioCaptureError(
            "PyAudioWPatch could not be loaded. Run setup_windows.bat again "
            "using 64-bit Python."
        ) from exc


def _device_from_info(info: dict, default_id: int | None = None) -> AudioDevice:
    if not info.get("isLoopbackDevice") or int(info.get("maxInputChannels", 0)) < 1:
        raise AudioCaptureError(
            "The selected device is not a speaker loopback device. "
            "Refresh the device list and select your speakers or headphones."
        )
    return AudioDevice(
        id=int(info["index"]),
        name=str(info["name"]),
        sample_rate=int(round(float(info["defaultSampleRate"]))),
        channels=int(info["maxInputChannels"]),
        is_default=int(info["index"]) == default_id,
    )


def list_loopback_devices() -> list[AudioDevice]:
    """List available speaker/headphone endpoints; raise a helpful error on failure.

    Device indices are process-local PortAudio indices, not permanent hardware
    identifiers. Refresh the list after changing audio devices. The default
    device is resolved again when a capture with ``device_id=None`` starts.
    """
    pyaudio = _load_pyaudio()
    manager = None
    try:
        manager = pyaudio.PyAudio()
        default_id = None
        try:
            default_id = int(manager.get_default_wasapi_loopback()["index"])
        except (OSError, ValueError, LookupError):
            # Explicit device selection is still useful without an OS default.
            pass
        devices = [
            _device_from_info(info, default_id)
            for info in manager.get_loopback_device_info_generator()
            if info.get("isLoopbackDevice") and int(info.get("maxInputChannels", 0)) > 0
        ]
        return sorted(devices, key=lambda item: (not item.is_default, item.name.casefold()))
    except AudioCaptureError:
        raise
    except Exception as exc:
        raise AudioCaptureError(
            "Windows could not list speaker loopback devices. "
            "Check your output device in Windows sound settings and refresh."
        ) from exc
    finally:
        if manager is not None:
            manager.terminate()


class Pcm16Converter:
    """Convert interleaved PCM16 input to mono little-endian PCM16 at 24 kHz.

    A single soxr stream retains filter history across blocks and rejects
    frequencies above the output Nyquist limit before downsampling. ``last``
    flushes its tail and may be used only once. The returned level is RMS in
    [0, 1], measured before resampling. Input must contain complete frames.
    """

    def __init__(self, sample_rate: int, channels: int):
        if not isinstance(sample_rate, int) or not 8_000 <= sample_rate <= 384_000:
            raise ValueError("The input sample rate must be between 8000 and 384000 Hz.")
        if not isinstance(channels, int) or not 1 <= channels <= 32:
            raise ValueError("The input channel count must be between 1 and 32.")
        self.sample_rate = sample_rate
        self.channels = channels
        self._finished = False
        self._resampler = None
        if sample_rate != OUTPUT_SAMPLE_RATE:
            try:
                soxr = importlib.import_module("soxr")
            except ImportError as exc:
                raise AudioCaptureError(
                    "The audio resampler is missing. Run setup_windows.bat again."
                ) from exc
            self._resampler = soxr.ResampleStream(
                sample_rate, OUTPUT_SAMPLE_RATE, 1, dtype="float32", quality="HQ"
            )

    def process(self, pcm: bytes, *, last: bool = False) -> tuple[bytes, float]:
        if self._finished:
            raise ValueError("The resampler has already been finalized.")
        if len(pcm) % (self.channels * 2):
            raise ValueError("PCM data ends in an incomplete audio frame.")
        frames = np.frombuffer(pcm, dtype="<i2").reshape(-1, self.channels)
        if len(frames):
            mono = frames.astype(np.float32).mean(axis=1, dtype=np.float32) / 32768.0
            level = float(np.sqrt(np.mean(mono * mono, dtype=np.float64)))
        else:
            mono = np.empty(0, dtype=np.float32)
            level = 0.0
        if self._resampler is not None:
            output = self._resampler.resample_chunk(mono, last=last)
        else:
            output = mono
        if last:
            self._finished = True
        encoded = np.clip(np.rint(output * 32768.0), -32768, 32767).astype("<i2")
        return encoded.tobytes(), min(1.0, max(0.0, level))


class LoopbackCapture:
    """Capture a selected Windows playback device without recording a file.

    ``device_id=None`` selects the current default output at each start. Only
    this device's output is captured; reroute other applications to it in the
    Windows mixer if necessary. Changing default output while running requires
    stop/start. ``on_audio`` receives nonempty mono PCM16 blocks at 24 kHz;
    ``on_level`` receives linear RMS levels no more than ten times per second.
    ``on_error`` reports one fatal error per run, after cleanup has started.
    """

    def __init__(
        self,
        device_id: int | None,
        on_audio: Callable[[bytes], None],
        on_level: Callable[[float], None],
        on_error: Callable[[str], None],
    ):
        self.device_id = device_id
        self.on_audio = on_audio
        self.on_level = on_level
        self.on_error = on_error
        self.current_device: AudioDevice | None = None
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=_QUEUE_CAPACITY)
        self._capture_issue: str | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._state_lock:
            if self.running:
                raise AudioCaptureError("Audio capture is already running or still stopping.")
            self._stop_event.clear()
            self._capture_issue = None
            self.current_device = None
            self._queue = queue.Queue(maxsize=_QUEUE_CAPACITY)
            self._thread = threading.Thread(
                target=self._run, name="PC audio capture", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        """Stop promptly and discard unsent buffered audio; safe to call repeatedly."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _run(self) -> None:
        manager = None
        stream = None
        error: str | None = None
        try:
            pyaudio = _load_pyaudio()
            if self._stop_event.is_set():
                return
            manager = pyaudio.PyAudio()
            if self.device_id is None:
                info = manager.get_default_wasapi_loopback()
            else:
                info = manager.get_device_info_by_index(self.device_id)
            device = _device_from_info(info)
            self.current_device = device
            converter = Pcm16Converter(device.sample_rate, device.channels)

            def capture_callback(in_data, _frame_count, _time_info, status):
                if self._stop_event.is_set():
                    return None, pyaudio.paComplete
                if status:
                    self._capture_issue = (
                        "Windows reported an audio capture discontinuity. "
                        "Stop other audio-heavy tasks, check the output device, and restart."
                    )
                    self._stop_event.set()
                    return None, pyaudio.paAbort
                if in_data:
                    try:
                        self._queue.put_nowait(in_data)
                    except queue.Full:
                        self._capture_issue = (
                            "Audio processing could not keep up. Capture stopped to avoid "
                            "delayed subtitles. Close heavy applications and restart."
                        )
                        self._stop_event.set()
                        return None, pyaudio.paAbort
                return None, pyaudio.paContinue

            if self._stop_event.is_set():
                return
            stream = manager.open(
                format=pyaudio.paInt16,
                channels=device.channels,
                rate=device.sample_rate,
                frames_per_buffer=max(128, math.ceil(device.sample_rate * _CAPTURE_BLOCK_MS / 1000)),
                input=True,
                input_device_index=device.id,
                stream_callback=capture_callback,
                start=False,
            )
            if self._stop_event.is_set():
                return
            stream.start_stream()
            last_meter = 0.0
            while not self._stop_event.is_set():
                try:
                    pcm = self._queue.get(timeout=0.1)
                except queue.Empty:
                    if not stream.is_active():
                        raise AudioCaptureError(
                            "Audio capture stopped. The output device may have disconnected. "
                            "Refresh the devices and start again."
                        )
                    continue
                if self._stop_event.is_set():
                    break
                output, level = converter.process(pcm)
                if output and not self._stop_event.is_set():
                    self.on_audio(output)
                now = time.monotonic()
                if now - last_meter >= 0.1 and not self._stop_event.is_set():
                    self.on_level(level)
                    last_meter = now
            if self._capture_issue:
                error = self._capture_issue
        except AudioCaptureError as exc:
            error = str(exc)
        except Exception as exc:
            error = (
                "Could not capture PC audio. Check that the selected speakers or "
                "headphones are connected, refresh devices, and try again. "
                f"({type(exc).__name__}: {exc})"
            )
        finally:
            # These native resources are owned and closed only by this worker.
            if stream is not None:
                try:
                    if stream.is_active():
                        stream.stop_stream()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
            if manager is not None:
                try:
                    manager.terminate()
                except Exception:
                    pass
            self._stop_event.set()
            # Retain no queued source audio after the session ends.
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
            try:
                self.on_level(0.0)
            except Exception:
                pass
            if error:
                try:
                    self.on_error(error)
                except Exception:
                    pass
