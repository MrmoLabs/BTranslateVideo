"use strict";
// Project actions keep server-side results separate until the user adopts them.
async function saveProjectDraftsForAction(){return projectActions.prepare();}
$('#new-project-role').onclick=()=>{
  $('#project-role-form').reset();$('#project-role-error').textContent='';openCueDialog($('#project-role-dialog'));
};
$('#project-role-form').onsubmit=e=>{e.preventDefault();guard(async()=>{
  if(state.creatingRole)return;state.creatingRole=true;$('#create-project-role').disabled=true;$('#project-role-error').textContent='';
  try{
    const {id,version}=await saveProjectDraftsForAction();
    await api(`/api/projects/${id}/roles`,json('POST',{name:$('#project-role-name').value.trim(),revision:state.editorRevision}));
    if(id!==state.id||version!==state.selectionVersion)return;
    state.voiceKey=null;state.boardKey=null;await refreshCurrent(true);$('#project-role-dialog').close();$('#voice-references').open=true;
    toast(tr('角色已创建，可先绑定音色，再在台词表中分配角色。'));
  }catch(error){$('#project-role-error').textContent=error.message;}
  finally{state.creatingRole=false;$('#create-project-role').disabled=false;renderEditingControls();}
});};
$('#project-role-dialog').oncancel=e=>{if(state.creatingRole)e.preventDefault();};

async function startRetranslation(segmentId,mode='translate'){
  if(!['asr','source'].includes(mode))requireAppliedTarget();
  const existing=state.project.retranslation;
  if(!state.dirty&&existing?.segment_id===segmentId&&existing.status==='ready'&&existing.revision===state.editorRevision&&(existing.mode||'translate')===mode){showRetranslation();return;}
  const {id,version}=await saveProjectDraftsForAction();
  const response=await api(`/api/projects/${id}/retranslate`,json('POST',{segment_id:segmentId,revision:state.editorRevision,mode}));
  if(id!==state.id||version!==state.selectionVersion)return;
  state.retranslationPending={project:id,request:response.request_id};
  await refreshCurrent(false);toast(tr('正在生成本句候选，现有原文和译文保留，生成后可对比采用。'));
}
function showRetranslation(){
  const result=state.project?.retranslation;if(!result||result.status!=='ready')return;
  state.retranslationDialog={project:state.id,request:result.request_id};
  const mode=result.mode||'translate',sourceMode=['asr','source','both'].includes(mode);
  const names={translate:tr('重新翻译'),asr:tr('重新识别原文'),source:tr('润色原文'),translation:tr('润色译文'),both:tr('联合校对'),shorten:tr('按实测时长生成短稿')};
  $('#retranslation-title').textContent=tr('第 {0} 句 · {1}',result.segment_id,names[mode]);
  $('#retranslation-source-compare').hidden=!sourceMode;$('#retranslation-source').hidden=sourceMode;
  $('#retranslation-original-source').textContent=result.source;$('#retranslation-new-source').textContent=result.after_source??result.source;
  $('#retranslation-note').textContent=mode==='asr'?tr('本句已重新听音识别，结果仍可能有误，请对照原声确认。采用变更后的原文会清空旧译文，时间与角色保持不变。'):sourceMode?tr('建议尚未替换当前内容。原文润色仅依据上下文推断，请听原声核对；单独采用变更后的原文会清空旧译文。'):tr('新译文尚未替换当前内容，采用时只修改这一句。');
  $('#retranslation-listen').dataset.sourceAudio=String(result.segment_id);$('#retranslation-listen').setAttribute('aria-label',tr('听第 {0} 句原声',result.segment_id));
  $('#retranslation-reason').textContent=result.reason||'';
  $('#apply-retranslation').textContent=['asr','source'].includes(mode)&&result.after_source!==result.source?tr('采用新原文并清空旧译文'):tr('采用本句建议');
  $('#retranslation-source').textContent=result.source;$('#retranslation-before').textContent=result.before||tr('尚无译文');$('#retranslation-after').textContent=result.after||tr('采用新原文后需要重新翻译');
  $('#retranslation-checks').textContent=(result.checks||[]).map(translationCheckText).join('\n');$('#retranslation-error').textContent='';
  if(!$('#retranslation-dialog').open)openCueDialog($('#retranslation-dialog'));
}
$('#apply-retranslation').onclick=()=>guard(async()=>{
  const context=state.retranslationDialog;if(!context||state.applyingRetranslation)return;
  state.applyingRetranslation=true;$('#apply-retranslation').disabled=true;
  try{
    const {id,version}=await saveProjectDraftsForAction();
    if(id!==context.project)throw Error(tr('项目已切换，请重新操作'));
    await api(`/api/projects/${id}/retranslate/apply`,json('POST',{request_id:context.request,revision:state.editorRevision}));
    if(id!==state.id||version!==state.selectionVersion)return;
    state.outputRenderKey=null;state.subtitleKey=null;state.boardKey=null;
    await refreshCurrent(true);$('#retranslation-dialog').close();toast(tr('已采用本句校对建议，其他句子保持不变。'));
  }catch(error){$('#retranslation-error').textContent=error.message;}
  finally{state.applyingRetranslation=false;$('#apply-retranslation').disabled=false;}
});
$('#retranslation-dialog').oncancel=e=>{if(state.applyingRetranslation)e.preventDefault();};

document.addEventListener('click',event=>{
  const retry=event.target.closest('[data-retranslate]');if(retry&&!retry.disabled){guard(()=>startRetranslation(Number(retry.dataset.retranslate),retry.dataset.refineMode||'translate'));return;}
  const sample=event.target.closest('[data-auto-reference]');if(!sample||sample.disabled)return;
  guard(async()=>{
    const {id,version}=await saveProjectDraftsForAction(),speaker=sample.dataset.autoReference;
    if(state.project.references[speaker]&&await confirmChoice(tr('将从已分配台词重新提取参考音；当前音色与成片会先备份。'),{title:tr('从原片取样'),proceed:tr('提取并替换')})==='cancel')return;
    if(id!==state.id||version!==state.selectionVersion)return;
    const response=await api(`/api/projects/${id}/reference/${speaker}/auto`,json('POST',{revision:state.editorRevision}));
    if(id!==state.id||version!==state.selectionVersion)return;
    state.referencePending={project:id,request:response.request_id};await refreshCurrent(false);toast(tr('正在从角色台词提取原片声音，完成后可在卡片试听。'));
  });
});

// Unlock this same media element in the Generate click. The silent WAV is only
// an activation gesture; never replay an old request or start after navigation.
let auditionSilence;
function armAuditionPlayback(video=false){
  const audio=$(video?'#sentence-video':'#sentence-audio');
  if(!auditionSilence){
    const bytes=new Uint8Array(44+480*2),view=new DataView(bytes.buffer);
    const word=(offset,value)=>[...value].forEach((c,i)=>bytes[offset+i]=c.charCodeAt(0));
    word(0,'RIFF');view.setUint32(4,bytes.length-8,true);word(8,'WAVE');word(12,'fmt ');view.setUint32(16,16,true);view.setUint16(20,1,true);view.setUint16(22,1,true);view.setUint32(24,24000,true);view.setUint32(28,48000,true);view.setUint16(32,2,true);view.setUint16(34,16,true);word(36,'data');view.setUint32(40,960,true);
    auditionSilence=URL.createObjectURL(new Blob([bytes],{type:'audio/wav'}));
  }
  audio.pause();audio.src=auditionSilence;
  const activation=audio.play();activation?.then(()=>{if(audio.getAttribute('src')===auditionSilence)audio.pause();}).catch(()=>{});
}
function videoAuditionSignature(row){return JSON.stringify([expressionSignature(row),row?.start,row?.end]);}
function auditionVideoClock(seconds){const centis=Math.round(Math.max(0,seconds)*100);return Math.floor(centis/6000)+':'+String(Math.floor(centis%6000/100)).padStart(2,'0')+'.'+String(centis%100).padStart(2,'0');}
function setAuditionCaptions(){for(const track of $('#sentence-video').textTracks)track.mode=$('#audition-video-captions').checked?'showing':'hidden';}
function renderAuditionVideo(){
  const sample=state.project?.audition,row=currentRow(),video=$('#sentence-video'),visible=!!sample?.video&&sample.segment.id===row?.id;
  $('#audition-video-result').hidden=!visible;$('#single-voice-editor .expression-card').classList.toggle('has-video-preview',visible);
  if(!visible){video.pause();return;}
  $('#audition-result').hidden=true;
  const same=videoAuditionSignature(row)===videoAuditionSignature(sample.segment);
  $('#audition-video-state').textContent=same?tr('与当前配音稿一致'):tr('预览的是之前版本');
  $('#audition-video-time').textContent=tr('原片 {0}–{1}',auditionVideoClock(sample.start),auditionVideoClock(sample.end));
  $('#audition-video-speed').textContent=sample.speed>1.005?tr('配音 {0} 倍速',sample.speed.toFixed(2)):tr('配音保持原速');
  $('#audition-video-note').textContent=same?tr('短配音保持原速，结束后留白；可重播核对画面与节奏。'):tr('配音稿、角色、情绪或时间已修改，请重新生成视频预览。');
  const url=fileUrl(sample.video);
  if(video.getAttribute('src')!==url){
    video.pause();video.replaceChildren();video.src=url;
    const track=document.createElement('track');track.kind='subtitles';track.label=languageName(targetLanguage(state.project));track.srclang=targetLanguage(state.project);track.src=fileUrl(sample.captions);track.default=$('#audition-video-captions').checked;track.addEventListener('load',setAuditionCaptions);video.append(track);video.load();
  }
}
$('#audition-video-captions').onchange=setAuditionCaptions;
$('#sentence-video').addEventListener('loadedmetadata',setAuditionCaptions);
$('#sentence-video').addEventListener('play',()=>$('#sentence-audio').pause());
$('#sentence-audio').addEventListener('play',()=>$('#sentence-video').pause());
$('#run-video-audition').onclick=()=>guard(()=>runAudition(true));

const auditionBeforeAutoPlay=runAudition;
runAudition=async function(video=false){
  if(state.auditionSubmitting)return;requireAppliedTarget();
  const row=currentRow();if(!row?.zh?.trim())throw Error(tr('先填写这句的译文配音稿'));
  state.auditionSubmitting=true;state.auditionIntent={project:state.id,version:state.selectionVersion,signature:video?videoAuditionSignature(row):expressionSignature(row),video};
  state.auditionPending=null;$('#sentence-video').pause();armAuditionPlayback(video);renderEditingControls();
  try{return await auditionBeforeAutoPlay(video);}
  catch(error){state.auditionIntent=null;state.auditionPending=null;throw error;}
  finally{state.auditionSubmitting=false;renderEditingControls();}
};
const renderAuditionBeforeAutoPlay=renderAudition;
renderAudition=function(){
  renderAuditionBeforeAutoPlay();renderAuditionVideo();
  const intent=state.auditionIntent,pending=state.auditionPending,sample=state.project?.audition;
  if(!intent||!pending||!sample||sample.request_id!==pending.request)return;
  const allowed=intent.project===state.id&&intent.version===state.selectionVersion&&(intent.video?videoAuditionSignature(currentRow()):expressionSignature(currentRow()))===intent.signature&&(intent.video?videoAuditionSignature(sample.segment):expressionSignature(sample.segment))===intent.signature&&(!intent.video||!!sample.video)&&$('#single-voice-editor').open&&state.section==='projects'&&!document.hidden;
  state.auditionIntent=null;state.auditionPending=null;
  if(!allowed)return;
  const audio=$(intent.video?'#sentence-video':'#sentence-audio');audio.currentTime=0;
  audio.play().catch(error=>{if(error.name==='NotAllowedError')toast(tr('试听已生成，浏览器未允许自动播放，请点击播放器播放。'));});
};
const chooseSentenceBeforeAutoPlay=chooseSentence;
chooseSentence=function(index){$('#sentence-video').pause();state.auditionIntent=null;state.auditionPending=null;return chooseSentenceBeforeAutoPlay(index);};
$('#single-voice-editor').addEventListener('close',()=>{$('#sentence-video').pause();state.auditionIntent=null;state.auditionPending=null;});
document.addEventListener('visibilitychange',()=>{if(document.hidden){$('#sentence-video').pause();state.auditionIntent=null;state.auditionPending=null;$('#sentence-audio').pause();}});
const showSectionBeforeAutoPlay=showSection;
showSection=function(section){if(section!=='projects'){$('#sentence-video').pause();state.auditionIntent=null;state.auditionPending=null;$('#sentence-audio').pause();}return showSectionBeforeAutoPlay(section);};

const controlsBeforeActions=renderEditingControls;
renderEditingControls=function(){controlsBeforeActions();$('#run-video-audition').disabled=$('#run-audition').disabled||state.auditionSubmitting||!state.project?.media?.video;$('#run-video-audition').setAttribute('aria-busy',String(active(state.project)&&!!state.project?.audition_request?.video&&state.project?.stage==='audition'));$('#run-video-audition').textContent=active(state.project)&&state.project?.stage==='audition'&&state.project?.audition_request?.video?tr('正在生成视频预览…'):tr('生成本句视频预览');if(state.auditionSubmitting)$('#run-audition').disabled=true;$('#new-project-role').disabled=active(state.project)||state.saving||state.creatingRole;};
const renderBeforeActions=render;
render=function(full=false){
  renderBeforeActions(full);
  const result=state.project?.retranslation,pending=state.retranslationPending;
  if(result?.status==='ready'&&!active(state.project)&&pending?.project===state.id&&pending.request===result.request_id){
    state.retranslationPending=null;
    if(state.section==='projects'&&state.tab==='subtitles'&&!document.querySelector('dialog[open]')&&!document.hidden)showRetranslation();
    else toast(tr('本句新译文已生成，可在字幕行查看并采用。'));
  }
  const reference=state.project?.reference_result,referencePending=state.referencePending;
  if(reference&&referencePending?.project===state.id&&referencePending.request===reference.request_id){state.referencePending=null;toast(tr('参考音已提取，请在角色卡片试听确认。'));}
};
for(const id of ['project-role-dialog','retranslation-dialog'])$('#'+id).addEventListener('close',()=>{
  const position=$('#'+id)._pagePosition;if(!position)return;
  requestAnimationFrame(()=>{if(position.opener?.isConnected)position.opener.focus({preventScroll:true});window.scrollTo({left:position.x,top:position.y,behavior:'instant'});});
});
