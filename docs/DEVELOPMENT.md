# Development and packaging

## Source layout

| File | Responsibility |
| --- | --- |
| `pc_subtitle_translator/ui.py` | Main settings/control window. |
| `pc_subtitle_translator/overlay.py` | Movable subtitle area and interactive play/pause companion. |
| `pc_subtitle_translator/controller.py` | Qt signals and background worker lifecycle. |
| `pc_subtitle_translator/audio.py` | WASAPI capture and stateful PCM resampling. |
| `pc_subtitle_translator/segmentation.py` | Local energy gate, pre-roll, quiet/max-duration commits. |
| `pc_subtitle_translator/engine.py` | WebSocket session, queues, ordering, translation worker. |
| `pc_subtitle_translator/protocol.py` | API schemas, SSE parsing, errors, ordered transcripts. |
| `pc_subtitle_translator/translation.py` | Streaming Responses HTTP request. |
| `pc_subtitle_translator/settings.py` | Preference validation and atomic JSON writes. |
| `pc_subtitle_translator/credentials.py` | Windows Credential Manager access. |
| `tests/` | Pure, mocked, and offscreen integration tests. |
| `scripts/build_portable.py` | Verified runtime/wheel download and portable assembly. |

## Test environment

On Windows, start with `setup_windows.bat`, then:

```bat
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest -q
```

For a Linux build/check environment with Python 3.12 or later:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
```

Linux can test the signal conversion, protocol, fake transports, preference storage, and GUI. It cannot establish WASAPI capture, Windows click-through/z-order behavior, Credential Manager, or binary compatibility on a user's machine. Windows-specific imports are guarded so these boundaries remain explicit.

## Portable bundle

Run from the source root:

```bash
python scripts/build_portable.py --download-only
python scripts/build_portable.py --offline
```

The default cache and output directories are siblings of the source root, not inside it. Use `--help` for available options. The builder verifies the official CPython Windows x64 embeddable ZIP against a pinned published SHA-256, verifies every wheel against its exact PyPI metadata, safely extracts members, maps wheel `.data` directories, and keeps original license/metadata files. It includes explicit `python314._pth` entries for bundled packages and app source, plus a file manifest.

Version 0.1.1 outputs `Subtitles.zip`, with a short `Sub/` root, embedded Python in `Sub/r/`, and packages in `Sub/r/p/`. Its `python314._pth` uses `p` and `../app`, preserving the packages' sibling DLL directories. Exactly four unused static-build `.cpp.obj` files under the Qt QML assetdownloader directory are omitted and recorded in the manifest. Runtime DLLs, plugins, package code, assets, and license files are retained. All final file and directory names are checked for Windows validity, case-insensitive collisions, and a 128-unit UTF-16 relative-path budget. A 120-character extraction destination still leaves full paths below 260 characters. The current longest archive path is 126 characters.

Runtime: CPython 3.14.9 x64. The bundle launches the included interpreter through `Start.bat`; it is not a cross-compiled PyInstaller EXE. The optional `build_windows.bat` creates an onedir PyInstaller build when run on Windows. Native acceptance remains a separate step.

The source setup supports 64-bit Python 3.12–3.14. Runtime package pins live in `requirements.txt`; Windows-only package markers let the shared tests run on Linux. PySide6 Essentials is sufficient: QtCore, QtGui, QtWidgets, and QtTest are used; the full Addons distribution is unnecessary.

## Maintaining model support

Before changing API schemas, check the actual official guide. In particular, do not use the conversation-session URL with a model query when creating a transcription session, do not send singular `language` for the new live model, and do not use server VAD with `gpt-live-transcribe`. The app handles client commits itself.

Model dropdowns contain compatible examples, not a live list of the user's entitlements. Model IDs remain editable. A future account-model browser should still filter out nontranscription and nontext models rather than assuming that every `/v1/models` result supports this workflow.

## Release checks still required

Run `docs/TESTING.md` on a representative Windows PC using an authorized API project before treating a release as Windows-verified. Retain actual results instead of changing simulated checks into claims of live coverage. Measure end-to-end translation delay on representative languages and audio; do not infer it from the HTTP request duration alone.
