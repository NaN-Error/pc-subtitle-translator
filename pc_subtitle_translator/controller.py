"""Qt bridge; all audio and network work stays off the GUI thread."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import threading

from PySide6.QtCore import QObject, Signal

from .engine import Callbacks, LiveSession
from .protocol import redact
from .settings import Settings


class Controller(QObject):
    caption = Signal(object)
    source_preview = Signal(str)
    status = Signal(str)
    error = Signal(str)
    level = Signal(float)
    running_changed = Signal(bool)
    stats = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, settings: Settings, api_key: str, device_id: int | None = None) -> None:
        if self.is_running:
            raise RuntimeError("The previous session is still stopping. Please wait.")
        snapshot = replace(settings).validated()
        api_key = api_key.strip()
        if not api_key or len(api_key) > 4096 or any(c.isspace() for c in api_key):
            raise ValueError("Enter your OpenAI API key in settings.")
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._worker, args=(snapshot, api_key, device_id),
                                        name="Live translation", daemon=True)
        self.running_changed.emit(True)
        self._thread.start()

    def stop(self) -> None:
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        loop, task = self._loop, self._task
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass
        if self.is_running:
            self.status.emit("Stopping…")

    def shutdown(self, timeout: float = 5.0) -> bool:
        self.stop()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, timeout))
        return not self.is_running

    def _worker(self, settings: Settings, api_key: str, device_id: int | None) -> None:
        async def work():
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()
            callbacks = Callbacks(self.caption.emit, self.source_preview.emit,
                                  self.status.emit, self.level.emit, self.stats.emit)
            session = LiveSession(settings, api_key, device_id, self._stop_event, callbacks)
            if not self._stop_event.is_set():
                await session.run()

        failed = False
        try:
            asyncio.run(work())
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            failed = True
            message = redact(str(exc), api_key) or "Translation stopped unexpectedly."
            self.error.emit(message)
            self.status.emit("Stopped · " + message)
        finally:
            self._loop = None
            self._task = None
            self.level.emit(0.0)
            if not failed:
                self.status.emit("Stopped · no audio is being sent")
            self.running_changed.emit(False)
