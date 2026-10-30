"""Local voice presets; project assignments keep independent audio snapshots."""
from __future__ import annotations

import json
import threading
import uuid
import wave
from pathlib import Path
from .config import atomic_json
from .store import now


class VoiceLibrary:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.RLock()

    def folder(self, identifier):
        if len(identifier) != 32 or any(c not in '0123456789abcdef' for c in identifier):
            raise ValueError('无效音色编号')
        return self.root / identifier

    def read(self, identifier):
        with self.lock:
            return json.loads((self.folder(identifier) / 'voice.json').read_text(encoding='utf-8'))

    def list(self, archived=False):
        if not self.root.exists():
            return []
        result=[]
        for path in self.root.glob('*/voice.json'):
            try:
                voice=self.read(path.parent.name)
                if bool(voice.get('archived')) == archived:
                    result.append(voice)
            except (ValueError,OSError):
                continue
        return sorted(result,key=lambda v:v['updated'],reverse=True)

    def save(self, name, description, audio=None, identifier=None, archived=None):
        name=name.strip()
        if not name or len(name)>100 or len(description)>1000:
            raise ValueError('音色名称需为 1～100 字，说明不超过 1000 字')
        with self.lock:
            voice=self.read(identifier) if identifier else dict(id=uuid.uuid4().hex,created=now(),archived=False)
            folder=self.folder(voice['id'])
            if audio is not None:
                with wave.open(str(audio),'rb') as stream:
                    duration=stream.getnframes()/stream.getframerate()
                if not 3 <= duration <= 30:
                    raise ValueError('参考声音需包含音频，长度为 3～30 秒')
                # Version audio files so edits cannot change an already-open player or project snapshot.
                name_audio=uuid.uuid4().hex+'.wav'
                folder.mkdir(parents=True,exist_ok=True)
                import shutil
                shutil.copyfile(audio,folder/name_audio)
                voice.update(audio=name_audio,duration=round(duration,2))
            elif not identifier:
                raise ValueError('请上传参考声音')
            voice.update(name=name,description=description.strip(),updated=now())
            if archived is not None:
                voice['archived']=archived
            atomic_json(folder/'voice.json',voice)
            return voice
