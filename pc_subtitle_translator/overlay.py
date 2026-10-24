"""A native, movable caption window with bounded, Unicode-aware text layout."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QMouseEvent, QPainter, QPen, QPolygonF, QTextLayout, QTextOption
from PySide6.QtWidgets import QAbstractButton, QApplication, QMenu, QSizeGrip, QWidget

from .models import Caption
from .settings import Settings


def _layout(text: str, font: QFont, width: float) -> tuple[QTextLayout, list]:
    # QTextLayout preserves shaping and bidirectional ordering. No HTML is parsed.
    layout = QTextLayout(text.replace("\r", "").replace("\n", "\u2028"), font)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    option.setAlignment(Qt.AlignmentFlag.AlignHCenter)
    layout.setTextOption(option)
    lines = []
    layout.beginLayout()
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(max(1.0, width))
        lines.append(line)
    layout.endLayout()
    return layout, lines


class _PlaybackButton(QAbstractButton):
    def __init__(self, parent):
        super().__init__(parent)
        self.running = False
        self.setFixedSize(44, 44)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Start translation")
        self.setAccessibleName("Start translation")

    def set_running(self, running: bool) -> None:
        self.running = running
        label = "Pause translation" if running else "Start or resume translation"
        self.setToolTip(label)
        self.setAccessibleName(label)
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor("#274b4b") if self.underMouse() else QColor("#153d3d"))
        painter.setPen(QPen(QColor("#74dcca"), 1))
        painter.drawRoundedRect(QRectF(1, 1, 42, 42), 12, 12)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#8af0dc"))
        if self.running:
            painter.drawRoundedRect(QRectF(14, 13, 6, 18), 1.5, 1.5)
            painter.drawRoundedRect(QRectF(25, 13, 6, 18), 1.5, 1.5)
        else:
            painter.drawPolygon(QPolygonF([QPointF(17, 12), QPointF(32, 22), QPointF(17, 32)]))


class _PlaybackWindow(QWidget):
    """A separate native input surface survives the caption's click-through flag."""

    playback_clicked = Signal()

    def __init__(self, overlay):
        # Keep this out of the caption's widget ancestry: Qt's transparent-mouse
        # attribute applies to descendants as well as the widget itself.
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setWindowTitle("Subtitle playback")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setFixedSize(44, 44)
        self.button = _PlaybackButton(self)
        self.button.clicked.connect(self.playback_clicked)
        overlay.destroyed.connect(self.deleteLater)


class SubtitleOverlay(QWidget):
    """Shows the newest translated lines that fit the user-sized text area.

    Streaming updates replace the current caption in place. When a very long
    caption grows beyond the window, its earlier lines scroll out of view; its
    complete text remains visible in the control panel.
    """

    geometry_changed = Signal(int, int, int, int)
    lock_requested = Signal(bool)
    hidden_by_user = Signal()
    playback_requested = Signal(bool)

    def __init__(self, settings: Settings, parent: QWidget | None = None):
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        super().__init__(parent, flags)
        self.setWindowTitle("Subtitles")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMinimumWidth(360)
        self.setMaximumWidth(2400)
        self.setMinimumHeight(110)
        self.setMaximumHeight(700)
        self.settings = replace(settings)
        self._source = ""
        self._target = ""
        self._sequence = -1
        self._drag_offset: QPoint | None = None
        self._initializing = True
        self._preview = False
        self._running = False
        self._status = "Waiting for speech"
        self._target_layout: QTextLayout | None = None
        self._target_lines: list = []
        self._source_layout: QTextLayout | None = None
        self._source_lines: list = []
        self._visible_target_lines: list = []
        self._visible_source_lines: list = []
        self._target_height = 0.0
        self._source_height = 0.0
        self._expiry = QTimer(self)
        self._expiry.setSingleShot(True)
        self._expiry.timeout.connect(self.clear_caption)
        self.grip = QSizeGrip(self)
        self.grip.setFixedSize(20, 20)
        self.grip.setStyleSheet("background: transparent;")
        self.grip.setToolTip("Drag to resize the subtitle window")
        self.playback_window = _PlaybackWindow(self)
        self.playback_window.playback_clicked.connect(self._request_playback)
        self.resize(settings.overlay_width, settings.overlay_height)
        self.apply_settings(settings)
        self.restore_position()
        self._initializing = False

    @property
    def source_text(self) -> str:
        return self._source

    @property
    def target_text(self) -> str:
        return self._target

    @Slot()
    def _request_playback(self) -> None:
        self.playback_requested.emit(not self._running)

    def apply_settings(self, settings: Settings) -> None:
        was_visible = self.isVisible()
        old_pos = self.pos()
        locked_changed = bool(self.windowFlags() & Qt.WindowType.WindowTransparentForInput) != settings.overlay_locked
        self.settings = replace(settings)
        self.resize(settings.overlay_width, settings.overlay_height)
        if locked_changed:
            # Clear the widget attribute before recreating native window flags;
            # otherwise Qt can re-add transparent input while unlocking.
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, settings.overlay_locked)
            self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, settings.overlay_locked)
            self.move(old_pos)
            if was_visible:
                self.show()
        self.grip.setVisible(not settings.overlay_locked)
        self.setCursor(Qt.CursorShape.ArrowCursor if settings.overlay_locked else Qt.CursorShape.SizeAllCursor)
        self._reflow()
        self._position_playback()
        self.update()

    def restore_position(self) -> None:
        screens = QApplication.screens()
        if not screens:
            return
        x, y = self.settings.overlay_x, self.settings.overlay_y
        if x is None or y is None:
            self.reset_position()
            return
        point = QPoint(x, y)
        screen = next((s for s in screens if s.availableGeometry().contains(point)), QApplication.primaryScreen())
        area = screen.availableGeometry()
        self.resize(min(self.width(), area.width()), self.height())
        self.move(
            max(area.left(), min(x, area.right() - self.width() + 1)),
            max(area.top(), min(y, area.bottom() - self.height() + 1)),
        )

    def reset_position(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.resize(min(self.settings.overlay_width, area.width()), self.height())
        self.move(area.left() + (area.width() - self.width()) // 2, area.bottom() - self.height() - 60)
        self._emit_geometry()

    def begin_session(self) -> None:
        self._sequence = -1
        self._status = "Listening for audio"
        self._preview = False
        self._running = True
        self.playback_window.button.set_running(True)
        self.clear_caption()

    def end_session(self) -> None:
        self._status = "Paused"
        self._running = False
        self.playback_window.button.set_running(False)
        self.clear_caption()

    def show_caption(self, caption: Caption, *, preview: bool = False) -> None:
        if not preview and caption.sequence < self._sequence:
            return
        self._sequence = caption.sequence
        self._source = caption.source
        self._target = caption.target
        self._preview = preview
        self._status = "Appearance preview" if preview else ("Live subtitles" if caption.final else "Translating")
        self.setAccessibleName("Subtitles: " + caption.target)
        self._reflow()
        self._expiry.start(round(self.settings.hold_seconds * 1000))
        self.update()

    def clear_caption(self) -> None:
        self._expiry.stop()
        self._source = ""
        self._target = ""
        self.setAccessibleName("Subtitles: " + self._status)
        self._reflow()
        self.update()

    def _reflow(self) -> None:
        text_width = max(1, self.width() - 108)
        target_font = QFont(self.settings.font_family, self.settings.font_size)
        target_font.setWeight(QFont.Weight.DemiBold)
        self._target_layout, self._target_lines = _layout(self._target, target_font, text_width)
        source_font = QFont(self.settings.font_family, max(12, round(self.settings.font_size * 0.62)))
        source_text = self._source if self.settings.show_original and self._target else ""
        self._source_layout, self._source_lines = _layout(source_text, source_font, text_width)
        target_line_height = QFontMetricsF(target_font).height()
        source_line_height = QFontMetricsF(source_font).height()
        min_height = round(64 + target_line_height + (source_line_height + 10 if self.settings.show_original else 0))
        self.setMinimumHeight(max(110, min_height))
        available = max(target_line_height, self.height() - 64)
        source_count = min(2, max(1, int(available / 3 / source_line_height))) if source_text else 0
        self._visible_source_lines = self._source_lines[-source_count:] if source_count else []
        self._source_height = sum(line.height() for line in self._visible_source_lines)
        target_available = available - (self._source_height + 10 if source_count else 0)
        target_count = max(1, int(target_available / target_line_height))
        self._visible_target_lines = self._target_lines[-target_count:]
        self._target_height = sum(line.height() for line in self._visible_target_lines)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        alpha = round(self.settings.background_opacity * 2.55)
        painter.setBrush(QColor(10, 17, 29, alpha))
        painter.setPen(QPen(QColor(145, 180, 195, 65 if alpha else 0), 1))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 15, 15)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#67e8d0") if not self._preview else QColor("#99b9f5"))
        painter.drawEllipse(QPointF(25, 22), 3, 3)
        painter.setPen(QColor("#b7c8d8"))
        painter.setFont(QFont(self.settings.font_family, 9))
        title = self._status
        if not self.settings.overlay_locked:
            title += "  ·  Drag to move"
        painter.drawText(QRectF(36, 11, self.width() - 64, 23), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
        text_height = self._target_height + (self._source_height + 10 if self._visible_source_lines else 0)
        y = max(44.0, 44.0 + (self.height() - 64 - text_height) / 2)
        if self._source and self.settings.show_original and self._target:
            painter.setPen(QColor("#bdc8d5"))
            for line in self._visible_source_lines:
                line.draw(painter, QPointF(28, y))
                y += line.height()
            y += 10
        if self._target:
            painter.setPen(QColor(self.settings.text_color))
            for line in self._visible_target_lines:
                line.draw(painter, QPointF(28, y))
                y += line.height()
        else:
            painter.setFont(QFont(self.settings.font_family, 16))
            painter.setPen(QColor("#b7c8d8"))
            painter.drawText(QRectF(28, (self.height() - 24) / 2, self.width() - 108, 34), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, self._status + "…" if self._status != "Paused" else "Paused")

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and not self.settings.overlay_locked:
            self._drag_offset = event.globalPosition().toPoint() - self.pos()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = None
            self._emit_geometry()
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "grip"):
            self.grip.move(self.width() - 24, self.height() - 24)
        if hasattr(self, "_target"):
            self._reflow()
        self._position_playback()
        self._emit_geometry()

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self._position_playback()
        self._emit_geometry()

    def _emit_geometry(self) -> None:
        if not getattr(self, "_initializing", True):
            self.geometry_changed.emit(self.x(), self.y(), self.width(), self.height())

    def _position_playback(self) -> None:
        if hasattr(self, "playback_window"):
            self.playback_window.move(self.x() + self.width() - 62, self.y() + (self.height() - 44) // 2)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._position_playback()
        self.playback_window.show()
        self.playback_window.raise_()

    def hideEvent(self, event) -> None:
        if hasattr(self, "playback_window"):
            self.playback_window.hide()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:
        self.playback_window.close()
        super().closeEvent(event)

    def contextMenuEvent(self, event) -> None:
        if self.settings.overlay_locked:
            return
        menu = QMenu(self)
        lock_action = menu.addAction("Lock position and pass clicks through")
        center_action = menu.addAction("Reset position")
        hide_action = menu.addAction("Hide subtitles")
        chosen = menu.exec(event.globalPos())
        if chosen is lock_action:
            self.lock_requested.emit(True)
        elif chosen is center_action:
            self.reset_position()
        elif chosen is hide_action:
            self.hide()
            self.hidden_by_user.emit()

    def sizeHint(self) -> QSize:
        return QSize(self.settings.overlay_width, self.settings.overlay_height)
