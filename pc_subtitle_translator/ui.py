"""Native desktop controls. Audio and API work live outside the GUI thread."""

from __future__ import annotations

import math
import sys
import time
from dataclasses import replace

from PySide6.QtCore import QPointF, QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox,
    QFontComboBox, QFormLayout, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QPlainTextEdit, QProgressBar, QProxyStyle,
    QPushButton, QScrollArea, QSizePolicy, QSpinBox, QStyle, QTabWidget,
    QVBoxLayout, QWidget,
)

from . import __version__
from .credentials import delete_api_key, get_api_key, save_api_key
from .languages import LANGUAGES, TARGET_LANGUAGES
from .models import Caption
from .overlay import SubtitleOverlay
from .settings import Settings, load_settings, save_settings


STYLE = """
QWidget { background: #0c1423; color: #e7edf5; font-family: 'Segoe UI'; font-size: 13px; }
QMainWindow, QScrollArea, QScrollArea > QWidget > QWidget { background: #0c1423; }
QFrame#card { background: #121e30; border: 1px solid #24354a; border-radius: 12px; }
QFrame#card QLabel, QFrame#card QCheckBox { background: transparent; }
QLabel#title { font-size: 26px; font-weight: 650; }
QLabel#subtitle { color: #91a6bc; font-size: 13px; }
QLabel#section { font-size: 15px; font-weight: 650; color: #e9f2fa; }
QLabel#muted { color: #91a6bc; }
QLabel#status { color: #6ce7d0; }
QLabel#error { color: #ffb6b6; background: #3b2030; border: 1px solid #753346; border-radius: 8px; padding: 10px; }
QPushButton { background: #1a2c43; border: 1px solid #31465f; border-radius: 7px; padding: 8px 14px; font-weight: 600; }
QPushButton:hover { background: #243b55; border-color: #4b6a89; }
QPushButton:pressed { background: #12273d; }
QPushButton:disabled { color: #62778e; background: #152136; border-color: #26354a; }
QPushButton#primary { background: #65e2cb; color: #082a29; border: none; font-size: 14px; padding: 11px 22px; }
QPushButton#primary:hover { background: #8cecd9; }
QPushButton#primary:disabled { background: #254c4b; color: #719693; }
QPushButton#stop { background: #302335; border-color: #795060; color: #ffc6cc; }
QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox, QPlainTextEdit { background: #0e1929; border: 1px solid #30445d; border-radius: 6px; padding: 7px; selection-background-color: #247d79; }
QComboBox:focus, QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus { border-color: #65d9c5; }
QComboBox:disabled, QLineEdit:disabled { color: #7b8da1; }
QComboBox QAbstractItemView { background: #15243a; selection-background-color: #274e64; color: #e7edf5; }
QCheckBox { spacing: 7px; }
QCheckBox::indicator { width: 17px; height: 17px; border-radius: 4px; border: 1px solid #4e657e; background: #0e1929; }
QCheckBox::indicator:checked { background: #65e2cb; border-color: #65e2cb; }
QTabWidget::pane { border: none; padding-top: 12px; }
QTabBar::tab { background: #0c1423; color: #91a6bc; border-bottom: 2px solid #26354a; padding: 10px 20px; }
QTabBar::tab:selected { color: #79e8d4; border-bottom: 2px solid #65e2cb; }
QProgressBar { background: #0e1929; border: 0; border-radius: 4px; height: 8px; }
QProgressBar::chunk { background: #65e2cb; border-radius: 4px; }
QScrollBar:vertical { width: 10px; background: #0c1423; }
QScrollBar::handle:vertical { background: #34465e; border-radius: 5px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QToolTip { color: #e7edf5; background: #263b54; border: 1px solid #52708e; padding: 5px; }
"""


class CaptionStyle(QProxyStyle):
    """Keep native control arrows visible against the dark application palette."""

    def __init__(self):
        super().__init__("Fusion")

    def drawPrimitive(self, element, option, painter, widget=None):
        arrows = {
            QStyle.PrimitiveElement.PE_IndicatorArrowDown: "down",
            QStyle.PrimitiveElement.PE_IndicatorArrowUp: "up",
            QStyle.PrimitiveElement.PE_IndicatorSpinDown: "down",
            QStyle.PrimitiveElement.PE_IndicatorSpinUp: "up",
        }
        if element not in arrows:
            return super().drawPrimitive(element, option, painter, widget)
        center = option.rect.center()
        x, y = float(center.x()), float(center.y())
        direction = 1 if arrows[element] == "down" else -1
        points = QPolygonF([
            QPointF(x - 4, y - direction * 2),
            QPointF(x + 4, y - direction * 2),
            QPointF(x, y + direction * 3),
        ])
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#a6c5da") if option.state & QStyle.StateFlag.State_Enabled else QColor("#52677d"))
        painter.drawPolygon(points)
        painter.restore()


def _label(text: str, kind: str = "", *, wrap: bool = False) -> QLabel:
    result = QLabel(text)
    result.setTextFormat(Qt.TextFormat.PlainText)
    if kind:
        result.setObjectName(kind)
    result.setWordWrap(wrap)
    return result


def _card(title: str, description: str = "") -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(18, 16, 18, 16)
    layout.setSpacing(12)
    layout.addWidget(_label(title, "section"))
    if description:
        layout.addWidget(_label(description, "muted", wrap=True))
    return frame, layout


def _scroll(content: QWidget) -> QScrollArea:
    scroll = QScrollArea()
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setWidget(content)
    return scroll


def _icon() -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#65e2cb"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(5, 8, 54, 44, 12, 12)
    painter.setPen(QColor("#092b30"))
    painter.setFont(QFont("Segoe UI", 21, QFont.Weight.Bold))
    painter.drawText(pixmap.rect().adjusted(0, -6, 0, 0), Qt.AlignmentFlag.AlignCenter, "CC")
    painter.end()
    return QIcon(pixmap)


class MainWindow(QMainWindow):
    def __init__(self, *, controller=None, settings: Settings | None = None, discover_devices: bool = True, demo: bool = False):
        super().__init__()
        load_error = ""
        if settings is None:
            try:
                settings = load_settings()
            except ValueError as exc:
                settings = Settings()
                load_error = str(exc)
        self.settings = replace(settings)
        if controller is None:
            from .controller import Controller
            controller = Controller()
        self.controller = controller
        self._devices = []
        self._started_at: float | None = None
        self._last_stats = {}
        self._building = True
        self._closing = False
        self._demo = demo
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("PC subtitle translator")
        self.setWindowIcon(_icon())
        self.resize(850, 920)
        self.setMinimumSize(690, 660)
        self.setStyleSheet(STYLE)
        self.overlay = SubtitleOverlay(self.settings)
        self.overlay.geometry_changed.connect(self._overlay_moved)
        self.overlay.lock_requested.connect(self._set_overlay_locked)
        self.overlay.playback_requested.connect(self._overlay_playback)
        self.overlay.hidden_by_user.connect(self._overlay_hidden)
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(26, 24, 26, 18)
        layout.setSpacing(12)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(5)
        titles.addWidget(_label("PC subtitle translator", "title"))
        titles.addWidget(_label("Live PC audio, translated into movable subtitles.", "subtitle"))
        header.addLayout(titles, 1)
        badge = _label(f"v{__version__}", "muted")
        header.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)
        self.error_banner = _label("", "error", wrap=True)
        self.error_banner.hide()
        layout.addWidget(self.error_banner)
        self.tabs = QTabWidget()
        self.tabs.addTab(_scroll(self._translation_tab()), "Translate")
        self.tabs.addTab(_scroll(self._appearance_tab()), "Appearance")
        self.tabs.addTab(_scroll(self._advanced_tab()), "Advanced")
        layout.addWidget(self.tabs, 1)
        footer = QHBoxLayout()
        self.status_label = _label("Ready · choose your audio output and language", "status")
        self.status_label.setWordWrap(True)
        footer.addWidget(self.status_label, 1)
        self.save_button = QPushButton("Save preferences")
        self.save_button.clicked.connect(self._save_preferences)
        footer.addWidget(self.save_button)
        layout.addLayout(footer)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._refresh_stats)
        self._timer.start()
        self._geometry_save_timer = QTimer(self)
        self._geometry_save_timer.setSingleShot(True)
        self._geometry_save_timer.setInterval(500)
        self._geometry_save_timer.timeout.connect(self._save_geometry)
        self._connect_controller()
        self._building = False
        if load_error:
            self._show_error(load_error + " Default preferences are loaded.")
        if discover_devices and not demo:
            QTimer.singleShot(0, self.refresh_devices)
        else:
            self.device_combo.addItem("Default speaker / headphone output", None)
        if demo:
            QTimer.singleShot(150, self.show_demo)

    def _translation_tab(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 5, 0)
        layout.setSpacing(14)
        card, body = _card("Audio and languages", "Capture sound playing through your speakers or headphones.")
        self.routing_card = card
        form = QFormLayout()
        form.setSpacing(11)
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(270)
        self.device_combo.setMinimumContentsLength(25)
        self.device_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.device_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh_devices)
        audio_row = QHBoxLayout()
        audio_row.addWidget(self.device_combo, 1)
        audio_row.addWidget(self.refresh_button)
        form.addRow("Audio output", audio_row)
        self.source_combo = QComboBox()
        self.source_combo.addItem("Detect automatically", "")
        for code, name in LANGUAGES:
            self.source_combo.addItem(name, code)
        self.source_combo.addItem("Other language code…", "custom")
        index = self.source_combo.findData(self.settings.source_language)
        self.source_combo.setCurrentIndex(index if index >= 0 else self.source_combo.findData("custom"))
        self.custom_source = QLineEdit(self.settings.source_language if index < 0 else "")
        self.custom_source.setPlaceholderText("e.g. yue or zh-cn")
        self.custom_source.setMaximumWidth(170)
        self.custom_source.setVisible(index < 0)
        source_row = QHBoxLayout()
        source_row.addWidget(self.source_combo, 1)
        source_row.addWidget(self.custom_source)
        self.source_combo.currentIndexChanged.connect(lambda: self.custom_source.setVisible(self.source_combo.currentData() == "custom"))
        form.addRow("Spoken language", source_row)
        self.target_combo = QComboBox()
        self.target_combo.setEditable(True)
        self.target_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.target_combo.addItems(TARGET_LANGUAGES)
        self.target_combo.setCurrentText(self.settings.target_language)
        self.target_combo.lineEdit().setMaxLength(80)
        self.target_combo.setToolTip("Choose a language or type one, including a regional preference.")
        form.addRow("Translate into", self.target_combo)
        body.addLayout(form)
        body.addWidget(_label("Automatic mode uses incoming audio. A source-language hint can help recognition; language support and accuracy vary by model.", "muted", wrap=True))
        layout.addWidget(card)
        card, body = _card("Your OpenAI connection")
        self.key_card = card
        key_row = QHBoxLayout()
        self.key_edit = QLineEdit(get_api_key())
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("Paste your OpenAI API key")
        self.key_edit.setMaxLength(1000)
        self.key_edit.setToolTip("Used directly with OpenAI. Never included in preferences or transcripts.")
        key_row.addWidget(self.key_edit, 1)
        show_key = QCheckBox("Show")
        show_key.toggled.connect(lambda checked: self.key_edit.setEchoMode(QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password))
        key_row.addWidget(show_key)
        body.addLayout(key_row)
        key_options = QHBoxLayout()
        self.remember_key = QCheckBox("Remember in Windows Credential Manager")
        self.remember_key.setChecked(self.settings.remember_key)
        key_options.addWidget(self.remember_key, 1)
        forget = QPushButton("Forget saved key")
        forget.clicked.connect(self._forget_key)
        key_options.addWidget(forget)
        body.addLayout(key_options)
        body.addWidget(_label("While running, audio is sent to OpenAI and billed to your API account. Audio and subtitles are not saved to disk.", "muted", wrap=True))
        layout.addWidget(card)
        actions = QHBoxLayout()
        self.start_button = QPushButton("Start translating")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self.start_translation)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setObjectName("stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.controller.stop)
        self.preview_button = QPushButton("Preview subtitles")
        self.preview_button.clicked.connect(self.preview_subtitles)
        actions.addWidget(self.start_button)
        actions.addWidget(self.stop_button)
        actions.addStretch()
        actions.addWidget(self.preview_button)
        layout.addLayout(actions)
        card, body = _card("Live preview")
        meter_row = QHBoxLayout()
        meter_row.addWidget(_label("Audio level", "muted"))
        self.audio_meter = QProgressBar()
        self.audio_meter.setRange(0, 100)
        self.audio_meter.setValue(0)
        self.audio_meter.setTextVisible(False)
        self.audio_meter.setFixedHeight(8)
        meter_row.addWidget(self.audio_meter, 1)
        body.addLayout(meter_row)
        self.source_preview = QPlainTextEdit()
        self.source_preview.setReadOnly(True)
        self.source_preview.setPlaceholderText("Recognized speech will appear here…")
        self.source_preview.setMaximumHeight(80)
        self.source_preview.setMinimumHeight(60)
        self.source_preview.document().setMaximumBlockCount(20)
        self.target_preview = QPlainTextEdit()
        self.target_preview.setReadOnly(True)
        self.target_preview.setPlaceholderText("Your translated subtitles will appear here…")
        self.target_preview.setMaximumHeight(80)
        self.target_preview.setMinimumHeight(60)
        self.target_preview.setStyleSheet("font-size: 16px; color: #b7f4e9;")
        self.target_preview.document().setMaximumBlockCount(20)
        preview_row = QHBoxLayout()
        original_column = QVBoxLayout()
        original_column.addWidget(_label("Original speech", "muted"))
        original_column.addWidget(self.source_preview)
        translation_column = QVBoxLayout()
        translation_column.addWidget(_label("Translation", "muted"))
        translation_column.addWidget(self.target_preview)
        preview_row.addLayout(original_column, 1)
        preview_row.addLayout(translation_column, 1)
        body.addLayout(preview_row)
        self.stats_label = _label("Session 00:00   ·   Audio sent 00:00   ·   Subtitles 0", "muted", wrap=True)
        body.addWidget(self.stats_label)
        layout.addWidget(card)
        layout.addStretch()
        return content

    def _appearance_tab(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 5, 0)
        layout.setSpacing(14)
        card, body = _card("Subtitle appearance", "These settings apply immediately, including while translation is running.")
        form = QFormLayout()
        form.setSpacing(14)
        self.font_combo = QFontComboBox()
        self.font_combo.setCurrentFont(QFont(self.settings.font_family))
        form.addRow("Font", self.font_combo)
        self.font_size = QSpinBox()
        self.font_size.setRange(14, 72)
        self.font_size.setValue(self.settings.font_size)
        self.font_size.setSuffix(" pt")
        form.addRow("Text size", self.font_size)
        self.color_button = QPushButton(self.settings.text_color)
        self._text_color = self.settings.text_color
        self.color_button.clicked.connect(self._choose_color)
        form.addRow("Text color", self.color_button)
        self.opacity = QSpinBox()
        self.opacity.setRange(0, 100)
        self.opacity.setSuffix(" %")
        self.opacity.setValue(self.settings.background_opacity)
        form.addRow("Background opacity", self.opacity)
        self.overlay_width = QSpinBox()
        self.overlay_width.setRange(360, 2400)
        self.overlay_width.setSingleStep(20)
        self.overlay_width.setSuffix(" px")
        self.overlay_width.setValue(self.settings.overlay_width)
        form.addRow("Subtitle width", self.overlay_width)
        self.overlay_height = QSpinBox()
        self.overlay_height.setRange(110, 700)
        self.overlay_height.setSingleStep(20)
        self.overlay_height.setSuffix(" px")
        self.overlay_height.setValue(self.settings.overlay_height)
        form.addRow("Subtitle height", self.overlay_height)
        self.hold_seconds = QDoubleSpinBox()
        self.hold_seconds.setRange(2, 30)
        self.hold_seconds.setSingleStep(0.5)
        self.hold_seconds.setDecimals(1)
        self.hold_seconds.setSuffix(" s")
        self.hold_seconds.setValue(self.settings.hold_seconds)
        form.addRow("Hide text after", self.hold_seconds)
        body.addLayout(form)
        self.show_original = QCheckBox("Show original speech above the translation")
        self.show_original.setChecked(self.settings.show_original)
        body.addWidget(self.show_original)
        body.addWidget(_label("The subtitle text wraps to the selected area. Increase its width or height to show more text. The complete current caption remains in Live preview.", "muted", wrap=True))
        layout.addWidget(card)
        card, body = _card("Position and visibility")
        self.lock_overlay = QCheckBox("Lock subtitles and pass mouse clicks through")
        self.lock_overlay.setChecked(self.settings.overlay_locked)
        body.addWidget(self.lock_overlay)
        body.addWidget(_label("Uncheck this here to move subtitles again. When unlocked, drag the subtitle window or resize it from the lower-right corner. The play/pause button on the right stays clickable even when locked.", "muted", wrap=True))
        row = QHBoxLayout()
        self.show_overlay_button = QPushButton("Show subtitles")
        self.show_overlay_button.clicked.connect(self._toggle_overlay)
        reset = QPushButton("Reset position")
        reset.clicked.connect(self.overlay.reset_position)
        preview = QPushButton("Preview appearance")
        preview.clicked.connect(self.preview_subtitles)
        row.addWidget(self.show_overlay_button)
        row.addWidget(reset)
        row.addWidget(preview)
        body.addLayout(row)
        body.addWidget(_label("For games, use a windowed or borderless mode so a normal desktop overlay can appear above the game.", "muted", wrap=True))
        layout.addWidget(card)
        layout.addStretch()
        self.font_combo.currentFontChanged.connect(self._apply_appearance)
        for spin in (self.font_size, self.opacity, self.overlay_width, self.overlay_height, self.hold_seconds):
            spin.valueChanged.connect(self._apply_appearance)
        self.show_original.toggled.connect(self._apply_appearance)
        self.lock_overlay.toggled.connect(self._apply_appearance)
        return content

    def _advanced_tab(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 5, 0)
        layout.setSpacing(14)
        card, body = _card("OpenAI models", "Choose speech recognition and translation models. Changes take effect the next time you start.")
        form = QFormLayout()
        form.setSpacing(14)
        self.transcription_model = QComboBox()
        self.transcription_model.setEditable(True)
        self.transcription_model.addItems(["gpt-live-transcribe", "gpt-transcribe", "gpt-4o-mini-transcribe", "gpt-4o-transcribe"])
        self.transcription_model.setCurrentText(self.settings.transcription_model)
        form.addRow("Speech recognition", self.transcription_model)
        self.translation_model = QComboBox()
        self.translation_model.setEditable(True)
        self.translation_model.addItems(["gpt-6-luna", "gpt-5.4-mini"])
        self.translation_model.setCurrentText(self.settings.translation_model)
        form.addRow("Text translation", self.translation_model)
        self.reasoning = QComboBox()
        self.reasoning.addItems(["none", "minimal", "low", "medium", "high", "omit"])
        self.reasoning.setCurrentText(self.settings.reasoning_effort)
        self.reasoning.setToolTip("None reduces delay with supported models. Choose omit when your selected model does not support this parameter.")
        form.addRow("Reasoning effort", self.reasoning)
        self.transcription_delay = QComboBox()
        self.transcription_delay.addItems(["minimal", "low", "medium", "high", "xhigh"])
        self.transcription_delay.setCurrentText(self.settings.transcription_delay)
        self.transcription_delay.setToolTip("Applies to models supporting streaming transcription delay.")
        form.addRow("Transcription delay", self.transcription_delay)
        body.addLayout(form)
        body.addWidget(_label("Model access depends on your API account. Keep the defaults unless you have a reason to change them. A custom model must support the selected API and parameters.", "muted", wrap=True))
        layout.addWidget(card)
        card, body = _card("Timing and recognition", "Translation begins after a short pause or the maximum segment length, then appears as text streams back.")
        form = QFormLayout()
        form.setSpacing(14)
        self.pause_ms = QSpinBox()
        self.pause_ms.setRange(250, 1500)
        self.pause_ms.setSingleStep(50)
        self.pause_ms.setSuffix(" ms")
        self.pause_ms.setValue(self.settings.pause_ms)
        form.addRow("Pause before translating", self.pause_ms)
        self.segment_seconds = QDoubleSpinBox()
        self.segment_seconds.setRange(1, 10)
        self.segment_seconds.setDecimals(1)
        self.segment_seconds.setSingleStep(0.5)
        self.segment_seconds.setSuffix(" s")
        self.segment_seconds.setValue(self.settings.max_segment_seconds)
        form.addRow("Maximum segment", self.segment_seconds)
        self.threshold_db = QDoubleSpinBox()
        self.threshold_db.setRange(-80, -15)
        self.threshold_db.setDecimals(0)
        self.threshold_db.setSingleStep(5)
        self.threshold_db.setSuffix(" dBFS")
        self.threshold_db.setValue(self.settings.silence_threshold_db)
        self.threshold_db.setToolTip("More negative values detect quieter speech. This is an energy gate, so music can also open it.")
        form.addRow("Quiet-audio threshold", self.threshold_db)
        body.addLayout(form)
        body.addWidget(_label("Shorter segments can reduce delay but provide less context and make more translation requests. Longer segments can improve phrasing. Actual delay depends on speech, your connection, and the models.", "muted", wrap=True))
        body.addWidget(_label("Vocabulary or subject hints", "muted"))
        self.context_hint = QPlainTextEdit(self.settings.context_hint)
        self.context_hint.setMaximumHeight(100)
        self.context_hint.setPlaceholderText("Optional: a robotics lecture; proper names or technical vocabulary…")
        body.addWidget(self.context_hint)
        body.addWidget(_label("Use a short description of the audio, up to 1,000 characters.", "muted"))
        layout.addWidget(card)
        layout.addStretch()
        return content

    def _connect_controller(self) -> None:
        self.controller.caption.connect(self._receive_caption)
        self.controller.source_preview.connect(self.source_preview.setPlainText)
        self.controller.status.connect(self.status_label.setText)
        self.controller.error.connect(self._show_error)
        self.controller.level.connect(self._set_level)
        self.controller.running_changed.connect(self._running_changed)
        self.controller.stats.connect(self._receive_stats)

    def refresh_devices(self) -> None:
        if self.controller.is_running:
            return
        current_name = self.device_combo.currentData(Qt.ItemDataRole.UserRole + 1) or self.settings.device_name
        try:
            from .audio import list_loopback_devices
            devices = list_loopback_devices()
        except Exception as exc:
            self.device_combo.clear()
            self.device_combo.addItem("No Windows audio output available", None)
            self._devices = []
            self._show_error(str(exc))
            return
        self._devices = devices
        self.device_combo.clear()
        self.device_combo.addItem("Follow the Windows default output at Start", None)
        for device in devices:
            suffix = " (default)" if device.is_default else ""
            self.device_combo.addItem(device.name + suffix, device.id)
            self.device_combo.setItemData(self.device_combo.count() - 1, device.name, Qt.ItemDataRole.UserRole + 1)
        if current_name:
            index = next((i for i in range(self.device_combo.count()) if self.device_combo.itemData(i, Qt.ItemDataRole.UserRole + 1) == current_name), -1)
            if index >= 0:
                self.device_combo.setCurrentIndex(index)
            else:
                self.status_label.setText("Saved output is unavailable; the Windows default will be used.")
        if not devices:
            self._show_error("No loopback audio outputs were found. Connect speakers or headphones and press Refresh.")
        else:
            self.error_banner.hide()

    def _collect_settings(self) -> Settings:
        source = self.source_combo.currentData()
        if source == "custom":
            source = self.custom_source.text().strip()
            if not source:
                raise ValueError("Enter a source language code, or choose Detect automatically.")
        return replace(
            self.settings,
            device_name=self.device_combo.currentData(Qt.ItemDataRole.UserRole + 1) or "",
            source_language=source or "",
            target_language=self.target_combo.currentText(),
            transcription_model=self.transcription_model.currentText(),
            translation_model=self.translation_model.currentText(),
            reasoning_effort=self.reasoning.currentText(),
            transcription_delay=self.transcription_delay.currentText(),
            context_hint=self.context_hint.toPlainText(),
            pause_ms=self.pause_ms.value(),
            max_segment_seconds=self.segment_seconds.value(),
            silence_threshold_db=self.threshold_db.value(),
            remember_key=self.remember_key.isChecked(),
            **self._appearance_values(),
        ).validated()

    def _appearance_values(self) -> dict:
        return {
            "font_family": self.font_combo.currentFont().family(), "font_size": self.font_size.value(),
            "text_color": self._text_color, "background_opacity": self.opacity.value(),
            "overlay_width": self.overlay_width.value(), "overlay_height": self.overlay_height.value(), "hold_seconds": self.hold_seconds.value(),
            "show_original": self.show_original.isChecked(), "overlay_locked": self.lock_overlay.isChecked(),
        }

    def _store_preferences(self, settings: Settings) -> None:
        if settings.remember_key:
            save_api_key(self.key_edit.text())
        elif self.settings.remember_key:
            delete_api_key()
        save_settings(settings)
        self.settings = settings

    def _save_preferences(self, checked=False, *, silent: bool = False) -> bool:
        try:
            self._store_preferences(self._collect_settings())
            self.overlay.apply_settings(self.settings)
            if not silent:
                self.error_banner.hide()
                self.status_label.setText("Preferences saved. Model and language changes apply at the next Start.")
            return True
        except Exception as exc:
            self._show_error(str(exc))
            return False

    def _forget_key(self) -> None:
        try:
            delete_api_key()
            self.key_edit.clear()
            self.remember_key.setChecked(False)
            self.settings.remember_key = False
            save_settings(self.settings)
            self.status_label.setText("Saved API key removed.")
            self.error_banner.hide()
        except Exception as exc:
            self._show_error(str(exc))

    def start_translation(self) -> None:
        if self.controller.is_running:
            return
        key = self.key_edit.text().strip()
        if not key:
            self._show_error("Paste your OpenAI API key to start. Preview subtitles works without a key.")
            self.key_edit.setFocus()
            return
        try:
            settings = self._collect_settings()
            self._store_preferences(settings)
            self.error_banner.hide()
            self.source_preview.clear()
            self.target_preview.clear()
            self._last_stats = {}
            self.overlay.apply_settings(settings)
            self.overlay.begin_session()
            self._show_overlay()
            self.controller.start(replace(settings), key, self.device_combo.currentData())
        except Exception as exc:
            self.overlay.end_session()
            self._show_error(str(exc))

    def _running_changed(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.preview_button.setEnabled(not running)
        self.routing_card.setEnabled(not running)
        self.key_card.setEnabled(not running)
        if running:
            self._started_at = time.monotonic()
        else:
            self._started_at = None
            self.audio_meter.setValue(0)
            self.overlay.end_session()
        self._refresh_stats()

    def _receive_caption(self, caption: Caption) -> None:
        self.target_preview.setPlainText(caption.target)
        # The control panel's source box follows live recognition independently.
        self.overlay.show_caption(caption)

    def _set_level(self, level: float) -> None:
        if not math.isfinite(level):
            level = 0.0
        # Log display makes ordinary speech levels useful without changing audio.
        db = 20 * math.log10(max(level, 1e-5))
        self.audio_meter.setValue(max(0, min(100, round((db + 60) / 60 * 100))))

    def _receive_stats(self, stats: dict) -> None:
        self._last_stats = stats
        self._refresh_stats()

    @staticmethod
    def _clock(seconds: float) -> str:
        value = max(0, int(seconds))
        if value >= 3600:
            return f"{value // 3600}:{(value // 60) % 60:02d}:{value % 60:02d}"
        return f"{value // 60:02d}:{value % 60:02d}"

    def _refresh_stats(self) -> None:
        elapsed = time.monotonic() - self._started_at if self._started_at is not None else 0
        audio = float(self._last_stats.get("audio_seconds", 0))
        segments = int(self._last_stats.get("translated_segments", 0))
        text = f"Session {self._clock(elapsed)}   ·   Audio sent {self._clock(audio)}   ·   Subtitles {segments}"
        dropped = int(self._last_stats.get("dropped_segments", 0))
        if dropped:
            text += f"   ·   Skipped {dropped}"
        latency = self._last_stats.get("translation_ms")
        if latency is not None:
            text += f"   ·   Last translation {float(latency) / 1000:.1f}s"
        self.stats_label.setText(text)

    def _apply_appearance(self, *args) -> None:
        if self._building:
            return
        self.settings = replace(self.settings, **self._appearance_values())
        self.overlay.apply_settings(self.settings)

    def _choose_color(self) -> None:
        chosen = QColorDialog.getColor(QColor(self._text_color), self, "Choose subtitle text color")
        if chosen.isValid():
            self._text_color = chosen.name()
            self.color_button.setText(chosen.name())
            self._apply_appearance()

    def _set_overlay_locked(self, locked: bool) -> None:
        self.lock_overlay.setChecked(locked)

    def _overlay_moved(self, x: int, y: int, width: int, height: int) -> None:
        self.settings.overlay_x, self.settings.overlay_y = x, y
        self.settings.overlay_width = width
        self.settings.overlay_height = height
        if hasattr(self, "overlay_width"):
            with QSignalBlocker(self.overlay_width):
                self.overlay_width.setValue(width)
        if hasattr(self, "overlay_height"):
            with QSignalBlocker(self.overlay_height):
                self.overlay_height.setValue(height)
        if hasattr(self, "_geometry_save_timer") and not self._building:
            self._geometry_save_timer.start()

    def _save_geometry(self) -> None:
        # Geometry is non-secret and saved independently of incomplete edits.
        try:
            save_settings(self.settings)
        except (OSError, ValueError) as exc:
            self._show_error(str(exc))

    def _show_overlay(self) -> None:
        self.overlay.show()
        self.overlay.raise_()
        self.overlay.playback_window.raise_()
        self.show_overlay_button.setText("Hide subtitles")

    def _overlay_hidden(self) -> None:
        self.show_overlay_button.setText("Show subtitles")

    def _overlay_playback(self, should_run: bool) -> None:
        if should_run:
            if not self.key_edit.text().strip():
                self.showNormal()
                self.raise_()
                self.activateWindow()
                self.tabs.setCurrentIndex(0)
            self.start_translation()
        else:
            self.controller.stop()

    def _toggle_overlay(self) -> None:
        if self.overlay.isVisible():
            self.overlay.hide()
            self.show_overlay_button.setText("Show subtitles")
        else:
            self._show_overlay()

    def preview_subtitles(self) -> None:
        if self.controller.is_running:
            self._show_overlay()
            return
        self._apply_appearance()
        target = self.target_combo.currentText().lower()
        examples = {
            "spanish": ("This is how your subtitles will appear.", "Así se verán tus subtítulos."),
            "french": ("This is how your subtitles will appear.", "Voici à quoi ressembleront vos sous-titres."),
            "german": ("This is how your subtitles will appear.", "So werden Ihre Untertitel angezeigt."),
            "japanese": ("This is how your subtitles will appear.", "字幕はこのように表示されます。"),
            "korean": ("This is how your subtitles will appear.", "자막은 이렇게 표시됩니다."),
            "arabic": ("This is how your subtitles will appear.", "هكذا ستظهر الترجمة على الشاشة."),
            "chinese": ("This is how your subtitles will appear.", "您的字幕将以这种方式显示。"),
            "portuguese": ("This is how your subtitles will appear.", "É assim que suas legendas aparecerão."),
        }
        source, translated = next((pair for name, pair in examples.items() if target.startswith(name)), ("Así se verán tus subtítulos.", "This is how your subtitles will appear."))
        caption = Caption(0, source, translated, True)
        self.overlay.show_caption(caption, preview=True)
        self._show_overlay()
        self.source_preview.setPlainText(source)
        self.target_preview.setPlainText(translated)
        self.status_label.setText("Appearance preview · sample text, no audio capture or API request")

    def show_demo(self) -> None:
        self.target_combo.setCurrentText("Spanish (Puerto Rico)")
        self.show_original.setChecked(True)
        self.preview_subtitles()

    def _show_error(self, message: str) -> None:
        # Avoid accidental rich-text interpretation and key display in exceptions.
        key = self.key_edit.text().strip() if hasattr(self, "key_edit") else ""
        safe = str(message).replace(key, "[API key hidden]") if key else str(message)
        self.error_banner.setText(safe[:1800])
        self.error_banner.show()

    def closeEvent(self, event) -> None:
        if self._closing:
            event.accept()
            return
        self._closing = True
        self._geometry_save_timer.stop()
        self.controller.stop()
        stopped = self.controller.shutdown(timeout=5)
        if not stopped:
            self._closing = False
            self._show_error("Translation is still shutting down. Wait a moment, then close the window again.")
            event.ignore()
            return
        self._save_preferences(silent=True)
        self.overlay.close()
        event.accept()


def run_app(*, demo: bool = False) -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("PC subtitle translator")
    app.setOrganizationName("PcSubtitleTranslator")
    app.setStyle(CaptionStyle())
    app.setWindowIcon(_icon())
    window = MainWindow(demo=demo)
    window.show()
    return app.exec()
