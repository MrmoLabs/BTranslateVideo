"use strict";
const novels={books:[],book:null,chapter:null,rows:[],dirty:false,generation:0,version:0,page:0,splitSource:'',saving:false,polling:false};
const novelBusy=()=>['queued','running','cancelling'].includes(novels.book?.job?.status);
const novelChapterUrl=(cid=novels.chapter?.id)=>'/api/audiobooks/'+novels.book.id+'/chapters/'+cid;
const novelAudioUrl=(filename,cid=novels.chapter?.id,download=false)=>novelChapterUrl(cid)+'/files/'+encodeURIComponent(filename.replace(/^cache\//,''))+(download?'?download=true':'');
function novelDirty(value=true){novels.dirty=value;novels.generation++;$('#novel-draft-dot').hidden=!value;$('#novel-draft-status').textContent=tr(value?'有未保存的修改':'章节已保存');$('#novel-prose-count').textContent=tr('{0} 字', $('#novel-prose').value.length);renderNovelButtons();if(novels.chapter)$('#novel-audio-version').hidden=!value||!novels.chapter.outputs.wav;}
async function allowNovelLeave(){
  if(novels.saving)throw Error(tr('章节正在保存，请稍候再切换'));
  if(!novels.dirty)return true;
  const choice=await confirmChoice(tr('当前章节有未保存的正文或逐句设置。保存后再切换，或保留在这里继续编辑。'),{save:true,title:tr('保留当前修改')});
  if(choice==='cancel')return false;if(choice==='save'){await saveNovelChapter();if(novels.dirty)throw Error(tr('仍有新输入的修改未保存，请保存后重试'));}else novelDirty(false);return true;
}
$('#menu-novels').onclick=()=>guard(async()=>{pauseMedia();showSection('novels');await refreshNovels();await refreshLibrary();});
$('#back-novels').onclick=()=>guard(async()=>{if(!await allowNovelLeave())return;novels.version++;novels.book=null;novels.chapter=null;renderNovels();await refreshNovels();});
async function refreshNovels(){
  const archived=$('#novel-archived').checked,data=await api('/api/audiobooks?archived='+archived);
  if(archived!==$('#novel-archived').checked)return;novels.books=data;renderNovels();
}
function renderNovels(){
  const book=novels.book;$('#novel-index').hidden=!!book;$('#novel-workspace').hidden=!book;
  if(!book){
    const query=$('#novel-search').value.trim().toLowerCase(),items=novels.books.filter(b=>b.title.toLowerCase().includes(query));
    const key=JSON.stringify([items,I18N.locale()]);if(novels.listKey===key)return;novels.listKey=key;
    $('#novel-books').innerHTML=items.map(b=>`<article class="card project-card"><h2>${esc(b.title)}</h2><p class="subtle">${esc(tr('{0} 个章节',b.chapters))}</p><p class="subtle">${esc(tr(b.job?.status==='running'||b.job?.status==='queued'?'任务处理中':'全部保存在本机'))}</p><div class="button-group">${b.archived?`<button data-restore-book="${b.id}">${esc(tr('恢复'))}</button>`:`<button class="primary" data-open-book="${b.id}">${esc(tr('打开书籍'))}</button><button class="text-button" data-archive-book="${b.id}">${esc(tr('归档'))}</button>`}</div></article>`).join('')||`<div class="card project-empty"><h2>${esc(tr('还没有书籍'))}</h2><p class="subtle">${esc(tr('新建书籍，再为每章粘贴正文。'))}</p></div>`;
    return;
  }
  $('#novel-book-title').textContent=book.title;$('#novel-book-meta').textContent=tr('{0} 个章节',book.chapters.length);
  const key=JSON.stringify([book.chapters,novels.chapter?.id,I18N.locale()]);
  if(novels.chapterListKey!==key){novels.chapterListKey=key;$('#novel-chapter-list').innerHTML=book.chapters.map((c,i)=>`<button data-open-chapter="${c.id}" class="${c.id===novels.chapter?.id?'selected':''}"><b>${i+1}. ${esc(c.title)}</b><small>${esc(tr('{0} / {1} 句已有音频',c.ready,c.sentences))}</small></button>`).join('')||`<p class="subtle">${esc(tr('还没有章节'))}</p>`;}
  const job=book.job||{},busy=novelBusy();$('#novel-job-phase').textContent=tr(job.phase||'等待开始');
  $('#novel-job-count').textContent=job.total?tr('已完成 {0} / {1}',job.completed||0,job.total):tr('音频按句保存，取消或中断后可继续复用。');
  $('#novel-job-progress').value=job.total?Math.min(100,100*(job.completed||0)/job.total):0;$('#novel-job-error').textContent=job.error||'';$('#novel-cancel-job').hidden=!busy;
  $('#novel-book-downloads').innerHTML=Object.entries(book.outputs||{}).map(([format,name])=>`<a class="novel-download" href="/api/audiobooks/${book.id}/files/${encodeURIComponent(name)}?download=true" download>${esc(tr('下载全书 {0}',format.toUpperCase()))}</a>`).join('');
  $('#novel-no-chapter').hidden=!!novels.chapter;$('#novel-chapter-editor').hidden=!novels.chapter;renderNovelButtons();renderNovelRows();
}
$('#novel-search').oninput=renderNovels;$('#novel-archived').onchange=()=>guard(refreshNovels);
$('#novel-books').onclick=e=>guard(async()=>{
  const button=e.target.closest('button');if(!button)return;
  if(button.dataset.openBook){await openNovel(button.dataset.openBook);return;}
  const id=button.dataset.archiveBook||button.dataset.restoreBook;if(!id)return;
  if(button.dataset.archiveBook&&await confirmChoice(tr('归档书籍会保留正文、音色与音频，可随时恢复。'),{title:tr('归档书籍'),proceed:tr('归档')})==='cancel')return;
  await api('/api/audiobooks/'+id+'/archive?archived='+!!button.dataset.archiveBook,{method:'POST'});await refreshNovels();
});
async function openNovel(id){
  if(!await allowNovelLeave())return;pauseNovelAudio();novels.switching=true;renderNovelButtons();try{const version=++novels.version,book=await api('/api/audiobooks/'+id);if(version!==novels.version)return;
  novels.book=book;novels.chapter=null;novels.rows=[];novels.chapterListKey=null;renderNovels();
  if(book.chapters.length)await openNovelChapter(book.chapters[0].id);
  }finally{novels.switching=false;renderNovelButtons();}
}
async function openNovelChapter(id){
  if(id===novels.chapter?.id)return;if(!await allowNovelLeave())return;pauseNovelAudio();
  novels.switching=true;renderNovelButtons();try{const book=novels.book.id,version=++novels.version,chapter=await api(novelChapterUrl(id));if(version!==novels.version||book!==novels.book?.id)return;
  novels.chapter=chapter;novels.rows=clone(chapter.sentences);novels.splitSource=chapter.text;novels.page=0;novels.rowKey=null;novels.chapterListKey=null;
  $('#novel-chapter-title').value=chapter.title;$('#novel-prose').value=chapter.text;$('#novel-language').value=chapter.language;$('#novel-gap').value=chapter.gap;$('#novel-sentence-search').value='';
  $('#novel-prose-panel').open=!chapter.sentences.length;novels.voiceKey=null;renderNovelVoices(chapter.voice_id);$('#novel-voice').value=chapter.voice_id;novelDirty(false);renderNovelAudio();renderNovels();
  }finally{novels.switching=false;renderNovelButtons();}
}
$('#novel-chapter-list').onclick=e=>{const button=e.target.closest('[data-open-chapter]');if(button)guard(()=>openNovelChapter(button.dataset.openChapter));};
function openNovelName(kind){
  novels.nameKind=kind;$('#novel-name-form').reset();$('#novel-name-error').textContent='';$('#novel-name-title').textContent=tr(kind==='book'?'新建书籍':'新建章节');$('#novel-name-label').textContent=tr(kind==='book'?'书名':'章节名称');$('#novel-name-dialog').showModal();
}
$('#new-novel').onclick=()=>guard(async()=>{if(await allowNovelLeave())openNovelName('book');});$('#new-novel-chapter').onclick=()=>guard(async()=>{if(await allowNovelLeave())openNovelName('chapter');});
$('#novel-name-form').onsubmit=async e=>{
  e.preventDefault();const button=e.target.querySelector('[type=submit]');button.disabled=true;
  try{
    const result=await api(novels.nameKind==='book'?'/api/audiobooks':'/api/audiobooks/'+novels.book.id+'/chapters',json('POST',{title:e.target.elements.title.value}));
    $('#novel-name-dialog').close();
    if(novels.nameKind==='book')await openNovel(result.id);else{await refreshNovelBook();await openNovelChapter(result.id);}
    await refreshNovels();
  }catch(error){$('#novel-name-error').textContent=error.message;}finally{button.disabled=false;}
};
function novelVoiceOptions(value,inherited=false){
  return `<option value="">${esc(tr(inherited?'沿用章节音色':'选择预设音色'))}</option>`+(!state.voices.some(v=>v.id===value)&&value?`<option value="${esc(value)}">${esc(tr('当前音色不可用，请重新选择'))}</option>`:'')+state.voices.map(v=>`<option value="${v.id}" ${v.id===value?'selected':''}>${esc(v.name)}</option>`).join('');
}
function renderNovelVoices(initial){
  $('#novel-voice-help').hidden=state.voices.length>0;if(!novels.chapter)return;const key=JSON.stringify([state.voices,novels.chapter.id,I18N.locale()]);if(key===novels.voiceKey&&$('#novel-voice').options.length)return;
  const selected=typeof initial==='string'?initial:$('#novel-voice').value;$('#novel-voice').innerHTML=novelVoiceOptions(selected);$('#novel-voice').value=selected;novels.voiceKey=key;novels.rowKey=null;renderNovelRows();
}
function renderNovelButtons(){
  const busy=novelBusy(),saving=novels.saving||novels.switching;
  for(const id of ['new-novel-chapter','novel-generate-all','novel-export'])$('#'+id).disabled=busy||saving||!novels.book;
  $('#novel-generate-all').disabled=busy||saving||!novels.book?.chapters.length;
  $('#novel-export').disabled=busy||saving||!novels.book?.chapters.length||!novels.book.chapters.every(c=>c.sentences>0&&c.ready===c.sentences);
  $('#novel-export').title=tr('完成所有章节配音后，可合并导出全书');
  for(const id of ['novel-save','novel-generate','novel-split'])$('#'+id).disabled=busy||saving||!novels.chapter;
  for(const id of ['novel-chapter-title','novel-prose','novel-voice','novel-language','novel-gap'])$('#'+id).disabled=busy||saving;
  $$('[data-novel-field]').forEach(el=>el.disabled=busy||saving);$$('[data-novel-generate]').forEach(el=>el.disabled=busy||saving);
}
for(const id of ['novel-chapter-title','novel-prose','novel-voice','novel-language','novel-gap'])$('#'+id).addEventListener('input',()=>novelDirty());
async function splitNovelDraft(confirm=true){
  if(!novels.chapter)return;const text=$('#novel-prose').value;
  if(!text.trim())throw Error(tr('请先录入章节正文'));
  if(confirm&&novels.rows.length&&await confirmChoice(tr('按当前正文重新分句？相同文字会保留逐句音色设置，旧音频缓存仍保留。'),{title:tr('重新分句'),proceed:tr('自动分句')})==='cancel')return false;
  const generation=novels.generation,version=novels.version,result=await api('/api/audiobooks/split',json('POST',{text}));
  if(version!==novels.version||generation!==novels.generation)throw Error(tr('分句期间内容又有修改，请重新分句；当前草稿未替换。'));
  const existing=new Map();novels.rows.forEach(row=>{if(!existing.has(row.text))existing.set(row.text,[]);existing.get(row.text).push(row);});
  novels.rows=result.sentences.map(row=>{const old=existing.get(row.text)?.shift();return old?{...old,paragraph:row.paragraph}:row;});novels.splitSource=text;novels.page=0;novels.rowKey=null;novelDirty();renderNovelRows();return true;
}
$('#novel-split').onclick=()=>guard(()=>splitNovelDraft());
async function saveNovelChapter(){
  if(novels.saving)throw Error(tr('章节正在保存，请稍候再切换'));if(!novels.chapter)return;
  if($('#novel-prose').value!==novels.splitSource){if(!await splitNovelDraft())throw Error(tr('请先完成分句再保存章节'));}
  const id=novels.chapter.id,book=novels.book.id,version=novels.version,generation=novels.generation;
  const payload={title:$('#novel-chapter-title').value,text:$('#novel-prose').value,sentences:clone(novels.rows),voice_id:$('#novel-voice').value,language:$('#novel-language').value,gap:Number($('#novel-gap').value),revision:novels.chapter.revision};
  novels.saving=true;renderNovelButtons();
  try{
    const chapter=await api(novelChapterUrl(id),json('PUT',payload));if(version!==novels.version||book!==novels.book?.id)return;
    novels.chapter=chapter;if(generation===novels.generation){novels.rows=clone(chapter.sentences);novelDirty(false);if(novels.rows.length)$('#novel-prose-panel').open=false;}novels.rowKey=null;renderNovelRows();renderNovelAudio();await refreshNovelBook();toast(tr('章节已保存'));
  }finally{novels.saving=false;renderNovelButtons();}
}
$('#novel-save').onclick=()=>guard(saveNovelChapter);
function filteredNovelRows(){const query=$('#novel-sentence-search').value.trim().toLowerCase();return novels.rows.map((row,index)=>({row,index})).filter(({row})=>!query||row.text.toLowerCase().includes(query));}
function renderNovelRows(force=false){
  if(!novels.chapter)return;const busy=novelBusy()||novels.saving,focused=document.activeElement;
  if(!force&&$('#novel-sentence-rows').contains(focused)&&focused.matches('textarea,select')){renderNovelButtons();return;}
  const items=filteredNovelRows(),pages=Math.max(1,Math.ceil(items.length/20));novels.page=Math.min(novels.page,pages-1);const visible=items.slice(novels.page*20,(novels.page+1)*20);
  const key=JSON.stringify([novels.chapter.id,visible,novels.chapter.audios,state.voices,busy,novels.page,I18N.locale()]);
  $('#novel-sentence-count').textContent=tr('共 {0} 句 · 本页显示 {1} 句',items.length,visible.length);$('#novel-page').textContent=`${novels.page+1} / ${pages}`;$('#novel-prev').disabled=!novels.page;$('#novel-next').disabled=novels.page>=pages-1;
  if(key===novels.rowKey)return;novels.rowKey=key;
  $('#novel-sentence-rows').innerHTML=visible.map(({row,index})=>{
    const audio=novels.chapter.audios[row.id];const stale=audio&&(audio.config.text!==row.text||audio.config.voice_id!==(row.voice_id||$('#novel-voice').value)||audio.config.language!==$('#novel-language').value||(audio.config.emotion||'inherit')!==(row.emotion||'inherit'));return `<tr><td><b>${index+1}</b><small>${esc(tr('第 {0} 段',row.paragraph+1))}</small></td><td><textarea dir="auto" rows="3" data-novel-field="text" data-index="${index}" aria-label="${esc(tr('第 {0} 句朗读文字',index+1))}">${esc(row.text)}</textarea></td><td><label><span>${esc(tr('音色'))}</span><select data-novel-field="voice_id" data-index="${index}" aria-label="${esc(tr('第 {0} 句音色',index+1))}">${novelVoiceOptions(row.voice_id,true)}</select></label><label><span>${esc(tr('表达情绪'))}</span><select data-novel-field="emotion" data-index="${index}" aria-label="${esc(tr('第 {0} 句情绪',index+1))}">${emotionOptions(row.emotion||'inherit')}</select></label></td><td>${audio?`<audio controls preload="metadata" src="${novelAudioUrl(audio.file)}"></audio><small>${Number(audio.duration).toFixed(1)} ${esc(tr('秒'))} · ${esc(audio.voice_name)}${stale?' · '+esc(tr('旧版本')):''}</small>`:`<small>${esc(tr(novelBusy()?'正在生成…':'尚未生成'))}</small>`}<button data-novel-generate="${row.id}">${esc(tr(audio?'重新生成本句':'生成本句'))}</button></td></tr>`;
  }).join('')||`<tr><td colspan="4" class="empty-inline">${esc(tr('尚无句子，请先自动分句或调整搜索条件。'))}</td></tr>`;
  $$('#novel-sentence-rows textarea').forEach(growTextarea);renderNovelButtons();
}
$('#novel-sentence-rows').oninput=e=>{
  const field=e.target.dataset.novelField,row=novels.rows[Number(e.target.dataset.index)];if(!field||!row||novelBusy())return;
  if(field==='emotion')setRowExpression(row,e.target.value,'medium');else row[field]=e.target.value;
  if(e.target.tagName==='TEXTAREA')growTextarea(e.target);novels.rowKey=null;novelDirty();
};
$('#novel-sentence-rows').onclick=e=>{const button=e.target.closest('[data-novel-generate]');if(button)guard(()=>generateNovel([button.dataset.novelGenerate]));};
$('#novel-sentence-search').oninput=()=>{novels.page=0;renderNovelRows(true);};$('#novel-prev').onclick=()=>{novels.page--;renderNovelRows(true);};$('#novel-next').onclick=()=>{novels.page++;renderNovelRows(true);};
async function generateNovel(sentenceIds=null,all=false){
  if(novelBusy())return;const book=novels.book.id,cid=novels.chapter?.id,version=novels.version;
  if(novels.dirty)await saveNovelChapter();if(book!==novels.book?.id||version!==novels.version||novels.dirty)throw Error(tr('仍有新输入的修改未保存，请保存后重试'));
  if(sentenceIds&&!sentenceIds.every(id=>novels.rows.some(row=>row.id===id)))throw Error(tr('句子已经重新分段，请重新选择后生成'));
  await api('/api/audiobooks/'+book+'/generate',json('POST',{chapter_ids:all?null:[cid],sentence_ids:sentenceIds}));await refreshNovelBook();
}
$('#novel-generate').onclick=()=>guard(()=>generateNovel());$('#novel-generate-all').onclick=()=>guard(()=>generateNovel(null,true));
$('#novel-export').onclick=()=>guard(async()=>{if(novels.dirty)await saveNovelChapter();if(novels.dirty)throw Error(tr('仍有新输入的修改未保存，请保存后重试'));await api('/api/audiobooks/'+novels.book.id+'/export',json('POST',{format:$('#novel-export-format').value}));await refreshNovelBook();});
$('#novel-cancel-job').onclick=()=>guard(async()=>{await api('/api/audiobooks/'+novels.book.id+'/cancel',{method:'POST'});await refreshNovelBook();});
function renderNovelAudio(){
  if(!novels.chapter)return;const chapter=novels.chapter,player=$('#novel-chapter-player'),name=chapter.outputs.wav;
  $('#novel-chapter-downloads').innerHTML=Object.entries(chapter.outputs).map(([format,file])=>`<a class="novel-download" download href="${novelAudioUrl(file,chapter.id,true)}">${esc(tr('下载 {0}',format.toUpperCase()))}</a>`).join('');
  player.hidden=!name;$('#novel-audio-empty').hidden=!!name;
  if(name){const url=novelAudioUrl(name);if(player.getAttribute('src')!==url)player.src=url;}else if(player.getAttribute('src')){player.pause();player.removeAttribute('src');player.load();}
}
async function refreshNovelBook(){
  if(!novels.book)return;const id=novels.book.id,version=novels.version,request=novels.bookRequest=(novels.bookRequest||0)+1,result=await api('/api/audiobooks/'+id);if(id!==novels.book?.id||version!==novels.version||request!==novels.bookRequest)return;
  novels.book=result;const current=result.chapters.find(chapter=>chapter.id===novels.chapter?.id);
  if(current&&!novelBusy()&&!novels.dirty&&!novels.saving&&current.updated!==novels.chapter.updated){
    const generation=novels.generation,chapter=await api(novelChapterUrl());if(id!==novels.book?.id||version!==novels.version||request!==novels.bookRequest)return;if(novels.dirty||generation!==novels.generation){renderNovels();return;}novels.chapter=chapter;novels.rows=clone(chapter.sentences);novels.rowKey=null;renderNovelAudio();
  }
  renderNovels();
}
setInterval(()=>{if(state.section!=='novels'||novels.polling)return;novels.polling=true;(novels.book?refreshNovelBook():refreshNovels()).catch(error=>{$('#novel-job-error').textContent=error.message;}).finally(()=>novels.polling=false);},1500);
window.addEventListener('beforeunload',e=>{if(novels.dirty){e.preventDefault();e.returnValue='';}});
$('#ui-language').addEventListener('change',()=>{novels.listKey=null;novels.rowKey=null;novels.chapterListKey=null;novels.voiceKey=null;renderNovels();renderNovelVoices();showSection(state.section);$('#novel-draft-status').textContent=tr(novels.dirty?'有未保存的修改':'章节已保存');});

$('#novel-open-library').onclick=()=>guard(async()=>{pauseMedia();showSection('library');await refreshLibrary();});

function pauseNovelAudio(){$$('#novels-page audio').forEach(audio=>audio.pause());}
for(const id of ['menu-projects','menu-library','open-settings','go-home'])$('#'+id).addEventListener('click',pauseNovelAudio);
