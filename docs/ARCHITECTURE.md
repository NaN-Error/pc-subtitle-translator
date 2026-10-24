# Architecture

## Data path

The GUI is a native PySide6 application. `Controller` runs an asyncio event loop on a background Python thread. `LoopbackCapture` owns PortAudio and its WASAPI stream on a separate worker; the PortAudio callback only enqueues native PCM in a bounded queue. Audio never needs a temporary file.

`Pcm16Converter` downmixes native channels and uses one stateful soxr stream to produce mono, little-endian PCM16 at 24 kHz. `EnergySegmenter` works on 20 ms frames, keeps up to 300 ms pre-roll, ends turns at a quiet interval or maximum duration, and independently commits after WASAPI goes idle without returning another frame. A subframe tail is preserved on idle. Very short turns receive enough zero padding to reach the app's conservative 250 ms minimum.

The websocket connects to `wss://api.openai.com/v1/realtime?intent=transcription`. It sends the GA `session.update` schema with type `transcription`, PCM rate 24000, `turn_detection: null`, and a selected transcription model. Capture begins only after `session.updated` accepts the configuration.

`gpt-live-transcribe` and `gpt-transcribe` receive a `languages` array when a source hint is selected; supported legacy models receive singular `language`. Automatic mode omits either hint. The live model receives a configurable delay setting. No language-detection sample request or hidden extra provider is used.

Each audio turn is streamed through `input_audio_buffer.append` and explicitly ended with `input_audio_buffer.commit`. The pending metadata queue maps the subsequent `input_audio_buffer.committed` identity to its local sequence number. WebSocket acknowledgment order preserves the submitted commit order. Final transcription events can arrive out of order: `TranscriptOrder` holds them until prior committed turns have completed or expired. Duplicate acknowledgments and late events for retired items are ignored.

Final source segments enter a bounded queue of three pending translations. A single worker calls `POST https://api.openai.com/v1/responses` with streaming enabled, response storage disabled, a bounded output length, target language, and the last two source/translation pairs for context. Spoken text is JSON data under a fixed instruction to translate only the current segment. It is not promoted into a developer instruction. The translator handles incremental text, completion, refusal, incomplete output, API errors, and premature transport closure.

The source preview is provisional recognition. The target overlay streams translation only after a final source segment. This avoids repeatedly retranslating unstable source deltas. An in-flight target caption is marked Translating; completed captions keep their sequence. Old sequence numbers cannot replace a newer overlay caption.

## Lifecycle and responsiveness

No network calls or native capture begin until the user presses Start or the overlay's play control. Pause cancels the worker, closes sockets/HTTP streams, stops capture, and discards buffered audio. Resume creates a fresh session. It does not replay paused audio. Controller cancellation is idempotent so repeated stop/close actions do not interrupt cleanup twice.

The GUI remains on its main thread; worker updates cross through Qt signals. The control panel can change appearance live. Model, routing, language, and timing settings are snapshotted per run. Restart applies those settings.

The overlay is a topmost frameless tool window. A separate small companion window at its right edge owns play/pause, keeping that control interactive when the subtitle surface uses native input transparency. The companion is an independent top-level window so it does not inherit transparent mouse handling from the caption widget; its visibility and destruction are explicitly managed. Width, height, position, font, color, opacity, and original-text visibility are preferences. QTextLayout provides wrapping and bidirectional shaping without HTML parsing; fonts and glyph fallback come from the installed system fonts.

## Bounds and failure behavior

- Native capture queue: about one second; overflow stops capture with a useful error.
- Engine audio queue: 100 resampler blocks; overflow stops the session.
- Audio commit acknowledgment deadline: 12 seconds.
- Transcript completion deadline: 12 seconds, after which a delayed turn is skipped.
- Translation queue: three waiting segments, with oldest pending work dropped if full.
- Translation request total deadline: 15 seconds.
- In-flight translations also inherit a 12-second age limit from their audio commit; expired partial results are cleared.
- Translation output bound: 1024 API output tokens and 16,000 incremental characters.
- Context: two previous completed source/translation pairs.
- Unmatched transcript events: bounded; excessive events stop the session.
- Authentication, access, model, parameter, quota, and rate errors surface to the user.
- Some transport/server failures retry with bounded backoff; repeated translation failures stop the session.

These bounds favor staying near live playback over retaining every word. The status and skipped counter expose loss; this is not an archival transcription tool. Audio level, submitted duration, and latest translation-request duration are diagnostic, not a measurement of complete source-to-display latency.

## Secret and data boundaries

The OpenAI endpoint URLs are constants. User input selects only model IDs and languages, not a provider host. TLS certificate verification remains enabled; redirects are disabled on the HTTP client. No audio or caption file logging is implemented. JSON preferences exclude credentials. Windows key persistence explicitly selects `WinVaultKeyring`; no automatic third-party plaintext backend is selected.

An API key is necessarily present in process memory while used. `store: false` controls Responses storage but is not a zero-retention assertion. This initial app does not request specific data residency or account retention options. Users should apply the appropriate OpenAI project settings for their situation.

## Why use two API stages

Separate live transcription and text translation provide a source-language hint and a flexible text target. The dedicated real-time speech-translation API is optimized for translated speech output, and its documented model has a smaller explicit output-language set. This version uses subtitles only and does not generate a translated voice stream. See the official [transcription guide](https://developers.openai.com/api/docs/guides/realtime-transcription), [Responses streaming guide](https://developers.openai.com/api/docs/guides/streaming-responses), and [translation guide](https://developers.openai.com/api/docs/guides/realtime-translation).
