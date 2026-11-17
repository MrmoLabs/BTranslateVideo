'use strict';

const projectDraft={timer:null,pending:null,offered:null,storageFailed:false};
const draftKey=id=>'btv-project-draft:'+id;
function projectProductLabel(p){return tr(ProjectWorkflow.productLabel(p));}
function scheduleProjectDraft(){
  if(!state.id||!state.project||!state.dirty)return;
  projectDraft.pending={version:1,id:state.id,revision:state.editorRevision||0,content_version:state.project.content_version,
    saved_at:Date.now(),rows:clone(state.rows)};
  clearTimeout(projectDraft.timer);projectDraft.timer=setTimeout(flushProjectDraft,350);
}
function flushProjectDraft(){
  clearTimeout(projectDraft.timer);const value=projectDraft.pending;if(!value)return;
  try{localStorage.setItem(draftKey(value.id),JSON.stringify(value));projectDraft.storageFailed=false;projectDraft.pending=null;}
  catch{projectDraft.storageFailed=true;}
  renderDraftStatus();
}
function clearProjectDraft(id){
  if(projectDraft.pending?.id===id){projectDraft.pending=null;clearTimeout(projectDraft.timer);}
  try{localStorage.removeItem(draftKey(id));}catch{}
  if(projectDraft.offered?.id===id){projectDraft.offered=null;$('#project-draft-banner').hidden=true;}
}
function readProjectDraft(id){try{return JSON.parse(localStorage.getItem(draftKey(id))||'null');}catch{return null;}}
function offerProjectDraft(){
  if(!state.project||state.dirty)return;
  const saved=readProjectDraft(state.id),status=ProjectWorkflow.draftState(saved,state.project);
  if(status==='saved'){clearProjectDraft(state.id);return;}
  projectDraft.offered=['recover','conflict'].includes(status)?saved:null;
  $('#project-draft-banner').hidden=!projectDraft.offered;
  if(!projectDraft.offered)return;
  $('#project-draft-message').textContent=tr(status==='conflict'?'发现未保存草稿，但项目版本已经改变。请先导出草稿副本比较，当前版本不会被覆盖。':'发现未保存的字幕草稿，可以恢复继续编辑。');
  $('#restore-project-draft').disabled=status!=='recover'||active(state.project);
}
function renderDraftStatus(){
  const node=$('#project-saved-at');if(!node||!state.project)return;
  node.textContent=projectDraft.storageFailed?tr('本地草稿保存失败，请及时保存或导出。'):state.dirty?tr('未保存 · 已启用本地草稿恢复'):tr('已保存 · {0}',new Date(state.project.updated).toLocaleTimeString(I18N.locale()));
}
function downloadText(text,name,type='text/plain;charset=utf-8'){
  const url=URL.createObjectURL(new Blob([text],{type})),link=document.createElement('a');
  link.href=url;link.download=name;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);
}
$('#restore-project-draft').onclick=()=>guard(async()=>{
  const saved=projectDraft.offered;if(!saved||ProjectWorkflow.draftState(saved,state.project)!=='recover')throw Error(tr('项目版本已经改变，请导出草稿副本比较。'));
  const id=state.id,version=state.selectionVersion,fresh=await api(`/api/projects/${id}`);
  if(id!==state.id||version!==state.selectionVersion)return;
  if(ProjectWorkflow.draftState(saved,fresh)!=='recover'){state.project=fresh;offerProjectDraft();throw Error(tr('项目版本已经改变，请导出草稿副本比较。'));}
  state.project=fresh;state.rows=clone(saved.rows);state.editorRevision=saved.revision;state.subtitleKey=null;state.boardKey=null;
  setDirty(true);$('#project-draft-banner').hidden=true;renderSentenceList(true);renderEditors();renderVoiceBoard(true);toast(tr('草稿已恢复，保存后才会写入项目。'));
});
$('#download-project-draft').onclick=()=>{const saved=projectDraft.offered;if(saved)downloadText(JSON.stringify(saved,null,2),'subtitle-draft-'+saved.id+'.json','application/json;charset=utf-8');};
$('#discard-project-draft').onclick=()=>guard(async()=>{
  if(await confirmChoice(tr('将放弃这份未保存草稿，保留项目中已经保存的版本。'),{title:tr('使用已保存版本'),proceed:tr('放弃本地草稿')})==='cancel')return;
  clearProjectDraft(state.id);
});
window.addEventListener('pagehide',flushProjectDraft);
document.addEventListener('visibilitychange',()=>{if(document.hidden)flushProjectDraft();});
document.addEventListener('keydown',event=>{
  if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='s'&&state.project&&state.section==='projects'){
    event.preventDefault();guard(async()=>{if(state.dirty)await saveSegments();if(state.audioDirty)await saveAudioOptions();});
  }
});

let importProfile=null,importProfileGeneration=0;
async function prepareImportExperience(){
  const generation=++importProfileGeneration;
  $('#import-profile-note').textContent=tr('正在读取识别配置…');$('#import-submit').disabled=true;
  try{
    const profile=await api('/api/asr-profile');if(generation!==importProfileGeneration)return;
    importProfile=profile;const select=$('#import-form').elements.language;
    for(const option of select.options)option.disabled=!profile.supported_languages.includes(option.value);
    select.value=profile.language;
    $('#import-profile-note').textContent=tr('识别模型：{0} · 可在项目的素材页调整。',profile.label);
  }finally{if(generation===importProfileGeneration)$('#import-submit').disabled=false;}
}

function renderProjectExperience(){
  const p=state.project;if(!p)return;
  const goal=ProjectWorkflow.goal(p),transcribe=goal==='transcribe',busy=active(p)||state.saving;
  document.body.classList.toggle('goal-transcribe',transcribe);document.body.classList.toggle('goal-translate',goal==='translate');
  $('#project-product-state').textContent=projectProductLabel({...p,segments:state.rows});
  if(state.goalEditorProject!==p.id||state.goalEditorSaved!==goal){state.goalEditorProject=p.id;state.goalEditorSaved=goal;$('#project-goal').value=goal;}
  $('#project-goal').disabled=busy;$('#apply-project-goal').disabled=busy||$('#project-goal').value===goal;
  for(const tab of $$('.tab'))tab.hidden=goal!=='dub'&&['voices','export'].includes(tab.dataset.tab);
  $('#run-all').textContent=tr(transcribe?'识别字幕':goal==='translate'?'识别并翻译':'一键完成');
  const subtitleTab=$('[data-tab="subtitles"]');subtitleTab.querySelector('span:not(.step-index) > span').textContent=tr(transcribe?'校对与导出':'翻译与校对');
  if(transcribe){
    $('#segment-count').textContent=tr('{0} 条原文字幕 · 校对后即可导出',state.rows.length);
    $('#step-subtitles-state').textContent=tr('字幕可导出');
    $('#workflow-need-translation').hidden=true;
    $('#source-actions-note').textContent=tr('识别完成后校对原文，即可导出 SRT 或 TXT。');
  }
  $('#panel-subtitles .section-toolbar h2').textContent=tr(transcribe?'校对原文字幕':'校对字幕与译文');
  $('#panel-source .tip p').textContent=tr(transcribe?'校对原文后直接导出字幕，无需翻译和配音。':'原文准备好后，进入“翻译与校对”设置目标语言并翻译。');
  $('#panel-source .tip button').textContent=tr(transcribe?'校对与导出字幕 →':'前往翻译与校对 →');
  $('#panel-source .source-reprocess p').textContent=tr('重新断句只复用已有识别结果；更换模型或热词后，请选择重新识别原声。两种操作都会先备份现有结果。');
  $('#run-all').title=tr(transcribe?'仅识别与断句，不生成配音':goal==='translate'?'仅识别与翻译，不生成配音':'按顺序自动完成，复用已有字幕和配音');
  $('#subtitle-language-summary').textContent=tr('目标语言：{0} · 修改语言',languageName(targetLanguage()));
  $('#use-source-script').hidden=transcribe||!p.language||p.language!==targetLanguage();
  $('#use-source-script').disabled=busy||!state.rows.length;
  $('#export-source-txt').disabled=!state.rows.length||!!state.projectUnavailable;
  const languageWrap=$('#subtitle-language-details');languageWrap.hidden=transcribe;
  for(const id of ['run-translate','run-optimize','export-translation-srt'])$('#'+id).hidden=transcribe;
  const translationImport=$('#chinese-srt').closest('label');translationImport.hidden=transcribe;
  const mode=$('#review-mode');for(const option of mode.options)option.disabled=transcribe&&!['source','asr'].includes(option.value);
  if(transcribe&&mode.selectedOptions[0]?.disabled)mode.value='asr';
  for(const option of $('#subtitle-filter').options)option.hidden=option.disabled=transcribe&&['missing','translated','checks'].includes(option.value);
  if(transcribe&&['missing','translated','checks'].includes($('#subtitle-filter').value)){$('#subtitle-filter').value='all';state.subtitleKey=null;}
  renderDraftStatus();renderASRProfile();
}
$('#project-goal').onchange=()=>{$('#apply-project-goal').disabled=$('#project-goal').value===ProjectWorkflow.goal(state.project);};
$('#apply-project-goal').onclick=()=>guard(async()=>{
  const goal=$('#project-goal').value;
  const token=await projectActions.prepare({audio:true});
  await api(`/api/projects/${token.id}/goal`,json('PUT',{goal,revision:state.editorRevision||0}));
  if(!projectActions.current(token))return;await refreshCurrent(true);switchTab(ProjectWorkflow.initialTab(state.project));
});
$('#use-source-script').onclick=()=>guard(async()=>{
  if(await confirmChoice(tr('将原文复制为配音稿，保留原文表达。已有译文与成片会先备份。需要整理为普通话书面语时，请先校对原文。'),{title:tr('使用原文作为配音稿'),proceed:tr('备份并使用原文')})==='cancel')return;
  const token=await projectActions.prepare({audio:true});
  await api(`/api/projects/${token.id}/copy-source`,json('POST',{revision:state.editorRevision||0}));
  if(projectActions.current(token))await refreshCurrent(true);
});
$('#export-source-txt').onclick=()=>guard(()=>{
  const valid=ProjectWorkflow.subtitleDownload(state.rows,'source',state.project);
  downloadText(state.rows.map(r=>r.text).join('\n')+'\n',valid.filename.replace(/\.srt$/,'.txt'));toast(tr('原文 TXT 已导出'));
});
$('#subtitle-density').onchange=()=>{document.body.classList.toggle('comfortable-subtitles',$('#subtitle-density').value==='comfortable');};

const asrEditor={project:null,signature:null,dirty:false,runtime:null,checking:false,saving:false};
function renderASRProfile(){
  const p=state.project,profile=p.asr_next;if(!profile)return;
  const same=asrEditor.project===p.id&&asrEditor.signature===profile.signature;
  const busy=active(p)||state.saving||asrEditor.saving;
  if(asrEditor.project!==p.id||!same&&!asrEditor.dirty){
    asrEditor.project=p.id;asrEditor.signature=profile.signature;asrEditor.dirty=false;asrEditor.runtime=null;
    $('#project-asr-provider').value=profile.provider;$('#project-asr-device').value=profile.device==='service'?'cuda':profile.device;
    $('#project-asr-language').value=p.language;$('#project-asr-hotwords').value=profile.hotwords;
  }
  $('#asr-next-summary').textContent=tr('下次识别：{0} · {1} · {2}',profile.label,languageName(p.language),profile.device==='cuda'?'GPU':profile.device==='cpu'?'CPU':tr('本地服务'));
  $('#asr-next-model').textContent=profile.model;
  const last=p.asr_last;
  const lastLabel=last?({fun_asr:'Fun-ASR-Nano',faster_whisper:'faster-whisper',openai:tr('本地兼容识别接口')})[last.provider]||last.provider:'';
  $('#asr-last-summary').textContent=last?tr('上次识别：{0} · {1}',lastLabel,(last.model||tr('历史模型未记录')).split(/[\\/]/).pop()):tr('已有字幕的识别来源未记录');
  if(!p.segments.length&&!last)$('#asr-last-summary').textContent=tr('尚未生成识别结果');
  const provider=$('#project-asr-provider').value;
  $('#project-asr-hotwords').closest('label').hidden=provider!=='fun_asr';
  $('#project-asr-device').closest('label').hidden=provider==='openai';
  for(const option of $('#project-asr-language').options)option.disabled=provider==='fun_asr'&&!['','zh','en','ja'].includes(option.value);
  $('#save-project-asr').disabled=busy||!asrEditor.dirty;
  for(const id of ['project-asr-provider','project-asr-language','project-asr-device','project-asr-hotwords'])$('#'+id).disabled=busy;
  $('#check-project-asr-runtime').disabled=busy||asrEditor.dirty||asrEditor.checking;
  $('#check-project-asr').disabled=busy||asrEditor.dirty;
  $('#run-retranscribe').disabled=busy||asrEditor.dirty;
  const inference=p.asr_check?.signature===profile.signature?p.asr_check:p.asr_last?.signature===profile.signature?p.asr_last:null;
  $('#asr-verified-state').textContent=tr(inference?'最近推理已通过 · 模型按任务加载':asrEditor.runtime?.ok?'运行环境可用 · 尚未验证推理':'文件检查与运行环境检查不代表推理通过');
  $('#asr-test-result').textContent=p.asr_check?.signature===profile.signature?p.asr_check.text:'';
}
for(const id of ['project-asr-provider','project-asr-language','project-asr-device','project-asr-hotwords'])$('#'+id).addEventListener('input',()=>{
  asrEditor.dirty=true;
  if(id==='project-asr-provider'&&$('#project-asr-provider').value==='fun_asr')$('#project-asr-language').value='zh';
  renderASRProfile();
});
$('#save-project-asr').onclick=()=>guard(async()=>{
  if(asrEditor.saving)return;
  const payload={provider:$('#project-asr-provider').value,language:$('#project-asr-language').value,device:$('#project-asr-device').value,hotwords:$('#project-asr-hotwords').value};
  asrEditor.saving=true;renderASRProfile();
  try{
    const token=await projectActions.prepare();
    await api(`/api/projects/${token.id}/asr-options`,json('PUT',{...payload,revision:state.editorRevision||0}));
    if(projectActions.current(token)){asrEditor.dirty=false;asrEditor.signature=null;await refreshCurrent(true);toast(tr('本项目识别设置已保存，已有字幕保持不变。'));}
  }finally{asrEditor.saving=false;renderASRProfile();}
});
$('#check-project-asr-runtime').onclick=()=>guard(async()=>{
  if(asrEditor.checking)return;const id=state.id,signature=state.project.asr_next.signature;asrEditor.checking=true;renderASRProfile();
  try{const value=await api(`/api/projects/${id}/asr-runtime`,{method:'POST'});if(state.id===id&&state.project.asr_next.signature===signature){asrEditor.runtime=value;$('#asr-runtime-result').textContent=tr(value.message);}}
  finally{asrEditor.checking=false;renderASRProfile();}
});
$('#check-project-asr').onclick=()=>guard(()=>run('check_asr'));
$('#run-retranscribe').onclick=()=>guard(async()=>{
  if(state.rows.length&&await confirmChoice(tr('将用当前项目识别设置重新识别原声。现有字幕、译文、配音和成片会先备份，新结果需要重新校对。'),{title:tr('重新识别原声'),proceed:tr('备份并重新识别')})==='cancel')return;
  await run('retranscribe');
});

if(state.project)renderProjectExperience();
