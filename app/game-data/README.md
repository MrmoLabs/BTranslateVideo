# Game dialogue lookup

`nier-dialogue.json` is an optional, local-only lookup used by
「游戏音频汉化 → 导入游戏台词」. It maps Wwise media IDs to Japanese, English
and Simplified Chinese text, a speaker name and the original text source.

- Dialogue text: [NierDocs `allDialogueLines.json`](https://github.com/ArthurHeitmann/NierDocs/blob/master/docs/audio/allDialogueLines.json)
- Wwise media names: [F-SERVO](https://github.com/ArthurHeitmann/F-SERVO)
- Traditional-to-Simplified characters: [OpenCC `TSCharacters.txt`](https://github.com/BYVoid/OpenCC) (Apache-2.0)

Game dialogue remains the property of its respective rights holders. Prepare
the lookup locally from source files you have permission to use. Git ignores
the generated lookup. Scanning, recognition, translation and MOD packaging can
run without it.

Generate from local copies:

```powershell
python scripts/build_nier_dialogue.py `
  --dialogue runtime/game-audio-tools/allDialogueLines.json `
  --names runtime/game-audio-tools/wemIdsToNames.dart `
  --opencc runtime/game-audio-tools/TSCharacters.txt
```
