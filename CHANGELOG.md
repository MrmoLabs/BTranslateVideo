# Changelog

## Unreleased

- Add native Fun-ASR integration for local video transcription, with an isolated runtime, Chinese dialect support, project-specific recognizer settings, environment checks and a non-destructive ten-second inference test.
- Add transcription-only, translated-subtitle and dubbed-video project goals; show artifact-based status and actionable next steps. Distinguish cached resegmentation from re-recognition, preserve local review drafts and tighten stale-response and revision protection.
- Add source waveforms, precise timing, cursor-based subtitle splitting, guarded adjacent-cue merging and draft undo/redo. Add content-bound human review marks, bulk filters, continuous source playback and keyboard navigation without invalidating completed outputs for mark-only saves.

- Finish Windows transport cleanup after peer-reset WinError 10054 without flooding the console. Match only the incomplete stdlib closing callback, retain other errors, and restore the previous event-loop handler on shutdown.

- Recover full-track timing conflicts within explicit adaptive fallback limits, preserving raw speech and text. Tighten only near-silent edges, expose actual alignment limits and all conflict rows, and preflight provably impossible cached regions before more synthesis.

- Play completed sentence speech directly from full-track alignment or validated caches in the fine-tuning dialog, without loading TTS or saving drafts. Preserve a completed-clip index and clearly identify earlier versions after edits.

- Add direct source/translated SRT downloads to subtitle review. Export all current draft sentences without saving or invalidating outputs; preserve timestamps, skip blank translations with counts, and exclude unadopted candidates.

- Reassign the four video stages to Source & recognition, Translate & review, Characters & dubbing, and Mix & export. Move background mixing and source resegmentation to their owning steps; promote batch tools and preserve cross-step drafts.
- Split content checkpoints from bound runtime snapshots and poll lightweight state until content changes. Centralize task preparation and selection guards, coalesce repeated dispatch, and block exporting an old voice track while dialogue edits are unsaved.
- Add per-project logical storage usage and exact-file, expiring cleanup previews with active-result/reference/audio protection. Add authored, opt-in real-backend acceptance fixtures with separate quality, timing and device-memory observations.

- Make role deletion visible beside each role name and allow deleting the last assigned role while keeping all lines unassigned. Support filtering and bulk reassignment, and reject dubbing/audition of unassigned lines before model loading.

- Resume mixed subtitle-review failures by their original per-cue operation, with explicit selected/all retry scope. Validate and quarantine corrupt speech caches, bound progress polling with a forced final count and per-request snapshots, roll back failed queue reservations, and tighten role-reuse ambiguity checks.

- Reuse established project roles across diarization runs using conservative timeline matching. Keep renamed roles and voice bindings, support assigning detected lines to existing roles, reject duplicate new names, and add guarded delete/merge with explicit line migration and backups.

- Prevent Windows progress-file sharing conflicts from aborting speech synthesis. Use unique temporary JSON files, bounded sharing retries and non-fatal TTS/ASR/model-phase telemetry; preserve errors for durable state, results, statistics and audio.

- Repair failed translation candidates with focused feedback and constrained Japanese-fragment conversion. Recognize source-grounded colloquial numeric ranges and Chinese frame/address units. Joint review automatically re-transcribes uncertain or persistently invalid cues in a shared ASR phase, then resumes translation with durable recovery and no automatic adoption.

- Add editable project role names, cross-page role line selection and filtered speaker-assignment correction. Preserve voice bindings and subtitle candidates when renaming roles.
- Stack floating task progress and diagnostic-log controls together to prevent overlap.

- Promote subtitle correction into a full-width batch workbench: cross-page selection, filtered issue batches, inline editable comparisons, selected adoption, per-cue failures and resumable jobs. Reuse one local translation session or Whisper instance per batch.

- Add per-cue audio re-transcription, source and translation polishing, and joint correction with compare-and-adopt, revision guards, original-audio review, and backup-backed output invalidation.
- Reject Japanese kana in Chinese translation candidates, including names and half-width text; surface derived review warnings for existing affected cues without overwriting saved translations.

- Continue batch translation/review after isolated cue validation failures; preserve text, surface actionable review warnings, and wait for missing translations before dubbing.

- Add optional local Community-1 speaker diarization with CPU/CUDA, speaker count controls, original-audio review, and explicit assignment adoption.
- Accept equivalent explicit clock times in translation checks (for example, `20時` and `晚上八点`) while retaining quantity and minute safeguards.

- Add direct source-audio playback to subtitle rows and review warnings, with draft timing, surrounding context, replay and loop controls. Source clips are decoded locally without loading speech models or saving edits.

- Add sentence retranslation with compare-and-adopt, independent named project roles, queued source-reference sampling, and request-scoped audition autoplay.

- Normalize equivalent numeric forms, isolate retry feedback to batch IDs, and surface uncertain source-number checks. Keep validated drafts when optional review or compaction returns invalid content.

- Compact embedded progress into a status strip with full diagnostics in the task monitor. Restore small role cards and show consistent role and voice names in sentence selectors and batch assignment.

- Use batch-local translation IDs and exclude context from the output namespace; retry exact ID mismatches with smaller groups without guessing sentence correspondence.
- Share output-language controls between subtitle review and dubbing, with a backup-backed option to keep manually edited translations while changing speech language.

- Add measured video/audiobook/model progress, global task monitoring, queue positions, elapsed clocks, conservative phase estimates and scoped diagnostics. Cancel queued work immediately and preserve real counts on failure/cancellation.

- Add explicit IndexTTS preload/cancel/unload controls, confirmed model readiness, live idle countdowns and configurable 0–1440 minute or persistent residency.

- Add independent native / loopback-service model backends, editable model IDs, cache revisions, capability checks and draft connection tests.
- Share configurable transcription and speech adapters across video auditions, dubbing and audiobook narration; document the reference-audio bridge protocol.

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
