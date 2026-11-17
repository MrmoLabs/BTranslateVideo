"""Small runtime snapshots bound to an atomic project-content checkpoint."""
from __future__ import annotations
from collections import OrderedDict
import copy
import hashlib
import json
from pathlib import Path
from .file_io import atomic_json

RUNTIME_FIELDS=frozenset({'status','stage','progress','processing','error','logs','tts_runtime','subtitle_review','updated'})


class ProjectStateFiles:
    def __init__(self):
        self.cache=OrderedDict();self.cached_bytes=0

    def _base(self,path):
        stat=path.stat();key=(stat.st_mtime_ns,stat.st_size,stat.st_ino)
        previous=self.cache.get(path)
        if previous and previous[0]==key:
            self.cache.move_to_end(path);return previous[1],previous[2]
        raw=path.read_bytes();data=json.loads(raw.decode('utf-8'));digest=hashlib.sha256(raw).hexdigest()
        old=self.cache.pop(path,None)
        if old:self.cached_bytes-=old[3]
        if len(raw)<=8*1024**2:
            while self.cache and (len(self.cache)>=16 or self.cached_bytes+len(raw)>16*1024**2):
                _,item=self.cache.popitem(last=False);self.cached_bytes-=item[3]
            self.cache[path]=(key,data,digest,len(raw));self.cached_bytes+=len(raw)
        return data,digest

    def read(self,path):
        path=Path(path);base,digest=self._base(path);result=dict(base)
        try:
            runtime=json.loads(path.with_name('project-state.json').read_text(encoding='utf-8'))
            if isinstance(runtime,dict) and runtime.get('version')==1 and runtime.get('base_digest')==digest and isinstance(runtime.get('state'),dict):
                result.update({key:value for key,value in runtime['state'].items() if key in RUNTIME_FIELDS})
        except (FileNotFoundError,ValueError,TypeError):pass
        return result,digest

    def write(self,path,data,changed,digest):
        path=Path(path)
        if set(changed)<=RUNTIME_FIELDS:
            atomic_json(path.with_name('project-state.json'),dict(version=1,base_digest=digest,state={key:data[key] for key in RUNTIME_FIELDS if key in data}))
        else:
            # Include the merged runtime as a checkpoint. The old snapshot's
            # digest no longer matches, so a crash between writes cannot apply
            # old task data to newly edited content.
            atomic_json(path,data)
            previous=self.cache.pop(path,None)
            if previous:self.cached_bytes-=previous[3]

    def runtime(self,path):
        data,_=self.read(path)
        return copy.deepcopy({key:data[key] for key in RUNTIME_FIELDS if key in data})
