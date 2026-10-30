# Changelog

## Unreleased

### Recording and chapter narration

- Voice presets can use explicit microphone or Windows WASAPI loopback recording, with device selection, live level, a 29-second limit, preview and cancellation cleanup.
- Add a chapter-based Audiobooks workspace with deterministic prose splitting, voice / emotion overrides, draft protection and visible generation progress.
- Share the existing queue and warm TTS session, retain completed clips after cancellation, and regenerate only changed audio.
- Export chapter WAV / MP3 and merge completed chapters into a book without time stretching; isolate private books and recordings from source publication.

### Workspace layout

- Separate Projects, Voice library and Settings menu pages; searchable project cards with rename and recoverable Trash / restore.
- Side-by-side, directly editable source / translation rows with search, translation filters and page size selection.
- Natural page scrolling in subtitle and dubbing tables; sentence details and auditions open in place and retain the document position.
- Keep settings and project drafts across menu changes; compact project headings prioritize review content.

### Languages and model lifecycle

- Add project-level Chinese, English, Japanese, Spanish and Arabic dubbing targets, with safe switching and target-aware subtitles, media tags and caches.

- Add a separate local voice library with reusable presets, project snapshots and reversible archiving.
- Add multi-row dubbing edits, speaker/text filters and selected-row emotion settings.
- Replace inline logs with a searchable live panel and complete process-log downloads.
- Preserve warm models across timestamp-only script rewrites and explain actual reload reasons.

- Keep IndexTTS warm across auditions with idle expiry, manual release and GPU-stage handoff.
- Add Simplified Chinese / English UI switching with draft preservation.
- Expand source language selection to 12 languages plus auto-detection.
- Preserve old segmentation caches and expand lifecycle/localization regression checks.

## 0.1.0 — initial public source release

- Local English/Japanese transcription, context-aware Chinese translation and editable review proposals.
- Four-step workspace with project search, paginated sentence editing and unsaved-change protection.
- IndexTTS-2.5 voice references, manual emotion presets/intensity, draft auditions and adaptive batching.
- Bounded timing alignment; short utterances retain natural speed.
- Background separation, browser MP4 previews, switchable Chinese captions and dual-track MKV export.
- Source-only distribution, installation documentation, third-party notices and CPU/media regression CI.

The public source release does not include model weights, user videos, reference voices or generated outputs. Complete Windows installation on additional machines and full Japanese/anime/multi-speaker validation remain ongoing work.
