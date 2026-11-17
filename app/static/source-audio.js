"use strict";
const sourceCue={row:null,project:null,version:null,url:'',generation:0};
const sourceCuePlayer=$('#source-cue-player'),sourceCueAudio=$('#source-cue-audio');
function updateSourceCueMarks(){
  for(const button of $$('[data-source-audio]')){
    const playing=sourceCue.project===state.id&&Number(button.dataset.sourceAudio)===sourceCue.row?.id&&!sourceCueAudio.paused;
    button.removeAttribute('aria-pressed');button.dataset.sourcePlaying=String(playing);button.setAttribute('aria-label',tr(playing?'重听第 {0} 句原声':'听第 {0} 句原声',button.dataset.sourceAudio));
    button.closest('tr')?.classList.toggle('source-cue-active',playing);
    button.closest('tr')?.classList.toggle('source-cue-current',sourceCue.project===state.id&&Number(button.dataset.sourceAudio)===sourceCue.row?.id);
  }
}
function stopSourceCue(){
  sourceCue.generation++;sourceCueAudio.pause();sourceCueAudio.removeAttribute('src');sourceCueAudio.load();
  sourceCue.row=null;sourceCue.project=null;sourceCue.url='';sourceCuePlayer.hidden=true;
  if(sourceCuePlayer.parentElement!==document.body)document.body.append(sourceCuePlayer);
  updateSourceCueMarks();
}
function sourceCueTime(seconds){return ProjectWorkflow.preciseTime(seconds);}
function loadSourceCue(){
  if(!sourceCue.row||sourceCue.project!==state.id||sourceCue.version!==state.selectionVersion)return;
  const row=sourceCue.row,context=$('#source-cue-context').checked?2:0,duration=Number(state.project.media.duration);
  const start=Math.max(0,row.start-context),end=Math.min(duration,row.end+context);
  $('#source-cue-title').textContent=tr('第 {0} 句原声',row.id);$('#source-cue-time').textContent=tr('原片 {0}–{1}',sourceCueTime(start),sourceCueTime(end));
  $('#source-cue-status').textContent=tr('正在准备原声片段…');
  const query=new URLSearchParams({start:String(row.start),end:String(row.end),context:String(context)});
  const reference=window.SubtitleTools?.referenceId(row)??row.id;
  sourceCue.url=`/api/projects/${state.id}/source-audio/${reference}?${query}`;
  const generation=++sourceCue.generation;
  sourceCueAudio.pause();sourceCueAudio.src=sourceCue.url;sourceCueAudio.playbackRate=Number($('#source-cue-speed').value);
  sourceCueAudio.play().catch(error=>{
    if(sourceCue.generation!==generation||error.name==='AbortError')return;
    if(error.name==='NotAllowedError')$('#source-cue-status').textContent=tr('原声已准备，请点击播放器播放。');
  });
}
function playSourceCue(row){
  if(!state.project||!row)return;
  // Copy the current draft timings without saving, selecting a row or rebuilding editors.
  const sample={id:row.id,start:Number(row.start),end:Number(row.end)};
  if(!Number.isFinite(sample.start)||!Number.isFinite(sample.end)||sample.start<0||sample.end<=sample.start||sample.end>Number(state.project.media.duration)+.001)throw Error(tr('请先将本句起止时间调整到原片范围内。'));
  const dialog=document.querySelector('dialog[open]');
  ['source-video','result-video','editor-video','dub-audio','sentence-audio'].forEach(id=>$('#'+id).pause());
  sourceCue.row=sample;sourceCue.project=state.id;sourceCue.version=state.selectionVersion;
  const anchor=dialog?.querySelector('.editor-footer');if(anchor)anchor.after(sourceCuePlayer);else (dialog||document.body).append(sourceCuePlayer);sourceCuePlayer.hidden=false;
  $('#source-cue-context').checked=false;loadSourceCue();
}
document.addEventListener('click',event=>{
  const button=event.target.closest('[data-source-audio]');if(!button)return;
  const row=state.rows.find(row=>row.id===Number(button.dataset.sourceAudio));if(row)guard(()=>playSourceCue(row));
});
$('#listen-source-cue').onclick=()=>guard(()=>playSourceCue(currentRow()));
$('#source-cue-close').onclick=stopSourceCue;
$('#source-cue-replay').onclick=()=>guard(async()=>{if(!sourceCue.row)return;sourceCueAudio.currentTime=0;await sourceCueAudio.play();});
$('#source-cue-context').onchange=loadSourceCue;
$('#source-cue-loop').onchange=()=>{sourceCueAudio.loop=$('#source-cue-loop').checked;if(sourceCueAudio.loop)$('#source-cue-continuous').checked=false;};
$('#source-cue-continuous').onchange=()=>{if($('#source-cue-continuous').checked){$('#source-cue-loop').checked=false;sourceCueAudio.loop=false;}};
$('#source-cue-prev').onclick=()=>guard(()=>window.SubtitleTools?.navigate(-1));
$('#source-cue-next').onclick=()=>guard(()=>window.SubtitleTools?.navigate(1));
$('#source-cue-speed').onchange=()=>sourceCueAudio.playbackRate=Number($('#source-cue-speed').value);
sourceCueAudio.addEventListener('loadedmetadata',()=>{if(sourceCue.row)$('#source-cue-status').textContent=tr('原片声音，可边听边校对。');});
sourceCueAudio.addEventListener('play',()=>{if(sourceCue.row)$('#source-cue-status').textContent=tr('正在播放原声');updateSourceCueMarks();});
sourceCueAudio.addEventListener('pause',updateSourceCueMarks);
sourceCueAudio.addEventListener('ended',()=>{if(sourceCue.row&&$('#source-cue-continuous').checked&&window.SubtitleTools?.navigate(1))return;if(sourceCue.row)$('#source-cue-status').textContent=tr('本段播放完毕，可重播或加入前后文。');updateSourceCueMarks();});
sourceCueAudio.addEventListener('error',()=>{
  if(!sourceCue.row||!sourceCue.url)return;
  const generation=sourceCue.generation,url=sourceCue.url;
  $('#source-cue-status').textContent=tr('原声暂时无法播放，请重试或检查原片音轨。');
  guard(async()=>{
    try{const response=await fetch(url,{headers:{Range:'bytes=0-0'}});if(!response.ok){const value=await response.json();if(generation===sourceCue.generation&&typeof value.detail==='string')$('#source-cue-status').textContent=value.detail;}}
    catch{}finally{updateSourceCueMarks();}
  });
});
// Other media starting must not overlap the original speech being checked.
document.addEventListener('play',event=>{if(event.target!==sourceCueAudio&&event.target instanceof HTMLMediaElement)sourceCueAudio.pause();},true);
document.addEventListener('visibilitychange',()=>{if(document.hidden)sourceCueAudio.pause();});
const chooseBeforeSourceCue=chooseSentence;chooseSentence=function(index){stopSourceCue();return chooseBeforeSourceCue(index);};
const pauseBeforeSourceCue=pauseMedia;pauseMedia=function(){pauseBeforeSourceCue();stopSourceCue();};
const sectionBeforeSourceCue=showSection;showSection=function(section){if(section!=='projects'||!state.projectDetail)stopSourceCue();return sectionBeforeSourceCue(section);};
const tabBeforeSourceCue=switchTab;switchTab=function(name){if(name!==state.tab)stopSourceCue();return tabBeforeSourceCue(name);};
const listBeforeSourceCue=renderSentenceList;renderSentenceList=function(force=false){listBeforeSourceCue(force);updateSourceCueMarks();};
const renderBeforeSourceCue=render;render=function(full=false){renderBeforeSourceCue(full);if(sourceCue.project&&(sourceCue.project!==state.id||sourceCue.version!==state.selectionVersion))stopSourceCue();updateSourceCueMarks();};
for(const id of ['subtitle-detail-dialog','single-voice-editor','retranslation-dialog'])$('#'+id).addEventListener('close',()=>{if($('#'+id).contains(sourceCuePlayer))stopSourceCue();});
