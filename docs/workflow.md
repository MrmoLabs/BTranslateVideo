# Project workflow / 项目工作流

The four steps have distinct responsibilities. The voice library and global settings remain separate menus. Switching project steps keeps the same subtitle draft; navigation does not silently start an unrelated task.

| Step | Main responsibility | 中文操作范围 |
| --- | --- | --- |
| Source & recognition | Prepare source subtitles and the original timeline | **素材与识别**：原片预览、识别、导入原文 SRT、整片重新断句 |
| Translate & review | Choose the output language and check meaning | **翻译与校对**：目标语言、补齐译文、导入译文 SRT、整片优化、批量候选、原声回听、逐句重新识别与润色 |
| Characters & dubbing | Assign voices and expression; synthesize dialogue | **角色与配音**：说话人分离、角色新增/改名/删除与音色、批量角色和情绪、音频/视频试听、整片配音、模型加载/卸载 |
| Mix & export | Mix the finished dialogue and make the deliverables | **混音与导出**：背景分离/独立背景音、音量、保存混音设置、成片生成、字幕开关与下载 |

逐句重新识别留在字幕行旁，因为它服务于校对；整片重新断句归到素材阶段，因为它重建时间轴。配音页显示生效语言，并提供返回字幕页的直接入口。单句试听浮层可返回该句校对，使用同一份草稿。角色配置未准备完整时首次进入会展开；已准备项目优先显示批量工作台。

页头“下一步”只切换步骤；有草稿时显示“保存全部草稿”。只有“一键完成”明确执行完整流水线。缺失上游素材时，当前页提供返回对应步骤的入口。台词或表达有未保存修改时，成片预览会标明旧版本，先保存并重新配音再导出。

## Runtime snapshots / 运行快照

`project.json` is the durable content checkpoint. `project-state.json` contains frequent task/progress/log summaries and is bound to the exact SHA-256 of that checkpoint. A content edit checkpoints the merged state; an old sidecar cannot overwrite a replaced or restored project file. Legacy projects are readable without a bulk migration. To copy or back up a live project manually, copy the entire project folder, including both files.

内容解析缓存按文件版本校验，最多 16 个文件、合计 16 MiB 原始 JSON；单个大于 8 MiB 的文件不缓存。该额度是源文件字节预算，不等于 Python 堆内存占用。高频进度使用小快照，普通读取仍返回合并项目。前端轮询 `/runtime`，正文版本变化后才获取完整项目；日志尾部仍有界更新。保存、跨项目切换和任务提交使用共同的上下文检查，保存期间继续输入不会被当作已保存。

On the authored 2,300-sentence fixture, the checkpoint was 7,319,974 bytes. Thirty progress writes produced 28,260 bytes of snapshots (943 bytes for the final snapshot), versus 219,599,220 bytes for thirty full checkpoints. The content file stayed unchanged. This measures serialized state traffic, not GPU inference acceleration or physical disk allocation.

## Storage / 存储管理

项目页头的“存储管理”显示原片、参考音、当前结果、缓存与备份的逻辑大小。勾选类别后必须先“预览清理范围”，核对路径，再点击永久清理。范围改变即废弃预览；预览十分钟过期，文件或字幕版本变化需要重新预览。任务处理中不能清理。

- 原片、当前参考音和结果、可复用的 `tts-*.wav` / `fit-*.wav`、当前批量校对候选、最近一次历史备份保留。
- 可选范围为已知临时/损坏隔离文件、明确可重建的分析 JSON 缓存、较早历史备份；未识别的缓存文件保守保留。
- 不跟随符号链接或目录联接。删除失败时停止，并准确返回已完成数量，不能把部分清理报告为全部成功。
- 清理是永久删除，无法通过项目回收站恢复。硬链接备份共享数据块，显示的逻辑大小不保证等量释放物理空间。

当前轮没有清理实际用户项目。后端测试仅删除隔离测试目录中的原创夹具。

## Backend acceptance / 后端实机验收

Use the authored fixture matrix independently from application correctness tests:

```powershell
.\.venv\Scripts\python.exe scripts/verify_backends.py --prepare-windows-speech
```

`scripts/fixtures/backend_cases.json` contains original English/Japanese cases for quantities, partial negation and word order, plus two Chinese speech cases. The optional preparer uses installed Windows voices only; missing language voices produce an explicit skip. Alternatively provide authorized 16 kHz mono PCM16 WAVs with matching names through `--fixtures`. No audio or model weights are committed.

```powershell
.\.venv\Scripts\python.exe scripts/verify_backends.py --stages translation --settings settings.local.json
.\.venv\Scripts\python.exe scripts/verify_backends.py --stages asr,tts --fixtures runtime/backend-fixtures
```

工具使用所选配置的本地适配器，不下载模型、不修改设置、不读取用户项目。原生 TTS 在独立会话中连续生成两段以观察保温复用，结束后释放该会话。应用有排队/运行任务或驻留 TTS 时拒绝验收，避免主动挤占当前任务；外部服务始终由用户管理。

Reports are saved in ignored `runtime/backend-checks/<timestamp>/report.json` with backend/model identifiers, elapsed time, original outputs, WER/CER observations and numeric/kana checks. GPU measurements are sampled **whole-device** baseline/peak MiB and can include other applications; native TTS allocator statistics are listed separately where available. “Completed” means the invocation completed, not that quality has been certified. Read/listen against the fixture's manual criteria; synthetic speech does not cover real anime overlap, emotional accuracy, speaker similarity, noise or every language.

On this machine, the English ASR fixture completed in 31.697 s; its WER was 1/22 solely from `8` versus `eight`. English/Japanese translation completed in 10.445 s, retaining the tested quantities, partial negation and sequence. Two Chinese TTS requests completed in 79.622 s including first load; the second reported model reuse with zero model-load time. Japanese ASR was skipped because a Japanese fixture voice was not installed. These are local observations, not portable performance promises.
