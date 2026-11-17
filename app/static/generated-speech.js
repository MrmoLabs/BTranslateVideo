'use strict';
const generatedCue={key:'',generation:0,timer:null,result:null};
function stopGeneratedCue(){
  clearTimeout(generatedCue.timer);generatedCue.generation++;generatedCue.key='';generatedCue.result=null;
  const audio=$('#generated-cue-audio');audio.pause();audio.removeAttribute('src');audio.load();
  $('#generated-cue-play').disabled=true;
}
function renderGeneratedCue(){
  if(!$('#single-voice-editor').open||!state.project||state.section!=='projects'){if(generatedCue.key)stopGeneratedCue();return;}
  const row=currentRow();if(!row)return;
  const project=state.project;
  const key=JSON.stringify([state.id,state.selectionVersion,state.editorRevision,row.id,row.zh,row.speaker,row.emotion,row.emotion_strength,row.start,row.end,project.outputs.audio,project.dub_signature,project.audition?.file,project.references,project.target_language,project.status]);
  if(generatedCue.key===key)return;
  stopGeneratedCue();generatedCue.key=key;
  const audio=$('#generated-cue-audio');audio.hidden=true;
  $('#generated-cue-state').textContent=tr('正在查找已生成配音…');$('#generated-cue-note').textContent=tr('只读取已有音频，不加载配音模型，也不保存当前修改。');
  const token=projectActions.context(),generation=generatedCue.generation,draft=clone(row),revision=state.editorRevision;
  generatedCue.timer=setTimeout(async()=>{
    try{
      const result=await api(`/api/projects/${token.id}/generated-speech`,json('POST',{segment:draft,revision}));
      if(generation!==generatedCue.generation||!projectActions.current(token)||!$('#single-voice-editor').open)return;
      generatedCue.result=result;
      if(!result.available){$('#generated-cue-state').textContent=tr('本句尚无可播放的已生成配音');$('#generated-cue-note').textContent=tr('可以使用下方“生成本句试听”；整片或本句生成后，这里会直接提供播放。');return;}
      const same=expressionSignature(draft)===expressionSignature(result.segment)&&result.settings_current&&result.target_language===targetLanguage()&&
        (!result.kind.startsWith('timeline')||(draft.start===result.segment.start&&draft.end===result.segment.end));
      const labels={timeline:tr('整片配音中的本句'),timeline_window:tr('整片配音的字幕时间窗口'),cache:tr('已生成的原速语音缓存'),previous_cache:tr('上次保存的逐句配音'),audition:tr('已生成的单句试听')};
      $('#generated-cue-state').textContent=tr('第 {0} 句 · {1}',draft.id,labels[result.kind])+' · '+(same?tr('与当前配音稿一致'):tr('已有音频是之前版本'));
      $('#generated-cue-note').textContent=(result.kind==='timeline'?tr('直接播放整片中已对齐的人声片段；重叠对白可能包含其他角色。'):result.kind==='timeline_window'?tr('旧项目没有逐句对齐记录，按原字幕时间截取；句尾可能位于相邻窗口。'):tr('播放已经保存的原速人声，不含背景音和整片时间对齐。'))+
        (same?'':' '+tr('文字、角色、情绪或声音设置已不同；仍可听旧音频，确认新效果需重新生成。'));
      audio.src=result.url;audio.hidden=false;audio.load();$('#generated-cue-play').disabled=false;
    }catch(error){
      if(generation!==generatedCue.generation||!projectActions.current(token))return;
      $('#generated-cue-state').textContent=tr('读取已生成配音失败');$('#generated-cue-note').textContent=error.message;
    }
  },180);
}
$('#generated-cue-play').onclick=()=>guard(async()=>{
  const audio=$('#generated-cue-audio');if(!audio.getAttribute('src'))return;
  audio.currentTime=0;await audio.play();
});
$('#generated-cue-audio').addEventListener('play',()=>{
  ['sentence-audio','sentence-video','dub-audio','source-cue-audio','source-video','result-video'].forEach(id=>$('#'+id)?.pause());
});
for(const id of ['sentence-audio','sentence-video','dub-audio','source-cue-audio'])$('#'+id)?.addEventListener('play',()=>$('#generated-cue-audio').pause());
$('#generated-cue-audio').addEventListener('error',()=>{
  if(!$('#generated-cue-audio').getAttribute('src'))return;
  $('#generated-cue-state').textContent=tr('音频加载失败，可关闭后重新打开本句');$('#generated-cue-play').disabled=true;
});
new MutationObserver(renderGeneratedCue).observe($('#single-voice-editor'),{attributes:true,attributeFilter:['open']});
$('#single-voice-editor').addEventListener('close',stopGeneratedCue);
document.addEventListener('visibilitychange',()=>{if(document.hidden)$('#generated-cue-audio').pause();});
