"""Optional Windows Credential Manager storage, with no plaintext fallback."""

from __future__ import annotations

import os
import sys

SERVICE = "PcSubtitleTranslator.OpenAI"
ACCOUNT = "api-key"


def _backend():
    if sys.platform != "win32":
        raise RuntimeError("Saving a key is supported through Windows Credential Manager on Windows only.")
    try:
        # Select the Windows vault explicitly. Never select a third-party plaintext backend.
        from keyring.backends.Windows import WinVaultKeyring
        return WinVaultKeyring()
    except Exception as exc:
        raise RuntimeError("Windows Credential Manager is unavailable. Leave Remember key off.") from exc


def get_api_key() -> str:
    value = os.environ.get("OPENAI_API_KEY", "").strip()
    if value:
        return value
    if sys.platform != "win32":
        return ""
    try:
        return _backend().get_password(SERVICE, ACCOUNT) or ""
    except Exception:
        return ""


def save_api_key(value: str) -> None:
    value = value.strip()
    if not value:
        raise ValueError("Enter your API key first.")
    try:
        _backend().set_password(SERVICE, ACCOUNT, value)
    except Exception as exc:
        raise RuntimeError("The API key could not be saved in Windows Credential Manager.") from exc


def delete_api_key() -> None:
    if sys.platform != "win32":
        return
    try:
        vault = _backend()
        if vault.get_password(SERVICE, ACCOUNT):
            vault.delete_password(SERVICE, ACCOUNT)
    except Exception as exc:
        raise RuntimeError("The saved API key could not be removed from Windows Credential Manager.") from exc
