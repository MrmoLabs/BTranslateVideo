"use strict";
const diarUI={project:null,version:null,key:null,choices:new Map(),page:0,ready:false,loading:false,submitting:false,applying:false};
const diarDialog=$('#diarization-dialog');
function pauseDiarizationMedia(){for(const audio of $$('#diarization-dialog audio'))audio.pause();if(typeof stopSourceCue==='function')stopSourceCue();}
async function loadDiarizationEnvironment(){
  const info=await api('/api/diarization/environment');
  diarUI.ready=info.ready;
  const message=info.ready?tr('Community-1 本地文件已准备'):info.runtime_ready?tr('Community-1 模型尚未准备'):info.model_ready?tr('需要准备说话人识别独立环境'):tr('需要准备独立环境和 Community-1 模型');
  $('#diarization-environment').textContent=message;$('#diarization-environment').dataset.ok=String(info.ready);
  $('#diarization-model-location').textContent=info.model;
  if(!diarUI.project)$('#diarization-device').value=info.device;
  renderDiarizationControls();return info;
}
async function openDiarization(){
  if(!state.id)return;
  pauseMedia();diarUI.project=state.id;diarUI.version=state.selectionVersion;
  $('#diarization-error').textContent='';
  if(!diarDialog.open)openCueDialog(diarDialog);
  renderDiarization();
  const project=state.id,version=state.selectionVersion;
  diarUI.loading=true;renderDiarizationControls();
  try{const info=await loadDiarizationEnvironment();if(project===state.id&&version===state.selectionVersion&&!$('#diarization-device').dataset.edited)$('#diarization-device').value=info.device;}
  finally{diarUI.loading=false;renderDiarizationControls();}
}
function diarizationProposal(){const proposal=state.project?.diarization_proposal;return proposal?.status==='ready'?proposal:null;}
function diarizationRoleName(speaker,proposal){return proposal?.roles[speaker]?.name||roleName(speaker);}
function diarizationChoices(proposal){return [...new Set([...Object.keys(proposal.roles),...projectSpeakers()])];}
function renderDiarizationControls(){
  const busy=!!state.project&&active(state.project),wrong=diarUI.project!==state.id||diarUI.version!==state.selectionVersion;
  const locked=busy||wrong||diarUI.submitting||diarUI.applying;
  $('#start-diarization').disabled=locked||diarUI.loading||!diarUI.ready||!state.rows.length||!!state.projectUnavailable;
  $('#start-diarization').textContent=tr(busy&&state.project?.stage==='diarize'?'正在识别说话人…':'开始识别');
  $('#cancel-diarization').hidden=!(busy&&state.project?.stage==='diarize');
  $('#apply-diarization').disabled=locked||!diarizationProposal()||!diarUI.choices.size||!!state.projectUnavailable||!!(diarizationProposal()&&(diarizationProposal().revision!==state.editorRevision||state.dirty));
  $('#apply-diarization').textContent=tr('采用所选分配（{0} 句）',diarUI.choices.size);
  for(const element of $$('#diarization-options input, #diarization-options select, #diarization-proposal input, #diarization-proposal select'))element.disabled=locked;
  const filtered=filteredDiarizationItems();
  $('#diarization-batch-count').textContent=tr('筛选 {0} 句 · 已选 {1} 句（跨页）',filtered.length,diarUI.choices.size);
  for(const control of $$('.diarization-batch-tools button,.diarization-batch-tools select'))control.disabled=locked||!diarizationProposal();
  $('#diarization-assign-filtered').disabled=locked||!filtered.length||!$('#diarization-bulk-role').value;
  const mode=$('#diarization-count-mode').value;
  $('#diarization-fixed-count').hidden=mode!=='fixed';$('#diarization-range-count').hidden=mode!=='range';
  $('#diarization-live').textContent=busy&&state.project?.stage==='diarize'?tr(state.project.processing?.message||'正在识别说话人…'):state.project?.stage==='diarize'&&state.project.error?state.project.error:'';
}
function renderDiarization(){
  if(!diarDialog.open)return;
  if(diarUI.project!==state.id||diarUI.version!==state.selectionVersion){diarDialog.close();return;}
  const proposal=diarizationProposal();$('#diarization-proposal').hidden=!proposal;
  if(proposal){
    const key=[state.id,proposal.request_id,proposal.status].join(':');
    if(diarUI.key!==key){
      const options=diarizationChoices(proposal).map(id=>`<option value="${esc(id)}">${esc(diarizationRoleName(id,proposal))}</option>`).join('');
      $('#diarization-role-filter').innerHTML=`<option value="">${esc(tr('全部角色'))}</option>`+options;$('#diarization-bulk-role').innerHTML=options;
      $('#diarization-config').open=false;diarUI.key=key;diarUI.page=0;diarUI.choices=new Map(proposal.items.filter(item=>item.after&&!item.needs_review).map(item=>[item.id,item.after]));
      $('#diarization-roles').innerHTML=Object.entries(proposal.roles).map(([id,role])=>`<article class="diarization-role"><b>${esc(role.name)}</b><p class="subtle">${esc(tr(role.reused?'复用已有角色，保留名称与音色':'新识别角色；可在下方批量分配到已有角色'))}</p><p class="subtle">${esc(tr('匹配 {0} 句',proposal.items.filter(item=>item.after===id).length))}</p>${proposal.references[id]?`<audio controls preload="none" aria-label="${esc(tr('{0} 原片参考音',role.name))}" src="${fileUrl(proposal.references[id])}"></audio>`:`<p class="subtle">${esc(tr('清晰独白不足 3 秒，采用后请上传参考音'))}</p>`}</article>`).join('');
      renderDiarizationTable();
    }
    $('#diarization-summary').textContent=tr('识别出 {0} 个说话人 · {1} 句待核对 · 设备 {2}',Object.keys(proposal.roles).length,proposal.items.filter(item=>item.needs_review).length,proposal.device.toUpperCase());
    $('#diarization-stale').hidden=proposal.revision===state.editorRevision&&!state.dirty;
    if(!$('#diarization-stale').hidden)$('#apply-diarization').disabled=true;
  }
  renderDiarizationControls();
  if(proposal&&(proposal.revision!==state.editorRevision||state.dirty))$('#apply-diarization').disabled=true;
}
function filteredDiarizationItems(){
  const proposal=diarizationProposal();if(!proposal)return [];
  const onlyReview=$('#diarization-only-review').checked,role=$('#diarization-role-filter').value;
  return proposal.items.filter(item=>(!onlyReview||item.needs_review)&&(!role||item.after===role));
}
function renderDiarizationTable(){
  const proposal=diarizationProposal();if(!proposal)return;
  const items=filteredDiarizationItems();
  const pages=Math.max(1,Math.ceil(items.length/20));diarUI.page=Math.max(0,Math.min(diarUI.page,pages-1));
  const rows=new Map(state.rows.map(row=>[row.id,row]));
  $('#diarization-rows').innerHTML=items.slice(diarUI.page*20,diarUI.page*20+20).map(item=>{
    const row=rows.get(item.id);if(!row)return '';
    const selected=diarUI.choices.get(item.id),suggested=selected||item.after;
    const options=diarizationChoices(proposal).map(key=>`<option value="${esc(key)}" ${key===suggested?'selected':''}>${esc(diarizationRoleName(key,proposal))}</option>`).join('');
    return `<tr><td><input type="checkbox" data-diar-check="${item.id}" ${selected?'checked':''} aria-label="${esc(tr('采用第 {0} 句分配',item.id))}"></td><td><b>${esc(tr('第 {0} 句',item.id))}</b><small>${clock(row.start)}–${clock(row.end)}</small><button type="button" data-source-audio="${item.id}">${esc(tr('听原声'))}</button></td><td><p dir="auto">${esc(row.zh||row.text)}</p><small>${esc(tr('原角色：{0}',roleName(item.before)))}</small></td><td><select data-diar-role="${item.id}" aria-label="${esc(tr('第 {0} 句识别角色',item.id))}">${!suggested?`<option value="">${esc(tr('请选择角色'))}</option>`:''}${options}</select><small class="${item.needs_review?'diar-review':'subtle'}">${esc(item.needs_review?item.reasons.map(reason=>tr(reason)).join(' · '):tr('按人声时间匹配'))}</small></td></tr>`;
  }).join('');
  $('#diarization-page').textContent=tr('{0} / {1} · {2} 句',diarUI.page+1,pages,items.length);
  $('#diarization-prev').disabled=diarUI.page===0;$('#diarization-next').disabled=diarUI.page===pages-1;
  renderDiarizationControls();
}
document.addEventListener('click',event=>{if(event.target.closest('[data-open-diarization]'))guard(openDiarization);});
$('#close-diarization').onclick=()=>diarDialog.close();
diarDialog.addEventListener('close',()=>{pauseDiarizationMedia();const position=diarDialog._pagePosition;if(position)requestAnimationFrame(()=>{position.opener?.isConnected&&position.opener.focus({preventScroll:true});window.scrollTo({left:position.x,top:position.y,behavior:'instant'});});});
diarDialog.addEventListener('cancel',event=>{if(diarUI.applying)event.preventDefault();});
$('#diarization-count-mode').onchange=renderDiarizationControls;
$('#diarization-device').onchange=()=>$('#diarization-device').dataset.edited='true';
$('#refresh-diarization-environment').onclick=()=>guard(loadDiarizationEnvironment);
$('#diarization-only-review').onchange=()=>{diarUI.page=0;renderDiarizationTable();};
$('#diarization-role-filter').onchange=()=>{diarUI.page=0;renderDiarizationTable();};
$('#diarization-select-filtered').onclick=()=>{
  for(const item of filteredDiarizationItems()){
    const select=$(`[data-diar-role="${item.id}"]`),speaker=select?.value||diarUI.choices.get(item.id)||item.after;
    if(speaker)diarUI.choices.set(item.id,speaker);
  }
  renderDiarizationTable();
};
$('#diarization-clear-filtered').onclick=()=>{for(const item of filteredDiarizationItems())diarUI.choices.delete(item.id);renderDiarizationTable();};
$('#diarization-assign-filtered').onclick=()=>{
  const speaker=$('#diarization-bulk-role').value;if(!speaker)return;
  const items=filteredDiarizationItems();for(const item of items)diarUI.choices.set(item.id,speaker);
  renderDiarizationTable();toast(tr('已调整筛选内 {0} 句候选分配，点击采用后才会保存。',items.length));
};
$('#diarization-prev').onclick=()=>{diarUI.page--;renderDiarizationTable();};$('#diarization-next').onclick=()=>{diarUI.page++;renderDiarizationTable();};
$('#diarization-rows').addEventListener('change',event=>{
  const checkbox=event.target.closest('[data-diar-check]'),select=event.target.closest('[data-diar-role]');
  if(checkbox){const id=Number(checkbox.dataset.diarCheck),speaker=$(`[data-diar-role="${id}"]`).value;if(checkbox.checked&&speaker)diarUI.choices.set(id,speaker);else {diarUI.choices.delete(id);checkbox.checked=false;}}
  if(select){const id=Number(select.dataset.diarRole);if(diarUI.choices.has(id))diarUI.choices.set(id,select.value);}
  renderDiarizationControls();
});
$('#start-diarization').onclick=()=>guard(async()=>{
  if(diarUI.submitting)return;diarUI.submitting=true;$('#diarization-error').textContent='';renderDiarizationControls();
  try{
    const {id,version}=await saveProjectDraftsForAction();
    const data={revision:state.editorRevision,device:$('#diarization-device').value},mode=$('#diarization-count-mode').value;
    if(mode==='fixed')data.num_speakers=Number($('#diarization-number').value);
    if(mode==='range'){data.min_speakers=Number($('#diarization-min').value);data.max_speakers=Number($('#diarization-max').value);}
    const response=await api(`/api/projects/${id}/diarization`,json('POST',data));
    if(id!==state.id||version!==state.selectionVersion)return;
    state.diarizationPending={project:id,request:response.request_id};await refreshCurrent(false);
  }catch(error){$('#diarization-error').textContent=error.message;}
  finally{diarUI.submitting=false;renderDiarizationControls();}
});
$('#cancel-diarization').onclick=()=>guard(async()=>{await api(`/api/projects/${state.id}/cancel`,json('POST',{}));await refreshCurrent(false);});
$('#apply-diarization').onclick=()=>guard(async()=>{
  const proposal=diarizationProposal();if(!proposal||diarUI.applying)return;
  diarUI.applying=true;$('#diarization-error').textContent='';renderDiarizationControls();pauseDiarizationMedia();
  try{
    const {id,version}=await saveProjectDraftsForAction();
    await api(`/api/projects/${id}/diarization/apply`,json('POST',{revision:state.editorRevision,request_id:proposal.request_id,assignments:[...diarUI.choices].map(([id,speaker])=>({id,speaker}))}));
    if(id!==state.id||version!==state.selectionVersion)return;
    state.voiceKey=null;state.boardKey=null;state.subtitleKey=null;state.outputRenderKey=null;
    await refreshCurrent(true);diarDialog.close();$('#voice-references').open=true;toast(tr('说话人分配已采用，请试听各角色参考音并核对重叠对白。'));
  }catch(error){$('#diarization-error').textContent=error.message;}
  finally{diarUI.applying=false;renderDiarizationControls();}
});
const renderBeforeDiarization=render;
render=function(full=false){
  renderBeforeDiarization(full);renderDiarization();
  const proposal=diarizationProposal(),pending=state.diarizationPending;
  if(proposal&&pending?.project===state.id&&pending.request===proposal.request_id&&!active(state.project)){
    state.diarizationPending=null;if(!diarDialog.open)toast(tr('说话人识别已完成，可在配音页面预览并采用。'));
  }
};
const controlsBeforeDiarization=renderEditingControls;
renderEditingControls=function(){controlsBeforeDiarization();for(const button of $$('[data-open-diarization]'))button.disabled=!state.rows.length||!!state.projectUnavailable;};
document.addEventListener('play',event=>{if(diarDialog.contains(event.target)&&event.target instanceof HTMLMediaElement)for(const audio of $$('#diarization-dialog audio'))if(audio!==event.target)audio.pause();},true);
document.addEventListener('visibilitychange',()=>{if(document.hidden)pauseDiarizationMedia();});
const sectionBeforeDiarization=showSection;
showSection=function(section){if(section!=='projects'&&diarDialog.open)diarDialog.close();return sectionBeforeDiarization(section);};

$('#check-diarization-runtime').onclick=()=>guard(async()=>{
  const button=$('#check-diarization-runtime');if(button.disabled)return;button.disabled=true;$('#diarization-runtime-info').textContent=tr('正在检查独立环境…');
  try{
    const info=await api('/api/diarization/check',json('POST',{}));
    $('#diarization-runtime-info').textContent=info.ok?tr('依赖检查通过 · PyTorch {0} · pyannote {1} · CUDA {2}',info.torch,info.pyannote,tr(info.cuda_available?'可用':'不可用')):info.message;
  }finally{button.disabled=false;}
});
