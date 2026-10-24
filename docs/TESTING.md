# Verification and Windows acceptance

## Packaging repair in 0.1.1

The Windows extraction error reported for 0.1.0 was traced to a 198-character internal path combined with the user's extraction destination. Version 0.1.1 shortens the archive name and directory layout, omits only four unused Qt static-build object files, and adds a build-time Windows path audit. Focused packaging regressions check path lengths in UTF-16 units, case-insensitive collisions, invalid Windows names, exact omissions, launcher targets, and the embedded interpreter's import paths. This repair does not alter audio capture, translation, credential storage, or subtitle controls.

The original 96-test application baseline remains recorded separately from the packaging repair checks in `verification.json`. Archive integrity, path budgets, and preserved runtime payloads are checked on the rebuilt ZIP. Native Windows Explorer extraction still needs an actual Windows check; it is not implied by static validation.

## Publication checks on Windows — 2026-10-24

The supplied Windows x64 embedded interpreter (CPython 3.14.9) ran the current source suite on Windows 11 Home, build 26200, with Qt set to `offscreen`: **103 passed, 6 skipped**, exit code 0. The six skips are the case-insensitive collision fixture and five invalid-filename fixtures that require a non-Windows filesystem. Pytest 9.1.1 and pytest-asyncio 1.4.0 were loaded from a separate temporary test-tools directory; the release includes neither of them.

The bundled interpreter imported PySide6 6.12.0, NumPy 2.5.4, and soxr 1.1.0 successfully. The CLI help command succeeded. `scripts/render_preview.py` rendered four sample Qt views offscreen using a fake controller and disabled credential/settings access. Segoe UI fonts from Windows were explicitly registered for the offscreen renderer; the fonts are not redistributed. These updated screenshots are included in the repository.

No real audio was captured, no stored credential was read for these checks, and no paid API request was made. Actual desktop clicks, z-order/click-through, WASAPI hardware, Windows Credential Manager, and end-to-end live API operation still require the manual checks below. The original Linux build record is historical evidence, not a claim that this Windows test run happened on Linux. See [publication-verification.json](publication-verification.json).

## What was verified in the original build environment

The automated suite uses synthetic audio, fake capture drivers, simulated WebSocket/HTTP responses, temporary preference files, and offscreen Qt widgets. It does not need an API key and does not make paid API requests. The final run count, process exit status, Python version, and tested dependency versions are recorded in `verification.json`.

Coverage includes:

- PCM byte order, full-scale clipping, stereo downmix, irregular block boundaries, resampled duration, and anti-alias filtering.
- Loopback endpoint selection, rejection of microphone endpoints, capture shutdown, and driver discontinuities using a fake driver.
- Audio pre-roll, continuous speech duration bounds, idle capture without frames, and preservation of subframe tail samples.
- Exact session and translation request construction, source-language hints, omission in automatic mode, and bounded context.
- Out-of-order transcript completion, duplicate commit acknowledgments, retired events, timeouts, and bounded queues.
- Capture starting only after the server accepts session configuration.
- Streamed translation text, SSE parsing, completed-response fallback, errors, refusals, incomplete output, and transport truncation.
- Cancellation of in-flight work, actual controller repeated-stop behavior, and clearing expired provisional subtitles.
- Atomic preference writes, malformed settings rejection, credential exclusion from JSON, and API-key redaction.
- Model/language settings passed into the controller, appearance isolation from active settings, no-network preview, Unicode layout, overlay positioning, resize persistence, and play/pause dispatch.

Rendered sample views in `screenshots/` are actual Qt widget previews. They use sample text and do not demonstrate a live API translation. The overlay preview combines the real subtitle surface and its real companion button because the button is a separate native window.

## What remains unverified

The original bundle was assembled on Linux. Publication checks added Windows runtime imports and offscreen UI coverage, without exercising real hardware, stored credentials, or a user API key. Therefore these are **not** established by the automated results:

- A normal interactive desktop session launched through `Start.bat` on a particular Windows machine.
- Real WASAPI capture on built-in, USB, HDMI, or Bluetooth output devices.
- Native topmost behavior, actual mouse click-through, and interactive companion-button hit testing on Windows.
- Live model access, accepted requests, translation quality, actual latency, or real account billing.
- Exclusive full-screen games, protected audio, unusual multichannel layouts, and elevated windows.

The portable archive was assembled from hash-verified official runtime/package files. Assembly and static dependency checks do not replace Windows execution.

## Short Windows acceptance check

1. Extract the entire portable archive and launch `Start.bat`. Confirm that the main window opens. If it does not, use `Debug.bat` and preserve the exact startup error.
2. Press **Preview subtitles** before entering a key. Move and resize the overlay. Check the font picker, font size, width, height, opacity, and original-text toggle.
3. Lock the overlay. Click through its text area into another application. Confirm that its far-right play button still accepts a real mouse click. Unlock it from Appearance and move it again.
4. Enter an API key from your authorized project and select an output playing a short, clear spoken video. Confirm that the audio meter moves and source text appears, followed by a translation.
5. Try automatic source language, then an explicit source hint. Check at least English → Spanish and Spanish → English, plus any language important to your use.
6. Pause using the button at the far right of the subtitles. Confirm the control changes to play, the audio level stops, and **Audio sent** stops increasing. Resume and confirm that only newly played audio is processed.
7. Change the target and models while stopped, then restart. Confirm settings take effect and unavailable-model/key/rate errors are visible rather than silent.
8. Test a pause in playback and a long stretch of uninterrupted speech. Confirm that the first and last words are retained reasonably, subtitles keep advancing, and no large backlog accumulates.
9. Try headphones and the Windows default output. Switch an output while paused and refresh; verify that the intended device is captured.
10. If you use key persistence, check Remember, restart, and verify retrieval. Use Forget saved key and restart to verify removal. An independently configured `OPENAI_API_KEY` environment variable must be removed separately.
11. Briefly interrupt the connection during translation. Check the reconnect status and that paused/old captions are not replayed. Close the main window during active translation; confirm capture and background processes exit.
12. Test your intended video player or game in windowed/borderless mode and at your monitor's scaling settings. Check multiple monitors if relevant.

Use real observations for latency: measure from the spoken phrase to its visible translated caption, and distinguish the first translated word from the final completed caption. The **Last translation** statistic measures only the text request, so it is not an end-to-end latency estimate.
