"""Validated, non-secret user preferences. API keys never enter this file."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path


@dataclass
class Settings:
    device_name: str = ""
    source_language: str = ""
    target_language: str = "English"
    transcription_model: str = "gpt-live-transcribe"
    translation_model: str = "gpt-6-luna"
    reasoning_effort: str = "none"
    transcription_delay: str = "low"
    context_hint: str = ""
    pause_ms: int = 450
    max_segment_seconds: float = 3.0
    silence_threshold_db: float = -50.0
    show_original: bool = False
    font_family: str = "Segoe UI"
    font_size: int = 28
    text_color: str = "#ffffff"
    background_opacity: int = 78
    overlay_width: int = 980
    overlay_height: int = 220
    hold_seconds: float = 8.0
    overlay_x: int | None = None
    overlay_y: int | None = None
    overlay_locked: bool = False
    remember_key: bool = False

    def validated(self) -> "Settings":
        """Reject malformed settings instead of silently creating API surprises."""
        defaults = Settings()
        for field in fields(self):
            value = getattr(self, field.name)
            default = getattr(defaults, field.name)
            if field.name in {"overlay_x", "overlay_y"}:
                if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                    raise ValueError("Overlay coordinates must be integers.")
            elif isinstance(default, bool):
                if not isinstance(value, bool):
                    raise ValueError(f"Invalid setting: {field.name}")
            elif isinstance(default, str):
                if not isinstance(value, str):
                    raise ValueError(f"Invalid setting: {field.name}")
            elif isinstance(default, int):
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(f"Invalid setting: {field.name}")
            elif isinstance(default, float):
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f"Invalid setting: {field.name}")
        self.source_language = self.source_language.strip().lower()
        self.target_language = self.target_language.strip()
        if self.source_language and not re.fullmatch(r"[a-z]{2,3}(?:-[a-z]{2,4})?", self.source_language):
            raise ValueError("Source language must be a language code such as en, es, ja, or zh-cn.")
        if not self.target_language or len(self.target_language) > 80 or any(ord(c) < 32 for c in self.target_language):
            raise ValueError("Enter a target language, such as English or Spanish (Puerto Rico).")
        for name in ("transcription_model", "translation_model"):
            value = getattr(self, name).strip()
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", value):
                raise ValueError("Enter a valid OpenAI model ID.")
            setattr(self, name, value)
        if self.reasoning_effort not in {"omit", "none", "minimal", "low", "medium", "high"}:
            raise ValueError("Invalid reasoning setting.")
        if self.transcription_delay not in {"minimal", "low", "medium", "high", "xhigh"}:
            raise ValueError("Invalid transcription delay.")
        if len(self.context_hint) > 1000:
            raise ValueError("Vocabulary context must be 1,000 characters or shorter.")
        limits = {
            "pause_ms": (250, 1500), "max_segment_seconds": (1.0, 10.0),
            "silence_threshold_db": (-80.0, -15.0), "font_size": (14, 72),
            "background_opacity": (0, 100), "overlay_width": (360, 2400),
            "overlay_height": (110, 700),
            "hold_seconds": (2.0, 30.0),
        }
        for name, (lo, hi) in limits.items():
            if not lo <= getattr(self, name) <= hi:
                raise ValueError(f"{name.replace('_', ' ')} must be between {lo} and {hi}.")
        if not re.fullmatch(r"#[a-fA-F0-9]{6}", self.text_color):
            raise ValueError("Subtitle text color must be a six-digit hex color.")
        if len(self.font_family) > 100 or len(self.device_name) > 500:
            raise ValueError("Invalid font or audio device name.")
        return self


def settings_path() -> Path:
    root = Path(os.environ.get("APPDATA", Path.home() / ".config"))
    return root / "PcSubtitleTranslator" / "settings.json"


def load_settings(path: Path | None = None) -> Settings:
    path = path or settings_path()
    if not path.exists():
        return Settings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        allowed = {field.name for field in fields(Settings)}
        return Settings(**{k: v for k, v in raw.items() if k in allowed}).validated()
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Saved preferences could not be read. Reset settings or fix settings.json.") from exc


def save_settings(settings: Settings, path: Path | None = None) -> None:
    path = path or settings_path()
    settings.validated()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_path = tempfile.mkstemp(prefix="settings-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(asdict(settings), handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
