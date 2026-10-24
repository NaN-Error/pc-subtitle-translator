# Contributing

Use GitHub Issues for reproducible bugs and feature requests. Include the app version, Windows version, output device, model IDs, expected behavior, actual behavior, and minimal steps to reproduce. Redact secrets and private content from logs and screenshots.

## Development

1. Install 64-bit Python 3.12–3.14 on Windows.
2. Run `setup_windows.bat` in the repository root.
3. Install test/build dependencies and run the checks:

```bat
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pip check
.venv\Scripts\python.exe -m pytest -q
```

For headless UI tests in PowerShell, set `$env:QT_QPA_PLATFORM = "offscreen"` before running pytest. Linux instructions and portable build commands are in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md). Windows capture remains a Windows-only feature.

The automated suite uses synthetic audio and mocked OpenAI transports. Tests must not need a real API key, access real credentials, record real audio, or incur API charges. Use `start_windows.bat --demo` for a sample subtitle preview. Treat live API/hardware testing as a separate manual step.

## Pull requests

Describe the user-visible problem, your change, and the checks you ran. Keep changes focused. Update usage documentation when behavior or configuration changes. Preserve bounded queues, cancellation, credential exclusion, official fixed API endpoints, and the distinction between simulated checks and live Windows acceptance. The application source is MIT-licensed; third-party libraries retain their own licenses.
