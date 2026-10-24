"""Render real Qt widgets with sample captions; no API key, audio, or saved preferences.

Usage: QT_QPA_PLATFORM=offscreen python scripts/render_preview.py OUTPUT_DIR
This is a visual QA utility, not a live translation demonstration.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QApplication

from pc_subtitle_translator import ui
from pc_subtitle_translator.settings import Settings


class PreviewController(QObject):
    caption = Signal(object)
    source_preview = Signal(str)
    status = Signal(str)
    error = Signal(str)
    level = Signal(float)
    running_changed = Signal(bool)
    stats = Signal(dict)
    is_running = False

    def start(self, *args):
        raise RuntimeError("The screenshot utility cannot start audio capture.")

    def stop(self):
        pass

    def shutdown(self, timeout=5):
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setStyle(ui.CaptionStyle())
    # A screenshot must not load, display, save, or remove an actual user's key.
    ui.get_api_key = lambda: ""
    ui.save_settings = lambda *_: None
    ui.save_api_key = lambda *_: None
    ui.delete_api_key = lambda: None
    settings = Settings(target_language="Spanish (Puerto Rico)", show_original=True)
    window = ui.MainWindow(controller=PreviewController(), settings=settings, discover_devices=False)
    window.resize(880, 1000)
    window.show()
    window.preview_subtitles()
    app.processEvents()
    window.grab().save(str(args.output / "pc-subtitle-translator.png"))
    window.tabs.setCurrentIndex(1)
    app.processEvents()
    window.grab().save(str(args.output / "appearance-settings.png"))
    window.tabs.setCurrentIndex(2)
    app.processEvents()
    window.grab().save(str(args.output / "openai-model-settings.png"))
    overlay = window.overlay
    overlay.resize(980, 220)
    app.processEvents()
    picture = overlay.grab()
    painter = QPainter(picture)
    painter.drawPixmap(overlay.width() - 62, (overlay.height() - 44) // 2,
                       overlay.playback_window.grab())
    painter.end()
    picture.save(str(args.output / "subtitle-overlay.png"))
    window.close()
    app.processEvents()
    print(f"Rendered 4 sample UI previews in {args.output.resolve()}")


if __name__ == "__main__":
    main()
