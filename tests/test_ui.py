"""Hardware-free checks for overlay safety and controls-to-engine behavior."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from pc_subtitle_translator.models import Caption
from pc_subtitle_translator.overlay import SubtitleOverlay
from pc_subtitle_translator.settings import Settings
from pc_subtitle_translator.ui import MainWindow


class FakeController(QObject):
    caption = Signal(object)
    source_preview = Signal(str)
    status = Signal(str)
    error = Signal(str)
    level = Signal(float)
    running_changed = Signal(bool)
    stats = Signal(dict)

    def __init__(self):
        super().__init__()
        self.is_running = False
        self.starts = []

    def start(self, settings, api_key, device_id=None):
        self.starts.append((settings, api_key, device_id))
        self.is_running = True
        self.running_changed.emit(True)

    def stop(self):
        if self.is_running:
            self.is_running = False
            self.running_changed.emit(False)

    def shutdown(self, timeout=5):
        self.stop()
        return True


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    instance.setStyle("Fusion")
    yield instance
    # Destroy native windows and their timers before QApplication is finalized.
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    instance.processEvents()


@pytest.fixture
def window(app, monkeypatch):
    monkeypatch.setattr("pc_subtitle_translator.ui.get_api_key", lambda: "")
    monkeypatch.setattr("pc_subtitle_translator.ui.save_settings", lambda settings: None)
    monkeypatch.setattr("pc_subtitle_translator.ui.save_api_key", lambda key: None)
    monkeypatch.setattr("pc_subtitle_translator.ui.delete_api_key", lambda: None)
    widget = MainWindow(controller=FakeController(), settings=Settings(), discover_devices=False)
    yield widget
    widget.close()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def test_language_override_and_appearance_do_not_mutate_running_settings(window, app):
    window.key_edit.setText("fake-test-key")
    window.source_combo.setCurrentIndex(window.source_combo.findData("es"))
    window.target_combo.setCurrentText("English (United Kingdom)")
    window.start_translation()
    saved, key, device = window.controller.starts[0]
    assert saved.source_language == "es"
    assert saved.target_language == "English (United Kingdom)"
    assert key == "fake-test-key"
    assert device is None
    assert not window.routing_card.isEnabled()
    old_size = saved.font_size
    window.font_size.setValue(old_size + 4)
    assert window.overlay.settings.font_size == old_size + 4
    assert saved.font_size == old_size
    window.controller.caption.emit(Caption(2, "Hola", "Hello", True))
    assert window.overlay.target_text == "Hello"
    assert window.target_preview.toPlainText() == "Hello"
    window.controller.stop()
    assert window.overlay.target_text == ""
    assert window.routing_card.isEnabled()


def test_preview_never_starts_capture_and_source_override_requires_code(window):
    window.preview_subtitles()
    assert window.controller.starts == []
    assert window.overlay.target_text
    assert "no audio capture" in window.status_label.text()
    window.key_edit.setText("fake-test-key")
    window.source_combo.setCurrentIndex(window.source_combo.findData("custom"))
    window.custom_source.clear()
    window.start_translation()
    assert not window.controller.starts
    assert "source language code" in window.error_banner.text()


def test_errors_are_plain_text_and_redact_key(window):
    window.key_edit.setText("fake-test-key")
    window._show_error("<b>Cannot connect using fake-test-key</b>")
    assert "fake-test-key" not in window.error_banner.text()
    assert "[API key hidden]" in window.error_banner.text()
    assert window.error_banner.textFormat() == Qt.TextFormat.PlainText


def test_overlay_ignores_stale_events_and_renders_long_multilingual_text(app):
    overlay = SubtitleOverlay(Settings(overlay_width=420, show_original=True))
    overlay.show()
    target = "<b>مرحبا بالعالم</b> 日本語の字幕 😀 " * 20
    overlay.show_caption(Caption(5, "Original source " * 40, target, False))
    app.processEvents()
    assert overlay.target_text == target
    assert overlay._target_layout.text() == target
    assert len(overlay._target_lines) > 3
    assert overlay.height() < 430
    assert not overlay.grab().isNull()
    overlay.show_caption(Caption(4, "stale", "old", True))
    assert overlay.target_text == target
    overlay._expiry.timeout.emit()
    assert overlay.target_text == ""
    overlay.close()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_click_through_can_be_reversed_from_control_panel(window, app):
    window.preview_subtitles()
    position = window.overlay.pos()
    window.lock_overlay.setChecked(True)
    app.processEvents()
    assert window.overlay.windowFlags() & Qt.WindowType.WindowTransparentForInput
    assert window.overlay.isVisible()
    window.lock_overlay.setChecked(False)
    app.processEvents()
    assert not window.overlay.windowFlags() & Qt.WindowType.WindowTransparentForInput
    assert window.overlay.isVisible()
    assert window.overlay.pos() == position


def test_overlay_play_pause_stays_interactive_when_text_is_locked(window, app):
    window.key_edit.setText("fake-test-key")
    window.preview_subtitles()
    window.lock_overlay.setChecked(True)
    app.processEvents()
    control = window.overlay.playback_window
    assert control.isWindow()
    assert control.parentWidget() is None
    assert not control.windowFlags() & Qt.WindowType.WindowTransparentForInput
    assert control.isVisible()
    QTest.mouseClick(control.windowHandle(), Qt.MouseButton.LeftButton, pos=control.rect().center())
    assert len(window.controller.starts) == 1
    assert control.button.running
    QTest.mouseClick(control.windowHandle(), Qt.MouseButton.LeftButton, pos=control.rect().center())
    assert not window.controller.is_running
    assert not control.button.running
    assert window.overlay.isVisible()
    assert control.isVisible()
    assert window.overlay._status == "Paused"


def test_user_height_is_preserved_during_streaming(window, app):
    window.overlay_height.setValue(420)
    window.overlay_width.setValue(600)
    window.overlay.show_caption(Caption(3, "A source.", "A translation. " * 60, False))
    app.processEvents()
    assert window.overlay.height() == 420
    assert window.overlay.width() == 600
    assert len(window.overlay._visible_target_lines) > 3
    window.overlay.show_caption(Caption(3, "A source.", "A shorter translation.", True))
    assert window.overlay.height() == 420
    assert window.settings.overlay_height == 420


def test_saved_offscreen_position_is_recovered(app):
    overlay = SubtitleOverlay(Settings(overlay_x=-99999, overlay_y=99999))
    screen = QApplication.primaryScreen().availableGeometry()
    assert screen.intersects(overlay.geometry())
    assert screen.contains(overlay.pos())
    overlay.close()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
