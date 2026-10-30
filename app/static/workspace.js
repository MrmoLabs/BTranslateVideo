'use strict';
Object.assign(state,{section:'projects',voices:[],voiceChecked:new Set(),voicePage:0,libraryRequest:0,logKind:'activity',logLines:500});

function showSection(section){
  const view=section==='projects'&&state.projectDetail&&state.project?'project:'+state.id:section;
  const changed=state.viewKey!==view;
  state.viewScroll??={};if(changed&&state.viewKey)state.viewScroll[state.viewKey]=window.scrollY;
  state.viewKey=view;state.section=section;
  $('#voice-library-page').hidden=section!=='library';
  $('#novels-page').hidden=section!=='novels';
  $('#settings-page').hidden=section!=='settings';
  $('#workspace').hidden=section!=='projects'||!state.projectDetail||!state.project;
  $('#welcome').hidden=section!=='projects'||!!(state.projectDetail&&state.project);
  for(const [id,name] of [['menu-projects','projects'],['menu-library','library'],['menu-novels','novels'],['open-settings','settings']]){
    $('#'+id).classList.toggle('selected',section===name);$('#'+id).setAttribute('aria-current',section===name?'page':'false');
  }
  $('#page-name').textContent=tr(section==='novels'?'小说有声书':section==='library'?'音色库':section==='settings'?'设置与模型':state.projectDetail&&state.project?'视频译制':'我的项目');
  if(section==='library')renderLibrary();
  if(changed)window.scrollTo({top:state.viewScroll[view]||0,left:0,behavior:'instant'});
}
function showProjects(){pauseMedia();state.projectDetail=false;showSection('projects');renderProjectList();}
$('#menu-projects').onclick=showProjects;
$('#back-projects').onclick=showProjects;
$('#menu-library').onclick=()=>guard(async()=>{pauseMedia();showSection('library');await refreshLibrary();});
$('#go-home').onclick=showProjects;
const selectProjectBeforeNavigation=selectProject;
selectProject=async function(id){await selectProjectBeforeNavigation(id);if(state.id===id){state.projectDetail=true;showSection('projects');}};
const originalRender=render;
render=function(full=false){originalRender(full);showSection(state.section);renderVoiceBoard();$('#log-status-badge').textContent=tr(labels[state.project?.status]||'等待开始');};
const originalEditors=renderEditors;
renderEditors=function(){originalEditors();renderVoiceBoard();};
const originalVoices=renderVoices;
renderVoices=function(){originalVoices();renderRolePresets();};

async function refreshLibrary(){
  const version=++state.libraryRequest,archived=$('#show-archived').checked;
  const data=await api('/api/voices?archived='+archived);
  if(version!==state.libraryRequest)return;
  state.libraryItems=data;
  if(!archived)state.voices=data;
  else state.voices=await api('/api/voices');
  state.libraryKey=null;renderLibrary();renderRolePresets();if(typeof renderNovelVoices==='function')renderNovelVoices();
}
function renderLibrary(){
  const q=$('#voice-search').value.trim().toLowerCase(),items=(state.libraryItems||state.voices).filter(v=>(v.name+' '+v.description).toLowerCase().includes(q));
  const key=JSON.stringify([items,q,I18N.locale()]);if(key===state.libraryKey)return;state.libraryKey=key;
  $('#library-count').textContent=tr('{0} 个音色',items.length);
  $('#voice-library-list').innerHTML=items.map(v=>`<article class="card voice-preset"><div class="card-heading"><h3>${esc(v.name)}</h3><span class="badge">${Number(v.duration).toFixed(1)} ${esc(tr('秒'))}</span></div><p class="preset-description">${esc(v.description||tr('暂无说明'))}</p><audio controls preload="none" src="/api/voices/${v.id}/audio?v=${encodeURIComponent(v.updated)}"></audio><div class="preset-actions"><button data-edit-voice="${v.id}" ${v.archived?'disabled':''}>${esc(tr('编辑音色'))}</button><button data-archive-voice="${v.id}" class="text-button">${esc(tr(v.archived?'恢复音色':'归档音色'))}</button></div></article>`).join('')||`<div class="card library-empty"><h3>${esc(tr('这里还没有音色'))}</h3><p>${esc(tr('上传一段参考声音，或把项目中已准备的角色音色保存到音色库。'))}</p><button id="empty-new-voice" class="primary">${esc(tr('＋ 新建音色'))}</button></div>`;
  $$('[data-edit-voice]').forEach(button=>button.onclick=()=>openPreset((state.libraryItems||state.voices).find(v=>v.id===button.dataset.editVoice)));
  $$('[data-archive-voice]').forEach(button=>button.onclick=()=>guard(async()=>{
    const voice=(state.libraryItems||state.voices).find(v=>v.id===button.dataset.archiveVoice);
    if(!voice.archived&&await confirmChoice(tr('归档后不再出现在可选音色中，已有项目的声音保持不变。可在已归档列表恢复。'),{title:tr('归档音色'),proceed:tr('归档')})==='cancel')return;
    await api(`/api/voices/${voice.id}/archive?archived=${!voice.archived}`,{method:'POST'});await refreshLibrary();
  }));
  if($('#empty-new-voice'))$('#empty-new-voice').onclick=()=>openPreset();
}
$('#voice-search').oninput=()=>{state.libraryKey=null;renderLibrary();};
$('#show-archived').onchange=()=>guard(refreshLibrary);
$('#new-voice').onclick=()=>openPreset();
function openPreset(voice=null,source=null){
  const form=$('#voice-preset-form');form.reset();state.presetContext={voice,source};state.presetDirty=false;state.recordedVoiceFile=null;if(typeof resetVoiceRecorder==='function')resetVoiceRecorder();
  $('#voice-preset-error').textContent='';$('#voice-preset-title').textContent=tr(voice?'编辑音色':'新建音色');
  form.elements.name.value=(voice?.name||source?.name||'').slice(0,100);form.elements.description.value=voice?.description||'';
  form.elements.audio.required=!voice&&!source;$('#voice-upload-label').hidden=!!source;
  $('#voice-upload-preview').hidden=!voice;
  if(voice)$('#voice-upload-preview').src=`/api/voices/${voice.id}/audio?v=${encodeURIComponent(voice.updated)}`;
  else $('#voice-upload-preview').removeAttribute('src');
  $('#voice-source-note').textContent=source?tr('使用当前项目中这个角色的参考声音，保存后可跨项目选用。'):tr('预设修改后不会改变已绑定项目的声音；在项目中重新应用即可更新。');
  $('#voice-preset-dialog').showModal();
}
async function closePreset(force=false){
  if(state.presetSaving&&!force)return;
  if(state.presetDirty&&await confirmChoice(tr('音色预设尚未保存，是否放弃这次编辑？'),{title:tr('保留当前修改'),proceed:tr('放弃修改')})==='cancel')return;
  if(typeof cancelVoiceRecording==='function')await cancelVoiceRecording();
  $('#voice-upload-preview').pause();$('#voice-preset-dialog').close();state.presetDirty=false;
  if(state.presetObjectUrl){URL.revokeObjectURL(state.presetObjectUrl);state.presetObjectUrl=null;}
}
$('#voice-preset-form').oninput=()=>state.presetDirty=true;
$('#voice-preset-form').elements.audio.onchange=e=>{
  state.recordedVoiceFile=null;state.presetDirty=true;if(state.presetObjectUrl)URL.revokeObjectURL(state.presetObjectUrl);
  const file=e.target.files[0];$('#voice-upload-preview').hidden=!file;
  if(file){state.presetObjectUrl=URL.createObjectURL(file);$('#voice-upload-preview').src=state.presetObjectUrl;}
};
$('#close-voice-preset').onclick=()=>guard(closePreset);
$('#voice-preset-dialog').oncancel=e=>{e.preventDefault();guard(closePreset);};
$('#voice-preset-form').onsubmit=async e=>{
  e.preventDefault();if(state.presetSaving)return;
  const form=e.target,{voice,source}=state.presetContext;
  const values={name:form.elements.name.value,description:form.elements.description.value};
  if(state.voiceRecorderBusy){$('#voice-preset-error').textContent=tr('请先停止录音并等待试听文件准备完成');return;}
  const body=new FormData();Object.entries(values).forEach(([k,v])=>body.append(k,v));const audio=state.recordedVoiceFile||form.elements.audio.files[0];if(!audio&&!voice&&!source){$('#voice-preset-error').textContent=tr('请先录音或上传参考声音');return;}if(audio)body.append('audio',audio);
  state.presetSaving=true;[...form.elements].forEach(el=>el.disabled=true);$('#voice-preset-error').textContent='';
  try{
    if(source)await api('/api/voices/from-project',json('POST',{...values,project_id:source.project,speaker:source.speaker}));
    else await api('/api/voices'+(voice?'/'+voice.id:''),{method:voice?'PUT':'POST',body});
    state.presetDirty=false;await closePreset(true);await refreshLibrary();toast(tr('音色预设已保存，可在项目角色中选用'));
  }catch(error){$('#voice-preset-error').textContent=error.message;}
  finally{state.presetSaving=false;[...form.elements].forEach(el=>el.disabled=false);}
};
window.addEventListener('beforeunload',e=>{if(state.presetDirty){e.preventDefault();e.returnValue='';}});

function renderRolePresets(){
  if(!state.project)return;
  $$('#speaker-cards .speaker-card').forEach(card=>{
    const speaker=card.querySelector('input[data-speaker]')?.dataset.speaker;if(!speaker)return;
    const assignment=state.project.voice_assignments?.[speaker];
    const key=JSON.stringify([state.voices,assignment,active(state.project),I18N.locale()]);if(card.dataset.libraryKey===key)return;card.dataset.libraryKey=key;
    const chosen=card.querySelector('[data-preset-select]')?.value||assignment?.id||'';
    card.querySelector('.preset-binding')?.remove();
    const node=document.createElement('div');node.className='preset-binding';
    node.innerHTML=`<label>${esc(tr('从音色库选择'))}<select data-preset-select aria-label="${esc(tr('角色 {0} 的预设音色',speaker))}" ${active(state.project)?'disabled':''}><option value="">${esc(tr('选择预设音色'))}</option>${state.voices.map(v=>`<option value="${v.id}">${esc(v.name)}</option>`).join('')}</select></label><p class="subtle">${esc(assignment?tr('当前项目音色：{0}',assignment.name):tr('使用项目参考声音'))}</p><div class="button-group"><button data-apply-preset ${active(state.project)?'disabled':''}>${esc(tr('应用音色'))}</button><button data-save-preset class="text-button" ${!state.project.references[speaker]||active(state.project)?'disabled':''}>${esc(tr('存入音色库'))}</button></div>`;
    card.append(node);node.querySelector('select').value=state.voices.some(v=>v.id===chosen)?chosen:'';
    node.querySelector('[data-save-preset]').onclick=()=>openPreset(null,{project:state.id,speaker,name:state.project.name+' · '+speaker});
    node.querySelector('[data-apply-preset]').onclick=()=>guard(async()=>{
      const voiceId=node.querySelector('select').value;if(!voiceId)throw Error(tr('请先选择预设音色'));
      const id=state.id,version=state.selectionVersion;
      if(state.dirty)await saveSegments();
      if(state.id!==id||version!==state.selectionVersion)return;
      if(state.dirty)throw Error(tr('仍有新输入的修改未保存，请保存后重试'));
      await api(`/api/projects/${id}/voice/${speaker}`,json('PUT',{voice_id:voiceId}));
      if(state.id!==id||version!==state.selectionVersion)return;
      state.voiceKey=null;await refreshCurrent(false);toast(tr('项目音色已更新，重新生成配音即可'));
    });
  });
}

function boardRows(){
  const query=$('#voice-row-search').value.trim().toLowerCase(),speaker=$('#voice-speaker-filter').value;
  return state.rows.map((row,index)=>({row,index})).filter(({row})=>(!speaker||row.speaker===speaker)&&(!query||`${row.id} ${row.text} ${row.zh}`.toLowerCase().includes(query)));
}
function emotionOptions(value,keep=false){return (keep?`<option value="">${esc(tr('保持原设置'))}</option>`:'')+Object.entries(emotions).map(([id,name])=>`<option value="${id}" ${id===value?'selected':''}>${esc(tr(name))}</option>`).join('');}
function strengthOptions(value,keep=false){return (keep?`<option value="">${esc(tr('保持原设置'))}</option>`:'')+Object.entries(strengths).map(([id,name])=>`<option value="${id}" ${id===value?'selected':''}>${esc(tr(name))}</option>`).join('');}
function renderBulkOptions(){
  $('#bulk-emotion').innerHTML=emotionOptions('',true);$('#bulk-strength').innerHTML=strengthOptions('',true);
}
const originalEmotions=renderEmotionOptions;
renderEmotionOptions=function(){originalEmotions();renderBulkOptions();state.boardKey=null;state.libraryKey=null;renderLibrary();if(state.settingsLoaded)updateSettingsDraft();};
renderBulkOptions();
function updateBoardControls(){
  const busy=active(state.project)||state.saving,selected=state.voiceChecked.size,visible=boardRows().slice(state.voicePage*Number($('#voice-page-size').value),(state.voicePage+1)*Number($('#voice-page-size').value));
  $('#voice-selected-count').textContent=tr('已选 {0} 句（跨页保留）',selected);
  $('#voice-check-page').checked=visible.length>0&&visible.every(({row})=>state.voiceChecked.has(row.id));
  $('#voice-check-page').indeterminate=visible.some(({row})=>state.voiceChecked.has(row.id))&&!$('#voice-check-page').checked;
  $('#apply-voice-batch').disabled=busy||!selected;$('#save-voice-board').disabled=busy||!!state.projectUnavailable||!state.dirty;
  $$('[data-board-field]').forEach(el=>el.disabled=busy||(el.dataset.boardField==='emotion_strength'&&!(state.rows[Number(el.dataset.index)]?.emotion)));
}
const originalEditingControls=renderEditingControls;
renderEditingControls=function(){originalEditingControls();updateBoardControls();updateReviewControls();};
function renderVoiceBoard(force=false){
  if(!state.project)return;
  if($('#single-voice-editor').open&&state.boardProject===state.id){updateBoardControls();return;}
  if(state.boardProject!==state.id){state.boardProject=state.id;state.voiceChecked.clear();state.voicePage=0;state.boardKey=null;$('#voice-row-search').value='';$('#voice-speaker-filter').value='';}
  const focused=document.activeElement;
  if(!force&&$('#voice-table-body').contains(focused)&&focused.matches('input,textarea,select')){updateBoardControls();return;}
  const speakers=[...new Set(state.rows.map(r=>r.speaker))],speakerKey=JSON.stringify([speakers,I18N.locale()]);
  if($('#voice-speaker-filter').dataset.key!==speakerKey){const value=$('#voice-speaker-filter').value;$('#voice-speaker-filter').innerHTML=`<option value="">${esc(tr('全部角色'))}</option>`+speakers.map(s=>`<option value="${esc(s)}">${esc(s)}</option>`).join('');$('#voice-speaker-filter').value=speakers.includes(value)?value:'';$('#voice-speaker-filter').dataset.key=speakerKey;$('#project-speakers').innerHTML=speakers.map(s=>`<option value="${esc(s)}"></option>`).join('');}
  const existing=new Set(state.rows.map(r=>r.id));state.voiceChecked=new Set([...state.voiceChecked].filter(id=>existing.has(id)));
  const items=boardRows(),size=Number($('#voice-page-size').value),pages=Math.max(1,Math.ceil(items.length/size));state.voicePage=Math.min(state.voicePage,pages-1);
  const visible=items.slice(state.voicePage*size,(state.voicePage+1)*size),busy=active(state.project)||state.saving;
  const key=JSON.stringify([state.id,visible,state.voicePage,size,[...state.voiceChecked],busy,I18N.locale()]);
  if(key!==state.boardKey){state.boardKey=key;
    $('#voice-table-body').innerHTML=visible.map(({row:r,index:i})=>`<tr class="${i===state.selected?'current':''}"><td><input type="checkbox" data-board-check="${r.id}" aria-label="${esc(tr('选择第 {0} 句',r.id))}" ${state.voiceChecked.has(r.id)?'checked':''}></td><td class="cue-cell"><b>${r.id}</b><small>${clock(r.start)}–${clock(r.end)}</small></td><td class="script-cell"><textarea dir="auto" rows="2" data-board-field="zh" data-index="${i}" aria-label="${esc(tr('第 {0} 句译文',r.id))}">${esc(r.zh)}</textarea><details><summary>${esc(tr('原文'))}</summary><p dir="auto">${esc(r.text)}</p></details></td><td data-label="${esc(tr('角色编号'))}"><input data-board-field="speaker" data-index="${i}" value="${esc(r.speaker)}" list="project-speakers" maxlength="40" aria-label="${esc(tr('第 {0} 句角色',r.id))}"></td><td data-label="${esc(tr('表达情绪'))}"><select data-board-field="emotion" data-index="${i}" aria-label="${esc(tr('第 {0} 句情绪',r.id))}">${emotionOptions(r.emotion||'inherit')}</select></td><td data-label="${esc(tr('情绪强度'))}"><select data-board-field="emotion_strength" data-index="${i}" aria-label="${esc(tr('第 {0} 句强度',r.id))}">${strengthOptions(r.emotion_strength||'medium')}</select></td><td><button data-board-detail="${i}">${esc(tr('细调 / 试听'))}</button></td></tr>`).join('')||`<tr><td colspan="7" class="empty-inline">${esc(tr('没有匹配的句子'))}</td></tr>`;
  }
  $$('#voice-table-body textarea').forEach(growTextarea);
  $('#voice-table-count').textContent=tr('共 {0} 句 · 本页显示 {1} 句',items.length,visible.length);
  $('#voice-page-label').textContent=`${state.voicePage+1} / ${pages}`;$('#voice-page-prev').disabled=!state.voicePage;$('#voice-page-next').disabled=state.voicePage>=pages-1;updateBoardControls();
}
function setRowExpression(row,emotion,strength){
  if(emotion==='inherit'){delete row.emotion;delete row.emotion_strength;}
  else {if(emotion)row.emotion=emotion;if(row.emotion)row.emotion_strength=strength||row.emotion_strength||'medium';}
}
$('#voice-table-body').oninput=e=>{
  const el=e.target,field=el.dataset.boardField;if(!field||active(state.project))return;
  const row=state.rows[Number(el.dataset.index)];if(!row)return;
  if(field==='emotion')setRowExpression(row,el.value,'');else if(field==='emotion_strength')setRowExpression(row,'',el.value);else row[field]=el.value;
  if(el.tagName==='TEXTAREA')growTextarea(el);setDirty(true);state.boardKey=null;updateBoardControls();
};
$('#voice-table-body').onchange=e=>{
  if(e.target.dataset.boardCheck){const id=Number(e.target.dataset.boardCheck);e.target.checked?state.voiceChecked.add(id):state.voiceChecked.delete(id);state.boardKey=null;updateBoardControls();}
  if(e.target.dataset.boardField){renderSentenceList();renderEditors();state.voiceKey=null;renderVoices();}
};
$('#voice-table-body').onclick=e=>{const button=e.target.closest('[data-board-detail]');if(!button)return;chooseSentence(Number(button.dataset.boardDetail));openSingle();};
function openSingle(){if(!currentRow())return;renderEditors();if(!$('#single-voice-editor').open)openCueDialog($('#single-voice-editor'));}
$('#open-single-editor').onclick=openSingle;
$('#save-voice-board').onclick=()=>guard(saveSegments);
for(const id of ['voice-row-search','voice-speaker-filter','voice-page-size'])$('#'+id).addEventListener(id==='voice-row-search'?'input':'change',()=>{state.voicePage=0;state.boardKey=null;renderVoiceBoard(true);});
$('#voice-page-prev').onclick=()=>{state.voicePage--;renderVoiceBoard();};$('#voice-page-next').onclick=()=>{state.voicePage++;renderVoiceBoard();};
$('#voice-check-page').onchange=e=>{const size=Number($('#voice-page-size').value);boardRows().slice(state.voicePage*size,(state.voicePage+1)*size).forEach(({row})=>e.target.checked?state.voiceChecked.add(row.id):state.voiceChecked.delete(row.id));state.boardKey=null;renderVoiceBoard();};
$('#voice-clear-selection').onclick=()=>{state.voiceChecked.clear();state.boardKey=null;renderVoiceBoard();};
$('#apply-voice-batch').onclick=()=>guard(async()=>{
  if(active(state.project)||state.saving)return;
  const speaker=$('#bulk-speaker').value.trim(),emotion=$('#bulk-emotion').value,strength=$('#bulk-strength').value;
  if(!speaker&&!emotion&&!strength)throw Error(tr('请先选择需要批量修改的设置'));
  if(speaker&&!/^[A-Za-z0-9_-]{1,40}$/.test(speaker))throw Error(tr('角色编号只能使用字母、数字、下划线或连字符'));
  const rows=state.rows.filter(r=>state.voiceChecked.has(r.id));if(!rows.length)return;
  let changed=0;
  rows.forEach(row=>{const before=JSON.stringify(row);if(speaker)row.speaker=speaker;setRowExpression(row,emotion,strength);if(JSON.stringify(row)!==before)changed++;});
  if(!changed){toast(tr('所选句子无需更改；沿用参考音时不单独设置强度。'));return;}
  setDirty(true);state.boardKey=null;state.voiceKey=null;renderSentenceList();renderEditors();renderVoices();renderVoiceBoard();toast(tr('已修改 {0} 句，保存后用于整片配音',changed));
});

function logLevel(text){return /error|failed|exception|traceback|失败|错误/i.test(text)?'error':/warn|警告|降级/i.test(text)?'warning':'info';}
async function openLogs(){
  if(!state.id)return;state.logsProject=state.id;state.logLines=500;state.logData=null;state.logRenderKey=null;
  $('#logs-project-name').textContent=state.project.name;$('#download-logs').href=`/api/projects/${state.id}/logs/download`;
  $('#log-viewer').textContent=tr('正在读取日志…');$('#logs-dialog').showModal();await refreshLogs();
}
async function refreshLogs(){
  if(!$('#logs-dialog').open||state.logsLoading)return;
  const id=state.logsProject;state.logsLoading=true;
  try{const data=await api(`/api/projects/${id}/logs?lines=${state.logLines}`);if(!$('#logs-dialog').open||id!==state.logsProject)return;state.logData=data;renderLogs();$('#logs-update-state').textContent=tr('更新于 {0}',new Date().toLocaleTimeString(I18N.locale(),{hour12:false}));}
  catch(error){$('#logs-update-state').textContent=error.message;}
  finally{state.logsLoading=false;}
}
function renderLogs(){
  if(!state.logData)return;
  const query=$('#log-search').value.trim().toLowerCase(),level=$('#log-level').value,data=state.logData;
  const activity=state.logKind==='activity',source=activity?data.activity:data.diagnostic.split('\n').filter(Boolean);
  const match=value=>{const text=activity?value.message:value;return (!query||text.toLowerCase().includes(query))&&(level==='all'||logLevel(text)===level);};
  const items=source.filter(match),key=JSON.stringify([items,state.logKind,query,level,I18N.locale()]);
  if(key!==state.logRenderKey){state.logRenderKey=key;const viewer=$('#log-viewer'),position=viewer.scrollTop;
    viewer.innerHTML=items.map(item=>{const text=activity?item.message:item;return `<div class="log-line ${logLevel(text)}">${activity?`<time>${esc(new Date(item.time).toLocaleTimeString(I18N.locale(),{hour12:false}))}</time>`:''}<span>${esc(text)}</span></div>`;}).join('')||`<div class="empty-inline">${esc(tr('没有匹配的日志'))}</div>`;
    viewer.scrollTop=$('#logs-follow').checked?viewer.scrollHeight:position;
  }
  $('#logs-summary').textContent=activity?tr('显示 {0} / {1} 条运行记录（保留最近 200 条）',items.length,source.length):tr('显示 {0} 行诊断日志；完整内容可下载',items.length);
  $('#logs-more').hidden=activity||!data.truncated||state.logLines>=3000;
  $('#logs-activity').classList.toggle('selected',activity);$('#logs-diagnostic').classList.toggle('selected',!activity);
}
$('#open-logs').onclick=()=>guard(openLogs);$('#close-logs').onclick=()=>$('#logs-dialog').close();
$('#logs-activity').onclick=()=>{state.logKind='activity';renderLogs();};$('#logs-diagnostic').onclick=()=>{state.logKind='diagnostic';renderLogs();};
$('#log-search').oninput=renderLogs;$('#log-level').onchange=renderLogs;$('#refresh-logs').onclick=()=>guard(refreshLogs);
$('#logs-follow').onchange=()=>{if($('#logs-follow').checked){state.logRenderKey=null;guard(refreshLogs);}};
$('#logs-more').onclick=()=>{state.logLines=Math.min(3000,state.logLines*2);guard(refreshLogs);};
setInterval(()=>{if($('#logs-dialog').open&&$('#logs-follow').checked)guard(refreshLogs);},2000);
guard(refreshLibrary);

// Both panels share one form, so hidden fields are retained when saving.
function selectSettingsPanel(name){
  $$('[data-settings-tab]').forEach(tab=>{const selected=tab.dataset.settingsTab===name;tab.classList.toggle('selected',selected);tab.setAttribute('aria-selected',String(selected));tab.tabIndex=selected?0:-1;});
  $('#settings-common-panel').hidden=name!=='common';$('#settings-models-panel').hidden=name!=='models';
  // Settings uses the document scroll, with both panels retaining their form values.
}
$$('[data-settings-tab]').forEach((tab,index,tabs)=>{
  tab.onclick=()=>selectSettingsPanel(tab.dataset.settingsTab);
  tab.onkeydown=e=>{
    const next=e.key==='ArrowRight'?(index+1)%tabs.length:e.key==='ArrowLeft'?(index+tabs.length-1)%tabs.length:e.key==='Home'?0:e.key==='End'?tabs.length-1:-1;
    if(next<0)return;e.preventDefault();tabs[next].focus();tabs[next].click();
  };
});

const dubbingTargets=['zh','en','ja','es','ar'];
function renderTargetControls(){
  const select=$('#project-target-language');
  const optionKey=I18N.locale();
  if(select.dataset.locale!==optionKey){const previous=select.value;select.innerHTML=dubbingTargets.map(code=>`<option value="${code}">${esc(languageName(code))}</option>`).join('');select.dataset.locale=optionKey;select.value=previous||targetLanguage();}
  if(!state.project)return;
  if(state.targetProject!==state.id){state.targetProject=state.id;state.targetSelectionDirty=false;}
  if(!state.targetSelectionDirty)select.value=targetLanguage();
  select.disabled=active(state.project)||state.saving||state.changingTarget;
  $('#apply-target-language').disabled=select.disabled||select.value===targetLanguage();
}
const renderBeforeTarget=render;
render=function(full=false){renderBeforeTarget(full);renderTargetControls();};
$('#project-target-language').onchange=()=>{state.targetSelectionDirty=$('#project-target-language').value!==targetLanguage();renderTargetControls();};
$('#apply-target-language').onclick=()=>guard(async()=>{
  const id=state.id,version=state.selectionVersion,language=$('#project-target-language').value;
  if(!id||language===targetLanguage()||active(state.project)||state.changingTarget)return;
  const choice=await confirmChoice(tr('切换为 {0} 后，当前译文、试听和成片会备份，原文、时间轴与角色音色保留。之后需要重新翻译和配音。',languageName(language)),{title:tr('切换目标语言'),proceed:tr('备份并切换')});
  if(choice==='cancel')return;
  if(state.dirty)await saveSegments();
  if(id!==state.id||version!==state.selectionVersion)return;
  if(state.dirty)throw Error(tr('仍有新输入的修改未保存，请保存后重试'));
  state.changingTarget=true;renderTargetControls();
  try{
    await api(`/api/projects/${id}/target-language`,json('PUT',{target_language:language,revision:state.editorRevision||0}));
    if(id!==state.id||version!==state.selectionVersion)return;
    state.targetSelectionDirty=false;state.outputRenderKey=null;state.voiceChecked.clear();
    await refreshCurrent(true);switchTab('subtitles');toast(tr('目标语言已切换，旧结果已备份，请重新生成译文。'));
  }finally{state.changingTarget=false;renderTargetControls();}
});

// Project index: project drafts remain mounted when switching application menus.
Object.assign(state,{projectDetail:false,projectTrash:false,trashProjects:[],subtitlePage:0});
renderProjectList=function(){
  const query=$('#project-search').value.trim().toLowerCase();
  const all=state.projectTrash?state.trashProjects:state.projects;
  const items=all.filter(p=>p.name.toLowerCase().includes(query));
  const key=JSON.stringify([items,state.projectTrash,I18N.locale()]);
  $('#project-count').textContent=tr('{0} 个项目',items.length);
  $('#trash-note').hidden=!state.projectTrash;
  $('#projects-active').classList.toggle('selected',!state.projectTrash);$('#projects-trash').classList.toggle('selected',state.projectTrash);
  if(key===state.projectIndexKey)return;state.projectIndexKey=key;
  $('#project-list').innerHTML=items.map(p=>`<article class="project-card card"><div class="project-card-top"><span class="badge">${esc(tr(labels[p.status]||p.status))}</span><span class="subtle">${esc(p.mode==='anime'?tr('动漫'):tr('课程'))}</span></div><h2>${state.projectTrash?esc(p.name):`<button class="project-open" data-open-project="${p.id}">${esc(p.name)}</button>`}</h2><p class="subtle">${esc(languageName(p.language))} → ${esc(languageName(targetLanguage(p)))}</p><p class="project-updated subtle">${esc(tr('更新于 {0}',new Date(p.updated).toLocaleString(I18N.locale())))}</p><div class="project-card-actions">${state.projectTrash?`<button data-restore-project="${p.id}">${esc(tr('恢复项目'))}</button>`:`<button data-open-project="${p.id}" class="primary">${esc(tr('打开项目'))}</button><button data-rename-project="${p.id}" ${active(p)?'disabled':''}>${esc(tr('重命名'))}</button><button data-delete-project="${p.id}" class="text-button danger-text" ${active(p)?'disabled':''}>${esc(tr('删除'))}</button>`}</div></article>`).join('')||`<div class="card project-empty"><h2>${esc(tr(state.projectTrash?'回收站为空':'没有匹配的项目'))}</h2><p class="subtle">${esc(tr(state.projectTrash?'删除的项目会在这里显示，可随时恢复。':'导入一个视频，就可以开始第一个项目。'))}</p></div>`;
};
$('#project-search').oninput=renderProjectList;
$('#projects-active').onclick=()=>{state.projectTrash=false;renderProjectList();};
$('#projects-trash').onclick=()=>guard(async()=>{state.trashProjects=await api('/api/projects/trash');state.projectTrash=true;renderProjectList();});
function openProjectName(project){state.namingProject=project;$('#project-name-input').value=project.name;$('#project-name-error').textContent='';$('#project-name-dialog').showModal();}
$('#rename-current-project').onclick=()=>openProjectName(state.project);
$('#project-name-form').onsubmit=async e=>{
  e.preventDefault();const project=state.namingProject,button=e.target.querySelector('[type=submit]');button.disabled=true;
  try{const data=await api('/api/projects/'+project.id,json('PATCH',{name:$('#project-name-input').value.trim(),revision:project.metadata_revision||0}));if(state.id===data.id){state.project.name=data.name;state.project.metadata_revision=data.metadata_revision;render(false);}$('#project-name-dialog').close();await refreshList();}
  catch(error){$('#project-name-error').textContent=error.message;}finally{button.disabled=false;}
};
$('#project-list').onclick=e=>guard(async()=>{
  const button=e.target.closest('button');if(!button)return;
  if(button.dataset.openProject){await selectProject(button.dataset.openProject);return;}
  if(button.dataset.renameProject){openProjectName(state.projects.find(p=>p.id===button.dataset.renameProject));return;}
  const id=button.dataset.deleteProject||button.dataset.restoreProject;if(!id)return;
  const project=(button.dataset.deleteProject?state.projects:state.trashProjects).find(p=>p.id===id);if(!project)return;
  if(button.dataset.deleteProject){
    if(await confirmChoice(tr('将“{0}”移入回收站？素材和处理结果会保留，可随时恢复。',project.name),{title:tr('删除项目'),proceed:tr('移入回收站')})==='cancel')return;
    if(state.id===id){if(!await allowLeave())return;if(state.id!==id)return;pauseMedia();}
    await api('/api/projects/'+id,json('DELETE',{revision:project.metadata_revision||0}));
    if(state.id===id){++state.selectionVersion;state.id=null;state.project=null;state.rows=[];state.audioDirty=false;state.projectDetail=false;setDirty(false);try{localStorage.removeItem('btv-project');}catch{}}
    toast(tr('项目已移入回收站，可随时恢复'));
  }else{await api('/api/projects/'+id+'/restore',json('POST',{revision:project.metadata_revision||0}));state.trashProjects=await api('/api/projects/trash');toast(tr('项目已恢复'));}
  await refreshList();showSection('projects');
});

// Subtitle review: simultaneous source/translation editing with document scrolling.
filteredRows=function(){const query=$('#segment-search').value.trim().toLowerCase(),filter=$('#subtitle-filter').value;
  return state.rows.map((r,i)=>({r,i})).filter(({r})=>(!query||`${r.id} ${r.text} ${r.zh}`.toLowerCase().includes(query))&&(filter!=='missing'||!r.zh.trim())&&(filter!=='translated'||!!r.zh.trim()));
};
renderSentenceList=function(force=false){
  if(state.subtitleProject!==state.id){state.subtitleProject=state.id;state.listPage=0;state.subtitleKey=null;$('#subtitle-filter').value='all';}
  const items=filteredRows(),size=Number($('#subtitle-page-size').value),pages=Math.max(1,Math.ceil(items.length/size));state.listPage=Math.min(state.listPage,pages-1);
  const visible=items.slice(state.listPage*size,(state.listPage+1)*size),busy=active(state.project)||state.saving;
  $('#list-count').textContent=tr('{0} 句',items.length);$('#segments-page').textContent=`${state.listPage+1} / ${pages}`;
  $('#segments-prev').disabled=!state.listPage;$('#segments-next').disabled=state.listPage>=pages-1;
  if(!force&&$('#segment-list').contains(document.activeElement)&&document.activeElement.matches('textarea')){updateReviewControls();return;}
  const key=JSON.stringify([state.id,visible,busy,I18N.locale()]);if(key===state.subtitleKey)return;state.subtitleKey=key;
  $('#segment-list').innerHTML=visible.map(({r,i})=>`<tr><td class="review-cue"><b>${esc(tr('第 {0} 句',r.id))}</b><small>${clock(r.start)}–${clock(r.end)}</small><small>${esc(r.speaker)}</small><span class="badge">${esc(tr(r.zh.trim()?'已翻译':'待翻译'))}</span></td><td data-label="${esc(tr('原文'))}"><textarea dir="auto" rows="3" data-review-field="text" data-index="${i}" aria-label="${esc(tr('第 {0} 句原文',r.id))}" ${busy?'disabled':''}>${esc(r.text)}</textarea></td><td data-label="${esc(tr('译文配音稿'))}"><textarea dir="auto" rows="3" data-review-field="zh" data-index="${i}" aria-label="${esc(tr('第 {0} 句译文',r.id))}" ${busy?'disabled':''}>${esc(r.zh)}</textarea></td><td><button data-cue-detail="${i}">${esc(tr('详情'))}</button><button data-cue-voice="${i}" class="text-button">${esc(tr('细调 / 试听'))}</button></td></tr>`).join('')||`<tr><td colspan="4" class="empty-inline">${esc(tr(state.rows.length?'没有匹配的句子':'先识别原文，或导入原文 SRT。字幕会显示在这里。'))}</td></tr>`;
  $$('#segment-list textarea').forEach(growTextarea);
};
function growTextarea(el){el.style.height='auto';el.style.height=Math.max(84,el.scrollHeight+2)+'px';}
function updateReviewControls(){$$('[data-review-field]').forEach(el=>el.disabled=active(state.project)||state.saving);}
$('#segment-list').oninput=e=>{
  const el=e.target,field=el.dataset.reviewField;if(!field||active(state.project)||state.saving)return;
  const row=state.rows[Number(el.dataset.index)];if(!row)return;row[field]=el.value;growTextarea(el);setDirty(true);state.subtitleKey=null;state.boardKey=null;
};
$('#segment-list').onchange=()=>{renderEditors();};
$('#segment-list').onclick=e=>{
  const detail=e.target.closest('[data-cue-detail]'),voice=e.target.closest('[data-cue-voice]');if(!detail&&!voice)return;
  chooseSentence(Number(detail?.dataset.cueDetail??voice.dataset.cueVoice));
  if(voice)openSingle();else if(!$('#subtitle-detail-dialog').open)openCueDialog($('#subtitle-detail-dialog'));
};
for(const id of ['subtitle-filter','subtitle-page-size'])$('#'+id).onchange=()=>{state.listPage=0;state.subtitleKey=null;renderSentenceList(true);};
$('#close-subtitle-detail').onclick=()=>$('#subtitle-detail-dialog').close();
$('#subtitle-detail-dialog').addEventListener('close',()=>{$('#editor-video').pause();state.subtitleKey=null;renderSentenceList();renderVoiceBoard();});
// The cue dialog's navigation remains useful without leaving the review page.
$('#panel-subtitles [data-go="voices"]')?.addEventListener('click',()=>$('#subtitle-detail-dialog').close());
$('#subtitle-detail-dialog [data-go="voices"]').onclick=()=>{$('#subtitle-detail-dialog').close();switchTab('voices');openSingle();};
function closeSingle(){$('#single-voice-editor').close();}
$('#close-single-editor').onclick=closeSingle;$('#finish-single-editor').onclick=closeSingle;
$('#single-voice-editor').addEventListener('close',()=>{$('#sentence-audio').pause();state.boardKey=null;state.subtitleKey=null;renderVoiceBoard();renderSentenceList();});
const renderBeforeReview=render;
render=function(full=false){renderBeforeReview(full);renderProjectAvailability();if(state.project){$('#workspace').classList.toggle('has-active-job',active(state.project));$('#workspace').classList.toggle('has-job-error',!!state.project.error);renderSentenceList();updateReviewControls();$('#audition-live-status').textContent=state.projectUnavailable?$('#project-unavailable-message').textContent:active(state.project)?[tr(state.project.tts_runtime?.phase||''),$('#job-status').textContent].filter(Boolean).join(' · '):state.project.error||'';}};

// A menu switch retains settings drafts; saving only clears the submitted generation.
function updateSettingsDraft(){
  $('#settings-draft-dot').hidden=!state.settingsDirty;
  $('#settings-draft-status').textContent=tr(state.settingsDirty?'有未保存的设置':'设置已保存');
  $('#reset-settings').disabled=!state.settingsDirty||!!state.settingsSaving;
}
$('#settings-form').oninput=()=>{state.settingsGeneration=(state.settingsGeneration||0)+1;state.settingsDirty=true;updateSettingsDraft();};
$('#reset-settings').onclick=()=>guard(async()=>{
  if(state.settingsDirty&&await confirmChoice(tr('放弃尚未保存的设置修改，恢复上次保存的内容？'),{title:tr('放弃设置修改'),proceed:tr('放弃修改')})==='cancel')return;
  Object.entries(state.settings).forEach(([k,v])=>{const el=$('#settings-form').elements[k];if(el)el.value=v;});state.settingsDirty=false;updateSettingsDraft();
});
$('#settings-form').onsubmit=async e=>{
  e.preventDefault();if(state.settingsSaving||!state.settingsLoaded)return;
  const generation=state.settingsGeneration||0;state.settingsSaving=true;$('#save-settings').disabled=true;$('#settings-error').textContent='';updateSettingsDraft();
  try{
    const data={...state.settings,...Object.fromEntries(new FormData(e.target))};
    for(const k of ['max_speed','max_lead','max_lag','audio_workers','tts_batch_size','tts_idle_minutes','translation_batch'])data[k]=Number(data[k]);
    data.translation_compact=[true,'true'].includes(data.translation_compact);
    state.settings=await api('/api/settings',json('PUT',data));
    if(generation===(state.settingsGeneration||0))state.settingsDirty=false;
    await diagnostics();toast(tr('设置已保存，下次任务生效'));
  }catch(error){$('#settings-error').textContent=error.message;}
  finally{state.settingsSaving=false;$('#save-settings').disabled=false;updateSettingsDraft();}
};
window.addEventListener('beforeunload',e=>{if(state.settingsDirty){e.preventDefault();e.returnValue='';}});

function openCueDialog(dialog){
  const opener=document.activeElement;
  const attribute=['data-board-detail','data-cue-detail','data-cue-voice'].find(name=>opener?.hasAttribute(name));
  dialog._pagePosition={x:window.scrollX,y:window.scrollY,opener,selector:attribute?'['+attribute+'="'+opener.getAttribute(attribute)+'"]':null};
  dialog.showModal();
}
for(const id of ['subtitle-detail-dialog','single-voice-editor']){
  $('#'+id).addEventListener('close',()=>{
    const position=$('#'+id)._pagePosition;if(!position)return;
    requestAnimationFrame(()=>{const opener=position.opener?.isConnected?position.opener:position.selector?document.querySelector(position.selector):null;opener?.focus({preventScroll:true});window.scrollTo({left:position.x,top:position.y,behavior:'instant'});});
  });
}

// Keep column labels below the save toolbar during natural document scrolling.
const reviewToolbar=$('#panel-subtitles>.section-toolbar');
new ResizeObserver(()=>$('#panel-subtitles').style.setProperty('--review-toolbar-height',reviewToolbar.getBoundingClientRect().height+'px')).observe(reviewToolbar);

// An unavailable project keeps its current rows and drafts until the user restores it.
function markProjectUnavailable(error){
  if(!['project_deleted','project_missing'].includes(error.code)||error.projectId!==state.id)return false;
  state.projectUnavailable={code:error.code,revision:error.revision,message:error.message};
  renderProjectAvailability();renderEditingControls();renderNextAction();return true;
}
function renderProjectAvailability(){
  const unavailable=state.projectUnavailable;
  $('#project-unavailable').hidden=!unavailable;
  $('#project-unavailable-message').textContent=unavailable?tr(unavailable.code==='project_deleted'?'项目已移入回收站。恢复后可继续编辑和试听，当前草稿仍保留在本页。':'项目文件不可用。请检查目录或重新导入视频，当前草稿仍保留在本页。'):'';
  const restorable=unavailable?.code==='project_deleted';
  $('#restore-unavailable-project').hidden=!restorable;$('#restore-audition-project').hidden=!restorable;
  if(unavailable)$('#audition-live-status').textContent=$('#project-unavailable-message').textContent;
}
async function restoreUnavailableProject(){
  const id=state.id,version=state.selectionVersion,unavailable=state.projectUnavailable;
  if(unavailable?.code!=='project_deleted')return;
  await api('/api/projects/'+id+'/restore',json('POST',{revision:unavailable.revision}));
  if(id!==state.id||version!==state.selectionVersion)return;
  await refreshCurrent(false);await refreshList();toast(tr('项目已恢复'));
}
$('#restore-unavailable-project').onclick=()=>guard(restoreUnavailableProject);
$('#restore-audition-project').onclick=()=>guard(restoreUnavailableProject);

// Resizing the workspace adjusts text heights without replacing focused controls.
let dubbingContentWidth=0,dubbingResizeFrame;
new ResizeObserver(entries=>{
  const width=entries[0].contentRect.width;if(Math.abs(width-dubbingContentWidth)<1)return;
  dubbingContentWidth=width;cancelAnimationFrame(dubbingResizeFrame);
  dubbingResizeFrame=requestAnimationFrame(()=>$$('#voice-table-body textarea').forEach(growTextarea));
}).observe($('.dubbing-board'));
