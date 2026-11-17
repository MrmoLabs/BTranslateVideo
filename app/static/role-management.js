'use strict';
// Display names never change stable speaker IDs, references, or cue assignments.
const roleNames={drafts:{},saving:new Set(),errors:new Map(),storageFailed:false};
try{roleNames.drafts=JSON.parse(localStorage.getItem('btv-role-name-drafts')||'{}');}catch{}
function saveRoleNameDrafts(){
  try{localStorage.setItem('btv-role-name-drafts',JSON.stringify(roleNames.drafts));roleNames.storageFailed=false;}catch{roleNames.storageFailed=true;}
}
function roleNameKey(speaker){return state.id+':'+speaker;}
function renderRoleNameFields(){
  if(!state.project)return;
  for(const card of $$('#speaker-cards .speaker-card')){
    const speaker=card.querySelector('[data-speaker]')?.dataset.speaker;if(!speaker)continue;
    const key=roleNameKey(speaker),draft=roleNames.drafts[key],busy=active(state.project)||state.saving||roleNames.saving.has(key);
    let input=card.querySelector('[data-role-name]');
    if(!input){
      const heading=card.querySelector('h3'),label=document.createElement('label');label.className='role-name-label';
      label.innerHTML=`<span class="sr-only">${esc(tr('角色名称'))}</span><input data-role-name="${esc(speaker)}" maxlength="60" aria-label="${esc(tr('{0} 的名称',roleName(speaker)))}" title="${esc(tr('直接修改角色名称'))}">`;
      heading.replaceWith(label);input=label.querySelector('input');
      const actions=document.createElement('div');actions.className='role-name-actions';
      actions.innerHTML=`<button type="button" data-save-role-name="${esc(speaker)}">${esc(tr('保存名称'))}</button><button type="button" class="text-button" data-reset-role-name="${esc(speaker)}">${esc(tr('撤销改名'))}</button><small role="status"></small>`;
      card.querySelector('.card-heading').after(actions);
      const select=document.createElement('button');select.type='button';select.className='text-button role-select-lines';select.dataset.selectRoleLines=speaker;select.textContent=tr('批量调整此角色台词');
      card.querySelector('.role-assigned-count').after(select);
    }
    if(document.activeElement!==input)input.value=draft?.value??roleName(speaker);
    input.disabled=busy;input.setAttribute('aria-label',tr('{0} 的名称',roleName(speaker)));
    const actions=card.querySelector('.role-name-actions');actions.hidden=!draft&&!roleNames.errors.has(key);
    actions.querySelector('[data-save-role-name]').disabled=busy||!draft;
    actions.querySelector('[data-reset-role-name]').disabled=roleNames.saving.has(key);
    const message=actions.querySelector('[role=status]');message.textContent=roleNames.errors.get(key)||'';message.classList.toggle('error',!!message.textContent);
    const select=card.querySelector('[data-select-role-lines]');select.disabled=busy||!state.rows.some(row=>row.speaker===speaker);
  }
}
document.addEventListener('input',event=>{
  const input=event.target.closest('[data-role-name]');if(!input)return;
  const speaker=input.dataset.roleName,key=roleNameKey(speaker),base=state.project.roles?.[speaker]?.name||'';
  if(input.value===roleName(speaker))delete roleNames.drafts[key];
  else roleNames.drafts[key]={value:input.value,base:roleNames.drafts[key]?.base??base};
  roleNames.errors.delete(key);saveRoleNameDrafts();renderRoleNameFields();
});
async function saveRoleName(speaker){
  const key=roleNameKey(speaker),draft=roleNames.drafts[key];if(!draft||roleNames.saving.has(key))return;
  roleNames.saving.add(key);roleNames.errors.delete(key);renderRoleNameFields();
  try{
    const name=draft.value.trim();if(!name)throw Error(tr('请填写角色名称'));
    const {id,version}=await saveProjectDraftsForAction();
    await api(`/api/projects/${id}/roles/${encodeURIComponent(speaker)}`,json('PUT',{revision:state.editorRevision,name,expected_name:draft.base}));
    delete roleNames.drafts[key];saveRoleNameDrafts();
    if(id!==state.id||version!==state.selectionVersion)return;
    state.voiceKey=null;state.boardKey=null;state.subtitleKey=null;await refreshCurrent(false);toast(tr('角色名称已更新，台词分配与音色保持不变。'));
  }catch(error){roleNames.errors.set(key,error.message);}
  finally{roleNames.saving.delete(key);renderRoleNameFields();}
}
document.addEventListener('click',event=>{
  const save=event.target.closest('[data-save-role-name]');if(save&&!save.disabled){void saveRoleName(save.dataset.saveRoleName);return;}
  const reset=event.target.closest('[data-reset-role-name]');if(reset&&!reset.disabled){const speaker=reset.dataset.resetRoleName,key=roleNameKey(speaker);delete roleNames.drafts[key];roleNames.errors.delete(key);saveRoleNameDrafts();renderRoleNameFields();return;}
  const select=event.target.closest('[data-select-role-lines]');if(select&&!select.disabled){
    const speaker=select.dataset.selectRoleLines;$('#voice-row-search').value='';$('#voice-speaker-filter').value=speaker;state.voicePage=0;
    state.voiceChecked=new Set(state.rows.filter(row=>row.speaker===speaker).map(row=>row.id));state.boardKey=null;renderVoiceBoard(true);
    toast(tr('已选中此角色全部 {0} 句，可批量修改角色、情绪与强度。',state.voiceChecked.size));
  }
});
const selectFilteredLines=document.createElement('button');selectFilteredLines.type='button';selectFilteredLines.id='voice-select-filtered';selectFilteredLines.className='text-button';selectFilteredLines.textContent=tr('选中全部筛选结果');
$('#voice-selected-count').before(selectFilteredLines);
selectFilteredLines.onclick=()=>{if(active(state.project)||state.saving)return;for(const {row} of boardRows())state.voiceChecked.add(row.id);state.boardKey=null;renderVoiceBoard();};
const controlsBeforeRoleNames=updateBoardControls;
updateBoardControls=function(){controlsBeforeRoleNames();selectFilteredLines.textContent=tr('选中全部筛选结果');selectFilteredLines.disabled=active(state.project)||state.saving||!boardRows().length;};
const voicesBeforeRoleNames=renderVoices;
renderVoices=function(){voicesBeforeRoleNames();renderRoleNameFields();};
window.addEventListener('beforeunload',event=>{if(roleNames.storageFailed&&Object.keys(roleNames.drafts).length){event.preventDefault();event.returnValue='';}});

// Explicit migration keeps assigned lines intact when a role is removed.
const roleDeletion={context:null,busy:false};
const roleDeleteDialog=document.createElement('dialog');roleDeleteDialog.id='delete-role-dialog';roleDeleteDialog.className='role-delete-dialog';document.body.append(roleDeleteDialog);
function openRoleDeletion(speaker){
  if(active(state.project)||state.saving)return;
  const count=state.rows.filter(row=>row.speaker===speaker).length,others=projectSpeakers().filter(id=>id!==speaker);
  roleDeletion.context={project:state.id,version:state.selectionVersion,speaker,name:state.project.roles?.[speaker]?.name||''};
  roleDeleteDialog.setAttribute('aria-label',tr('删除角色'));
  roleDeleteDialog.innerHTML=`<form id="delete-role-form"><div class="dialog-heading"><h2>${esc(tr('删除角色'))}</h2><button type="button" data-close-role-delete aria-label="${esc(tr('关闭'))}">×</button></div><p>${esc(tr('角色：{0}',roleName(speaker)))}</p><p>${esc(count?tr('此角色有 {0} 句台词。台词将保留，请选择暂不分配或迁移到已有角色。',count):tr('此角色没有台词，可以直接删除。'))}</p>${count?`<label>${esc(tr('台词处理方式'))}<select id="delete-role-target" required>${others.length?`<option value="">${esc(tr('请选择处理方式'))}</option>`:''}<option value="${UNASSIGNED_SPEAKER}">${esc(tr('暂不分配角色（保留全部台词）'))}</option>${others.map(id=>`<option value="${esc(id)}">${esc(tr('{0} · {1} 句台词',roleName(id),state.rows.filter(row=>row.speaker===id).length))}</option>`).join('')}</select></label>`:'<input type="hidden" id="delete-role-target" value="">'}<p class="subtle">${esc(tr('未分配的台词可稍后批量指定角色；配音前需完成分配。音色库预设保留。'))}</p><p id="delete-role-error" class="error" role="status"></p><div class="button-group"><button type="button" data-close-role-delete>${esc(tr('取消'))}</button><button type="submit" class="primary">${esc(tr('删除角色'))}</button></div></form>`;
  roleDeleteDialog.querySelectorAll('[data-close-role-delete]').forEach(button=>button.onclick=()=>{if(!roleDeletion.busy)roleDeleteDialog.close();});
  roleDeleteDialog.querySelector('form').onsubmit=async event=>{
    event.preventDefault();if(roleDeletion.busy)return;const context=roleDeletion.context;
    roleDeletion.busy=true;const replacement=$('#delete-role-target').value;
    for(const input of roleDeleteDialog.querySelectorAll('button,select'))input.disabled=true;
    try{
      if(context.project!==state.id||context.version!==state.selectionVersion)throw Error(tr('项目已切换，请重新操作'));
      const {id,version}=await saveProjectDraftsForAction();
      await api(`/api/projects/${id}/roles/${encodeURIComponent(speaker)}`,json('DELETE',{revision:state.editorRevision,replacement,expected_name:context.name}));
      delete roleNames.drafts[id+':'+speaker];saveRoleNameDrafts();roleDeleteDialog.close();
      if(id!==state.id||version!==state.selectionVersion)return;
      state.voiceKey=null;state.boardKey=null;state.subtitleKey=null;state.outputRenderKey=null;await refreshCurrent(true);
      toast(tr(replacement===UNASSIGNED_SPEAKER?'角色已删除，台词已保留为未分配，可批量重新指定。':'角色已删除，相关台词已按选择迁移。'));
    }catch(error){$('#delete-role-error').textContent=error.message;}
    finally{roleDeletion.busy=false;for(const input of roleDeleteDialog.querySelectorAll('button,select'))input.disabled=false;}
  };
  openCueDialog(roleDeleteDialog);
}
roleDeleteDialog.addEventListener('cancel',event=>{if(roleDeletion.busy)event.preventDefault();});
roleDeleteDialog.addEventListener('close',()=>{const position=roleDeleteDialog._pagePosition;if(position)requestAnimationFrame(()=>{position.opener?.isConnected&&position.opener.focus({preventScroll:true});window.scrollTo({left:position.x,top:position.y,behavior:'instant'});});});
document.addEventListener('click',event=>{const button=event.target.closest('[data-delete-role]');if(button&&!button.disabled)openRoleDeletion(button.dataset.deleteRole);});
const renderNamesBeforeDelete=renderRoleNameFields;
renderRoleNameFields=function(){
  renderNamesBeforeDelete();
  if(!state.project)return;
  for(const card of $$('#speaker-cards .speaker-card')){
    const speaker=card.querySelector('[data-speaker]')?.dataset.speaker;if(!speaker)continue;
    let button=card.querySelector('[data-delete-role]');
    if(!button){button=document.createElement('button');button.type='button';button.className='role-delete-button';button.dataset.deleteRole=speaker;card.querySelector('.card-heading').append(button);}
    button.textContent=tr('删除角色');button.setAttribute('aria-label',tr('删除角色：{0}',roleName(speaker)));button.disabled=active(state.project)||state.saving||roleNames.saving.has(roleNameKey(speaker));
  }
};
