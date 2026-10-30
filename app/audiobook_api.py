"""Local API for audiobook projects and reference recording."""
from pathlib import Path
import re
from fastapi import APIRouter,HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel,Field
from .config import load_settings
from .audiobooks import BookStore,Narration,split_prose
from .recording import Recorder

class BookCreate(BaseModel):
    title:str=Field(min_length=1,max_length=160)
class Sentence(BaseModel):
    id:str
    text:str=Field(min_length=1,max_length=1500)
    voice_id:str=''
    paragraph:int=Field(default=0,ge=0)
    emotion:str='inherit'
    emotion_strength:str='medium'
class ChapterSave(BaseModel):
    title:str=Field(min_length=1,max_length=160)
    text:str=Field(max_length=250000)
    sentences:list[Sentence]=Field(max_length=8000)
    voice_id:str=''
    language:str='zh'
    gap:float=Field(default=.25,ge=0,le=3)
    revision:int=Field(ge=0)
class Split(BaseModel):
    text:str=Field(max_length=250000)
    max_chars:int=Field(default=220,ge=40,le=500)
class Generate(BaseModel):
    chapter_ids:list[str]|None=None
    sentence_ids:list[str]|None=None
    format:str='mp3'
class RecordingStart(BaseModel):
    kind:str
    device:int|None=None

def attach(app,store,pipeline,voices):
    books=BookStore(store.root.parent/'audiobooks');narration=Narration(books,voices,pipeline)
    recordings=Recorder(store.root.parent/'runtime'/'recordings')
    app.state.books,app.state.narration,app.state.recordings=books,narration,recordings
    router=APIRouter(prefix='/api')
    @router.get('/recording/devices')
    def devices():return recordings.devices()
    @router.post('/recording/start')
    def start(data:RecordingStart):return recordings.start(data.kind,data.device)
    @router.get('/recording/{token}')
    def status(token:str):return recordings.status(token)
    @router.post('/recording/{token}/stop')
    def stop(token:str):return recordings.stop(token)
    @router.post('/recording/{token}/cancel')
    def cancel(token:str):return recordings.stop(token,discard=True)
    @router.get('/recording/{token}/audio')
    def audio(token:str):return FileResponse(recordings.audio(token),media_type='audio/wav')
    @router.get('/audiobooks')
    def list_books(archived:bool=False):return books.list(archived)
    @router.post('/audiobooks')
    def create(data:BookCreate):return books.create(data.title)
    @router.post('/audiobooks/split')
    def split(data:Split):return {'sentences':split_prose(data.text,data.max_chars)}
    @router.get('/audiobooks/{book}')
    def detail(book:str):return books.detail(book)
    @router.post('/audiobooks/{book}/archive')
    def archive(book:str,archived:bool=True):
        with pipeline.lock:
            if narration.busy(book):raise HTTPException(409,'请先完成或取消书籍任务')
            return books.update_book(book,archived=archived)
    @router.post('/audiobooks/{book}/chapters')
    def add(book:str,data:BookCreate):
        with pipeline.lock:narration.editable(book);return books.add_chapter(book,data.title)
    @router.get('/audiobooks/{book}/chapters/{chapter}')
    def chapter(book:str,chapter:str):return books.chapter(book,chapter)
    @router.put('/audiobooks/{book}/chapters/{chapter}')
    def save(book:str,chapter:str,data:ChapterSave):
        with pipeline.lock:narration.editable(book);return books.save(book,chapter,data.model_dump())
    @router.post('/audiobooks/{book}/generate')
    def generate(book:str,data:Generate):return narration.submit(book,data.chapter_ids,load_settings(),sentence_ids=data.sentence_ids)
    @router.post('/audiobooks/{book}/export')
    def export(book:str,data:Generate):return narration.submit(book,data.chapter_ids,load_settings(),kind='export',format=data.format)
    @router.post('/audiobooks/{book}/cancel')
    def cancel_book(book:str):books.book(book);narration.cancel(book);return books.detail(book)
    @router.get('/audiobooks/{book}/files/{name}')
    def book_file(book:str,name:str,download:bool=False):
        books.book(book)
        if not re.fullmatch(r'book-[a-f0-9]{24}\.(mp3|wav)',name):raise HTTPException(404,'文件不可访问')
        path=books.folder(book)/name
        if not path.is_file():raise HTTPException(404,'尚未生成此文件')
        return FileResponse(path,filename=name if download else None)
    @router.get('/audiobooks/{book}/chapters/{chapter}/files/{name}')
    def chapter_file(book:str,chapter:str,name:str,download:bool=False):
        data=books.chapter(book,chapter);folder=books.folder(book,chapter)
        if re.fullmatch(r'chapter-[a-f0-9]{24}\.(mp3|wav)',name):path=folder/name
        elif re.fullmatch(r'tts-[a-f0-9]{24}\.wav',name) and 'cache/'+name in {value['file'] for value in data['audios'].values()}:path=folder/'cache'/name
        else:raise HTTPException(404,'文件不可访问')
        if not path.is_file():raise HTTPException(404,'尚未生成此文件')
        return FileResponse(path,filename=name if download else None)
    app.include_router(router)
    return books,recordings
