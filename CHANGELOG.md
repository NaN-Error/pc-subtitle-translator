# Changelog

## 0.1.1 — 2026-10-24

- Shortened the portable distribution layout to `Sub/r/p` to reduce Windows extraction path lengths.
- Added Windows path-budget, invalid-name, collision, launcher, and import-location checks to the portable builder.
- Omitted four unused Qt static build object files while retaining runtime libraries, plugins, assets, and license notices.
- Preserved the existing audio, translation, credential, and subtitle behavior.
- Published source documentation, sample interface screenshots, contribution guidance, automated CI, and a portable Windows release with a SHA-256 checksum.
- Recorded publication checks on Windows 11: 103 tests passed, 6 platform-specific cases skipped, bundled runtime imports succeeded, and four offscreen Qt previews rendered. Live capture and API acceptance remain pending.

## 0.1.0

- Initial Windows system-audio subtitle translator with WASAPI loopback, OpenAI transcription and text translation, a movable subtitle overlay, language/model settings, appearance controls, and optional Windows Credential Manager storage.

The original build and packaging records are preserved in [docs/verification.json](docs/verification.json). Publication checks are recorded separately in [docs/publication-verification.json](docs/publication-verification.json).
