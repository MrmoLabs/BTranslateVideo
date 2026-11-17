# Processing progress / 处理进度

Video projects and audiobook jobs expose a measured progress card. Open **Task progress** in the header from any menu to see running, queued and recent tasks. A floating task button stays available while work is active. Opening/closing the monitor preserves the underlying page position and settings/editor drafts.

The card shows the current phase, real completed/total work when available, queued tasks ahead, task/phase elapsed time, reusable clips, current chapter, recent activity and optional phase ETA. Expand diagnostic logs to read a bounded local log tail without navigating away. Long periods without a semantic update are labelled as no new progress; this is not treated as a failure or proof that computation stopped.

## Percentages and timing

- Percentages describe the **current phase**, not an estimated weighted percentage of the entire workflow. The step list distinguishes transcription, translation, speech and export.
- Model loading, GPU-lock waits and opaque service requests stay indeterminate when no quantitative progress is available. They show elapsed time rather than a simulated increasing percentage.
- ASR uses actual decoded segment timestamps. Semantic segmentation, translation, speech and alignment use actual completed words/sentences. FFmpeg reports output timeline seconds through its progress protocol.
- A successful terminal task shows 100% and stops the waiting animation. Failed, cancelled or interrupted tasks retain their last real counts; they are never marked complete.
- Queue wait begins at submission. Running elapsed time begins at execution. Phase time resets on a real phase transition. Terminal times freeze; restart recovery uses the last known update rather than including application downtime.
- A phase ETA requires at least three increasing samples spanning two seconds. It is a conservative recent-throughput estimate for that phase only. Model loading and the first synthesized clip do not train the speech estimate. Stale estimates are hidden after 30 seconds without new activity.

## Cancellation, history and privacy

Queued futures can be cancelled before execution and are removed from the live queue immediately. Running jobs request cancellation and retain completed clips while the current worker exits. All model, video and audiobook tasks share the existing queue. This UI does not add model instances or interrupt unrelated local model servers.

The monitor lists live tasks and recent persisted project/book operations from the last 24 hours. Each project/book retains its latest task snapshot, including up to eight recent phase events; existing activity and process logs remain available separately. This is not a permanent full task-history database. Diagnostic reads are limited to local process-log tails (up to 15 KB per selected log), never arbitrary user paths.

Progress is polled every two seconds; elapsed clocks update locally every second. A disconnected API preserves the last known task view and shows a connection warning. Unknown service internals, individual neural-network kernels and browser file-upload byte progress are not inferred from CPU/GPU utilization.

## Implementation and verification

`app/progress.py` owns task timestamps, phase transitions, measured counters and ETA samples. Video progress is persisted as `processing`; audiobook snapshots live in `job.processing`. `GET /api/tasks` combines video, audiobook and explicit model-load work, with queue positions. `GET /api/tasks/{kind}/{id}/logs` reads scoped diagnostics. `app/static/progress-ui.js` renders cards and updates only progress regions, preserving form state.

Regression checks cover phase counts, unknown totals, ETA reset, terminal states, workflow ordering, queued cancellation, restart recovery, scoped logs, real FFmpeg output timestamps and ASR worker reports. UI acceptance uses isolated authored synthetic media, simulated ASR/translation and a synthetic local speech service, with actual audio alignment/export; it does not establish new model quality or GPU speed.


## Windows file sharing / Windows 文件占用

JSON snapshots use unique temporary files and UTF-8 without a BOM. Atomic replacement retries short Windows access-denied/sharing/lock conflicts with bounded backoff. Persistent project state, worker results and final synthesis statistics still propagate write failures.

TTS/ASR progress and warm-model phase snapshots use a separate best-effort publisher. If publishing remains blocked, the previous complete snapshot stays available and inference continues; the next update attempts to publish the latest value. A rate-limited warning records the skipped refresh. Audio output failures are still fatal. Existing completed clips remain reusable when restarting a synthesis job.

`tests/test_file_io.py` uses real Windows handles that deny deletion to reproduce the original `tts-progress.pending.json` replacement failure. It verifies retry after a short read, skipping a continuously blocked snapshot, recovery on the next update, concurrent unique temporary files, valid UTF-8, and completion of all synthesis clips plus durable statistics while progress remains locked. These tests do not weaken errors for persistent saves or actual audio writes.
