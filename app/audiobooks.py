"""Chapter-oriented local narration. Original implementation inspired by Voicebox's workflow."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import threading
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

from .config import atomic_json, resolve
from .emotions import emotion_fields
from .languages import target_code, tts_language
from .media import Cancelled, check_cancel, ffmpeg, wav_duration
from .pipeline import Pipeline, file_hash, fingerprint, make_voice_clip
from .store import now

MAX_TEXT = 250000


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
        raise ValueError('无效书籍或章节编号')
    return value


def split_prose(text, max_chars=220):
    """Keep prose verbatim apart from surrounding whitespace; never use an LLM to rewrite it."""
    if not isinstance(text, str) or len(text) > MAX_TEXT:
        raise ValueError('每章正文不能超过 250000 字')
    if not 40 <= max_chars <= 500:
        raise ValueError('分句长度需为 40～500 字')
    paragraphs = [p.strip() for p in text.replace('\r\n','\n').replace('\r','\n').split('\n') if p.strip()]
    result=[]
    abbreviations={'mr','mrs','ms','dr','prof','sr','jr','vs','etc','e.g','i.e','u.s','u.k'}
    for paragraph, prose in enumerate(paragraphs):
        start=0; i=0; ends=[]
        while i < len(prose):
            char=prose[i]
            boundary=char in '。！？!?；;؟' or char=='…' and (i+1==len(prose) or prose[i+1]!='…')
            if char=='.':
                token=re.search(r'[A-Za-z.]+$',prose[:i])
                word=token.group().lower().rstrip('.') if token else ''
                decimal=i>0 and i+1<len(prose) and prose[i-1].isdigit() and prose[i+1].isdigit()
                initial=bool(re.fullmatch(r'[A-Za-z]',word)) and i+2<len(prose) and prose[i+1].isspace() and prose[i+2].isupper()
                following=i+1==len(prose) or prose[i+1].isspace() or prose[i+1] in '\"\'”’）)】》'
                boundary=following and not decimal and word not in abbreviations and not initial
            if boundary:
                end=i+1
                while end<len(prose) and prose[end] in '。！？!?；;؟.…\"\'”’）)】》': end+=1
                ends.append(end);i=end
            else:i+=1
        if not ends or ends[-1]!=len(prose):ends.append(len(prose))
        for end in ends:
            piece=prose[start:end].strip();start=end
            while len(piece)>max_chars:
                cut=max(piece.rfind(c,0,max_chars+1) for c in ('，',',','、',':','：',' '))
                if cut<max_chars//3:cut=max_chars
                elif piece[cut]!=' ':cut+=1
                part=piece[:cut].strip()
                if part:result.append(dict(id=uuid.uuid4().hex,text=part,paragraph=paragraph,voice_id=''))
                piece=piece[cut:].strip()
            if piece:result.append(dict(id=uuid.uuid4().hex,text=piece,paragraph=paragraph,voice_id=''))
    if len(result)>8000:raise ValueError('分句超过 8000 条，请拆成多个章节')
    return result


def voice_config(row,chapter):
    return dict(text=row['text'],voice_id=row.get('voice_id') or chapter['voice_id'],language=chapter['language'],**emotion_fields(row))


class BookStore:
    def __init__(self,root):
        self.root=Path(root);self.lock=threading.RLock();self.root.mkdir(parents=True,exist_ok=True)

    def folder(self,book,chapter=None):
        path=self.root/identifier(book)
        return path/'chapters'/identifier(chapter) if chapter else path

    def book(self,book,archived=False):
        with self.lock:
            data=json.loads((self.folder(book)/'book.json').read_text(encoding='utf-8'))
            if data.get('archived') and not archived:raise HTTPException(409,'这本书已归档，请先恢复')
            return data

    def chapter(self,book,chapter):
        if chapter not in self.book(book)['chapters']:raise FileNotFoundError(chapter)
        with self.lock:return json.loads((self.folder(book,chapter)/'chapter.json').read_text(encoding='utf-8'))

    def update_book(self,book,**values):
        with self.lock:
            data=self.book(book,archived=True);data.update(values,updated=now());atomic_json(self.folder(book)/'book.json',data);return data

    def update(self,book,chapter,**values):
        with self.lock:
            data=self.chapter(book,chapter);data.update(values,updated=now());atomic_json(self.folder(book,chapter)/'chapter.json',data);return data

    def create(self,title):
        title=title.strip()
        if not title or len(title)>160:raise ValueError('书名需为 1～160 字')
        data=dict(id=uuid.uuid4().hex,title=title,chapters=[],created=now(),updated=now(),archived=False,job={},outputs={})
        atomic_json(self.folder(data['id'])/'book.json',data);return self.detail(data['id'])

    def add_chapter(self,book,title):
        title=title.strip()
        if not title or len(title)>160:raise ValueError('章节名需为 1～160 字')
        with self.lock:
            parent=self.book(book)
            if len(parent['chapters'])>=500:raise ValueError('每本书最多 500 章')
            cid=uuid.uuid4().hex
            data=dict(id=cid,title=title,text='',sentences=[],voice_id='',language='zh',gap=.25,revision=0,audios={},outputs={},status='idle',progress={},error='',updated=now())
            atomic_json(self.folder(book,cid)/'chapter.json',data)
            self.update_book(book,chapters=parent['chapters']+[cid],outputs={})
            return data

    def summary(self,book,chapter):
        data=self.chapter(book,chapter)
        return {**{k:data[k] for k in ('id','title','revision','status','progress','error','outputs','updated')},'sentences':len(data['sentences']),'ready':len(data['audios']),'duration':round(sum(a['duration'] for a in data['audios'].values()),2)}

    def detail(self,book):
        data=self.book(book);data['chapters']=[self.summary(book,c) for c in data['chapters']];return data

    def list(self,archived=False):
        result=[]
        for path in self.root.glob('*/book.json'):
            try:
                data=self.book(path.parent.name,archived=True)
                if data.get('archived',False)==archived:result.append({k:data[k] for k in ('id','title','updated','archived','job')}|{'chapters':len(data['chapters'])})
            except (OSError,ValueError):continue
        return sorted(result,key=lambda item:item['updated'],reverse=True)

    def save(self,book,chapter,payload):
        with self.lock:
            old=self.chapter(book,chapter)
            if payload['revision']!=old['revision']:raise HTTPException(409,'章节已经更新，当前草稿未覆盖，请先保留修改后重新打开章节')
            text=payload['text'];title=payload['title'].strip();rows=payload['sentences']
            if not title or len(title)>160 or len(text)>MAX_TEXT:raise ValueError('章节标题或正文长度无效')
            if len(rows)>8000:raise ValueError('每章最多 8000 句')
            clean=[];seen=set()
            for row in rows:
                sid=identifier(row['id']);value=row['text'].strip();voice=row.get('voice_id','')
                if sid in seen or not value or len(value)>1500:raise ValueError('句子编号重复、内容为空或超过 1500 字')
                if voice:identifier(voice)
                seen.add(sid);clean.append(dict(id=sid,text=value,voice_id=voice,paragraph=max(0,int(row.get('paragraph',0))),**emotion_fields(row)))
            default=payload.get('voice_id','')
            if default:identifier(default)
            language=target_code(payload.get('language','zh'));gap=float(payload.get('gap',.25))
            if not math.isfinite(gap) or not 0<=gap<=3:raise ValueError('句间停顿需为 0～3 秒')
            changed=dict(title=title,text=text,sentences=clean,voice_id=default,language=language,gap=gap)
            configuration={**old,**changed}
            audios={row['id']:old['audios'][row['id']] for row in clean if row['id'] in old['audios'] and old['audios'][row['id']]['config']==voice_config(row,configuration)}
            outputs=old['outputs'] if all(old.get(k)==v for k,v in changed.items() if k!='title') else {}
            result=self.update(book,chapter,**changed,audios=audios,outputs=outputs,revision=old['revision']+1,error='',status='idle')
            self.update_book(book,outputs={});return result

    def recover(self):
        for book in self.list():
            parent=self.book(book['id']);job=parent.get('job',{})
            if job.get('status') in {'queued','running','cancelling'}:
                self.update_book(book['id'],job={**job,'status':'interrupted','phase':'任务已中断，可继续并复用缓存'})
            for cid in parent['chapters']:
                chapter=self.chapter(book['id'],cid)
                if chapter['status'] in {'queued','running','cancelling'}:self.update(book['id'],cid,status='interrupted',error='上次任务中断，已完成句子可复用')


def concatenate(paths,destination,gap,settings,log,cancel):
    """Normalize a streaming concat to WAV; no speed filter, no in-memory whole-book buffer."""
    if not paths:raise ValueError('没有可合并的音频')
    folder=destination.parent;folder.mkdir(parents=True,exist_ok=True)
    def normalize(path):
        import wave
        path=Path(path)
        with wave.open(str(path),'rb') as source:
            if (source.getframerate(),source.getnchannels(),source.getsampwidth())==(24000,1,2):return path
            duration=source.getnframes()/source.getframerate()
        key=fingerprint(dict(file=path.name,sha256=file_hash(path)))
        normalized=path.parent/('pcm-'+key+'.wav')
        if normalized.is_file():
            try:valid=abs(wav_duration(normalized)-duration)<.005
            except (OSError,EOFError,wave.Error):valid=False
            if not valid:normalized.unlink()
        if not normalized.is_file():
            temporary=normalized.with_suffix('.pending.wav')
            try:
                ffmpeg(settings,['-i',str(path),'-ar','24000','-ac','1','-c:a','pcm_s16le','-threads','1',str(temporary)],path.parent/('normalize-'+key+'.log'),cancel)
                if abs(wav_duration(temporary)-duration)>.005:raise ValueError('音频重采样改变了朗读时长，未采用该片段')
                os.replace(temporary,normalized)
            finally:temporary.unlink(missing_ok=True)
        return normalized
    unique=list(dict.fromkeys(map(Path,paths)))
    with ThreadPoolExecutor(max_workers=settings.audio_workers) as pool:
        normalized=dict(zip(unique,pool.map(normalize,unique)))
    paths=[normalized[Path(path)] for path in paths]
    listing=folder/('concat-'+uuid.uuid4().hex+'.txt');silence=folder/('silence-'+uuid.uuid4().hex+'.wav')
    try:
        if gap:
            import wave
            with wave.open(str(silence),'wb') as wav:
                wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000);wav.writeframes(b'\0\0'*round(24000*gap))
        lines=[]
        for index,path in enumerate(paths):
            if index and gap:lines.append(silence)
            lines.append(Path(path))
        listing.write_text(''.join("file '"+str(p.resolve()).replace('\\','/').replace("'","'\\''")+"'\n" for p in lines),encoding='utf-8')
        pending=destination.with_suffix('.pending'+destination.suffix)
        arguments=['-f','concat','-safe','0','-i',str(listing),'-vn','-ar','24000','-ac','1']
        arguments+=['-c:a','libmp3lame','-b:a','128k'] if destination.suffix=='.mp3' else ['-c:a','pcm_s16le']
        try:
            ffmpeg(settings,[*arguments,str(pending)],log,cancel);check_cancel(cancel);os.replace(pending,destination)
        finally:pending.unlink(missing_ok=True)
    finally:listing.unlink(missing_ok=True);silence.unlink(missing_ok=True)


class Narration:
    def __init__(self,store,voices,pipeline):self.store,self.voices,self.pipeline=store,voices,pipeline
    def key(self,book):return 'audiobook:'+identifier(book)
    def busy(self,book):return self.pipeline.busy(self.key(book))
    def editable(self,book):
        self.store.book(book)
        if self.busy(book):raise HTTPException(409,'这本书正在处理，请完成或取消后再编辑')

    def submit(self,book,chapter_ids,settings,kind='generate',format='mp3',sentence_ids=None):
        with self.pipeline.lock:
            self.editable(book);parent=self.store.book(book);ids=chapter_ids if chapter_ids is not None else parent['chapters']
            if not ids or len(set(ids))!=len(ids) or any(cid not in parent['chapters'] for cid in ids):raise ValueError('请选择有效章节')
            if format not in {'mp3','wav'}:raise ValueError('导出格式仅支持 MP3 或 WAV')
            if sentence_ids and len(ids)!=1:raise ValueError('逐句生成只能选择一个章节')
            for cid in ids:
                chapter=self.store.chapter(book,cid)
                if kind=='generate':
                    tts_language({'target_language':chapter['language']},settings.index_version)
                    rows=[r for r in chapter['sentences'] if not sentence_ids or r['id'] in sentence_ids]
                    if not rows or sentence_ids and set(sentence_ids)!={r['id'] for r in rows}:raise ValueError('章节尚未分句，或所选句子不存在')
                    for row in rows:
                        vid=voice_config(row,chapter)['voice_id']
                        if not vid:raise ValueError('请为每个章节选择音色，或逐句指定音色')
                        try:voice=self.voices.read(vid)
                        except FileNotFoundError as exc:raise ValueError('所选音色不存在，请重新选择') from exc
                        if voice.get('archived'):raise ValueError('所选音色已归档，请重新选择音色')
                elif len(chapter['audios'])!=len(chapter['sentences']) or not chapter['sentences']:raise ValueError('请先完成所有所选章节的配音，再合并导出')
            event=threading.Event();key=self.key(book);self.pipeline.events[key]=event
            self.store.update_book(book,job=dict(status='queued',kind=kind,phase='排队中',completed=0,total=len(sentence_ids) if sentence_ids else sum(len(self.store.chapter(book,c)['sentences']) for c in ids),chapters=ids))
            self.pipeline.pool.submit(self.execute,book,ids,settings,event,kind,format,sentence_ids)
            return self.store.detail(book)

    def cancel(self,book):
        with self.pipeline.lock:
            event=self.pipeline.events.get(self.key(book))
            if event:event.set();self.job(book,status='cancelling',phase='正在取消')

    def job(self,book,**values):
        with self.store.lock:
            current=self.store.book(book).get('job',{});self.store.update_book(book,job={**current,**values})

    def execute(self,book,ids,settings,cancel,kind,format,sentence_ids):
        try:
            self.job(book,status='running',phase='准备音色',error='')
            if kind=='generate':
                completed=0
                for cid in ids:
                    check_cancel(cancel);self.generate_chapter(book,cid,settings,cancel,sentence_ids,completed)
                    completed+=len(self.store.chapter(book,cid)['sentences']) if not sentence_ids else len(sentence_ids)
                self.job(book,status='done',phase='配音完成',completed=completed)
            else:
                self.job(book,phase='合并导出')
                paths=[]
                for index,cid in enumerate(ids):
                    self.job(book,completed=index,total=len(ids))
                    chapter=self.store.chapter(book,cid)
                    if not chapter['outputs'].get('wav'):self.merge_chapter(book,cid,settings,cancel)
                    paths.append(self.store.folder(book,cid)/self.store.chapter(book,cid)['outputs']['wav'])
                key=fingerprint([(cid,self.store.chapter(book,cid)['outputs']['wav']) for cid in ids])
                name='book-'+key+'.'+format;out=self.store.folder(book)/name
                if not out.exists():concatenate(paths,out,1,settings,self.store.folder(book)/'process.log',cancel)
                self.store.update_book(book,outputs={**self.store.book(book)['outputs'],format:name})
                self.job(book,status='done',phase='合并导出完成',completed=len(paths),total=len(paths))
        except Exception as exc:
            status='cancelled' if isinstance(exc,Cancelled) else 'failed'
            self.job(book,status=status,phase='任务已取消' if status=='cancelled' else '处理失败',error=str(exc) or type(exc).__name__)
            for cid in ids:
                if self.store.chapter(book,cid)['status']=='running':self.store.update(book,cid,status=status,error=str(exc))
        finally:
            with self.pipeline.lock:self.pipeline.events.pop(self.key(book),None)

    def generate_chapter(self,book,cid,settings,cancel,sentence_ids,offset):
        chapter=self.store.chapter(book,cid);folder=self.store.folder(book,cid);(folder/'cache').mkdir(exist_ok=True)
        (folder/'references').mkdir(exist_ok=True)
        self.store.update(book,cid,status='running',error='',progress={'phase':'准备音色','completed':0,'total':len(chapter['sentences'])})
        model=resolve(settings.index_model)
        if not model.is_dir():raise ValueError('IndexTTS 模型目录不存在')
        signature=sorted((str(p.relative_to(model)),p.stat().st_size,p.stat().st_mtime_ns) for p in model.rglob('*') if p.is_file() and '.cache' not in p.parts)
        rows=[r for r in chapter['sentences'] if not sentence_ids or r['id'] in sentence_ids]
        plans=[];pending=[];seen=set();references={}
        for row in rows:
            check_cancel(cancel);config=voice_config(row,chapter);vid=config['voice_id']
            if vid not in references:
                voice=self.voices.read(vid)
                if voice.get('archived'):raise ValueError('所选音色已归档，请重新选择')
                source=self.voices.folder(vid)/voice['audio'];digest=file_hash(source);target=folder/'references'/(digest+'.wav')
                if not target.exists():shutil.copyfile(source,target)
                references[vid]=(str(target.relative_to(folder)),digest,voice['name'])
            reference,digest,name=references[vid]
            item={**row,'zh':row['text'],'speaker':vid}
            clip=make_voice_clip(item,folder,reference,settings,signature,digest,chapter['language']);path=Path(clip['output'])
            if path.exists():
                try:
                    if wav_duration(path)<=0:path.unlink()
                except (OSError,EOFError,ValueError,wave.Error):path.unlink(missing_ok=True)
            plans.append((row,clip,config,digest,name))
            if not path.exists() and clip['output'] not in seen:pending.append(clip);seen.add(clip['output'])
        original=dict(chapter['audios'])
        def publish():
            ready=dict(original)
            for row,clip,config,digest,name in plans:
                path=Path(clip['output'])
                if path.is_file():
                    try:duration=wav_duration(path)
                    except (OSError,EOFError,ValueError,wave.Error):continue
                    if duration>0:ready[row['id']]=dict(file='cache/'+path.name,duration=duration,config=config,voice_name=name,voice_hash=digest)
            self.store.update(book,cid,audios=ready)
            count=sum(r['id'] in ready and ready[r['id']]['file']=='cache/'+Path(clip['output']).name for r,clip,config,digest,name in plans)
            self.job(book,completed=offset+count,current_chapter=cid)
        owner=self
        class AdapterStore:
            def folder(self,_):return folder
            def update(self,_,**values):
                if 'tts_runtime' in values:
                    runtime=values['tts_runtime'];owner.store.update(book,cid,progress=runtime)
                    owner.job(book,phase=runtime.get('phase','正在合成'));publish()
            def log(self,_,message):
                with (folder/'process.log').open('a',encoding='utf-8') as log:log.write(message+'\n')
        adapter=SimpleNamespace(store=AdapterStore(),tts=self.pipeline.tts)
        def progress(_,stage,percent,message):self.job(book,phase='正在合成');publish()
        adapter.progress=progress
        try:Pipeline.synthesize(adapter,cid,pending,len(plans),settings,cancel,'audiobook')
        finally:publish()
        check_cancel(cancel)
        current=self.store.chapter(book,cid)
        if any(current['audios'].get(row['id'],{}).get('file')!='cache/'+Path(clip['output']).name for row,clip,config,digest,name in plans):raise ValueError('部分句子未生成有效音频，已完成片段保留')
        if len(current['audios'])==len(current['sentences']):self.merge_chapter(book,cid,settings,cancel)
        self.store.update(book,cid,status='done',progress={'phase':'章节配音完成','completed':len(rows),'total':len(rows),'reused':len(plans)-len(pending)})

    def merge_chapter(self,book,cid,settings,cancel):
        chapter=self.store.chapter(book,cid);folder=self.store.folder(book,cid)
        paths=[folder/chapter['audios'][row['id']]['file'] for row in chapter['sentences']]
        key=fingerprint(dict(files=[str(p.relative_to(folder)) for p in paths],gap=chapter['gap']))
        self.job(book,phase='合并章节音频');outputs={}
        for format in ('wav','mp3'):
            name='chapter-'+key+'.'+format
            if not (folder/name).exists():concatenate(paths,folder/name,chapter['gap'],settings,folder/'process.log',cancel)
            outputs[format]=name
        self.store.update(book,cid,outputs=outputs)
