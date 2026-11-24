'use strict';
// A single table is the workspace: select, generate, compare, edit, adopt.
const subtitleReview={project:null,selected:new Set(),items:{},fetchKey:'',fetchGeneration:0,submitting:false,applying:false,drafts:{},memory:new Map(),storageFailed:false};
function reviewStorageKey(){return 'btv-subtitle-review:'+state.id;}
function saveReviewLocal(){
  const saved={selected:[...subtitleReview.selected],drafts:subtitleReview.drafts};subtitleReview.memory.set(state.id,saved);
  try{localStorage.setItem(reviewStorageKey(),JSON.stringify(saved));subtitleReview.storageFailed=false;}catch{subtitleReview.storageFailed=true;}
}
function ensureReviewProject(){
  if(subtitleReview.project===state.id)return;
  if(subtitleReview.project)subtitleReview.memory.set(subtitleReview.project,{selected:[...subtitleReview.selected],drafts:subtitleReview.drafts});
  Object.assign(subtitleReview,{project:state.id,selected:new Set(),items:{},fetchKey:'',drafts:{}});subtitleReview.fetchGeneration++;
  try{const saved=subtitleReview.memory.get(state.id)||JSON.parse(localStorage.getItem(reviewStorageKey())||'{}');subtitleReview.selected=new Set((saved.selected||[]).filter(id=>state.rows.some(r=>r.id===id)));subtitleReview.drafts=saved.drafts||{};}catch{}
}
function reviewMeta(){const meta=state.project?.subtitle_review;return meta?.target_language===targetLanguage()?meta:null;}
function reviewStatus(id){return reviewMeta()?.item_statuses?.[id]||'';}
function reviewItem(id){const item=subtitleReview.items[id];return item&&reviewMeta()?.id===subtitleReview.request?item:null;}
function reviewMatches(row,item){return !!item&&['text','zh','start','end','speaker'].every(key=>row[key]===item[key]);}
function reviewDraftKey(item){return item.candidate_id||`${subtitleReview.request}:${item.id}`;}
function reviewDraft(item){return subtitleReview.drafts[reviewDraftKey(item)]||{};}
function eligibleReviewIds(){return [...subtitleReview.selected].filter(id=>reviewStatus(id)==='ready');}

const checksBeforeBatch=translationChecks;
translationChecks=function(row){
  const checks=checksBeforeBatch(row),item=reviewItem(row.id);
  return reviewStatus(row.id)==='failed'&&(!item||reviewMatches(row,item))?[...checks,{code:'batch_review_failed',message:tr('本轮处理失败，可修改后重试。')}]:checks;
};
const filterBeforeBatch=filteredRows;
filteredRows=function(){
  ensureReviewProject();const filter=$('#subtitle-filter').value;
  return filterBeforeBatch().filter(({r})=>filter==='candidates'?reviewStatus(r.id)==='ready':filter==='review-failed'?reviewStatus(r.id)==='failed':true);
};
function reviewSelection(ids,replace=false){ensureReviewProject();if(replace)subtitleReview.selected.clear();for(const id of ids)subtitleReview.selected.add(id);saveReviewLocal();renderSentenceList();}
$('#review-select-page').onclick=()=>reviewSelection(reviewVisibleIds());
$('#review-select-filtered').onclick=()=>reviewSelection(filteredRows().map(({r})=>r.id));
$('#review-select-issues').onclick=()=>{$('#subtitle-filter').value='checks';state.listPage=0;state.subtitleKey=null;reviewSelection(state.rows.filter(r=>translationChecks(r).length).map(r=>r.id),true);};
$('#review-clear-selection').onclick=()=>{subtitleReview.selected.clear();saveReviewLocal();renderSentenceList();};
$('#review-select-candidates').onclick=()=>reviewSelection(state.rows.filter(r=>reviewStatus(r.id)==='ready').map(r=>r.id),true);
function reviewVisibleIds(){const size=Number($('#subtitle-page-size').value);return filteredRows().slice(state.listPage*size,(state.listPage+1)*size).map(({r})=>r.id);}
async function startReviewBatch(ids,mode){
  ensureReviewProject();if(subtitleReview.submitting||active(state.project))return;
  if(!ids.length)throw Error(tr('请先勾选句子，或选择全部筛选结果。'));
  if(!['asr','source'].includes(mode))requireAppliedTarget();
  if(ids.some(id=>{const item=reviewItem(id);return item&&Object.keys(reviewDraft(item)).length;}))throw Error(tr('所选句子包含编辑过的候选，请先采用或还原候选后再重新处理。'));
  subtitleReview.submitting=true;renderReviewToolbar();
  try{
    const {id,version}=await saveProjectDraftsForAction();
    await api(`/api/projects/${id}/subtitle-review`,json('POST',{ids,mode,revision:state.editorRevision}));
    if(id!==state.id||version!==state.selectionVersion)return;
    subtitleReview.selected=new Set(ids);subtitleReview.fetchKey='';state.retranslationPending=null;saveReviewLocal();
    await refreshCurrent(false);toast(tr('批量处理已开始，候选会逐句显示在表格中，当前字幕保持不变。'));
  }finally{subtitleReview.submitting=false;renderReviewToolbar();}
}
$('#review-generate').onclick=()=>guard(()=>startReviewBatch([...subtitleReview.selected],$('#review-mode').value));
const singleBeforeWorkbench=startRetranslation;
startRetranslation=async function(id,mode='translate'){
  if(state.section==='projects'&&state.tab==='subtitles')return startReviewBatch([id],mode);
  return singleBeforeWorkbench(id,mode);
};
async function applyReviewIds(ids){
  if(!ids.length)throw Error(tr('所选句子没有可采用的候选。'));
  subtitleReview.applying=true;renderReviewToolbar();
  try{
    const {id,version}=await saveProjectDraftsForAction(),meta=reviewMeta();if(!meta)throw Error(tr('候选已经失效，请重新处理。'));
    // Reload unseen pages before submitting so restored cross-page draft edits
    // are included even when this browser session has not displayed those rows.
    for(let offset=0;offset<ids.length;offset+=100){
      const page=await api(`/api/projects/${id}/subtitle-review?ids=${ids.slice(offset,offset+100).join(',')}`);
      if(id!==state.id||version!==state.selectionVersion||page.id!==meta.id)throw Error(tr('候选已经失效，请重新处理。'));
      subtitleReview.request=page.id;Object.assign(subtitleReview.items,page.items);
    }
    const edits={};for(const cue of ids){const item=reviewItem(cue);if(item&&Object.keys(reviewDraft(item)).length)edits[cue]=reviewDraft(item);}
    const response=await api(`/api/projects/${id}/subtitle-review/apply`,json('POST',{revision:state.editorRevision,request_id:meta.id,ids,edits}));
    if(id!==state.id||version!==state.selectionVersion)return;
    for(const cue of response.adopted){const item=reviewItem(cue);if(item)delete subtitleReview.drafts[reviewDraftKey(item)];}
    saveReviewLocal();subtitleReview.fetchKey='';state.subtitleKey=null;state.boardKey=null;state.outputRenderKey=null;
    await refreshCurrent(true);toast(tr('已采用 {0} 句候选，其他字幕保持不变。',response.adopted.length));
  }finally{subtitleReview.applying=false;renderReviewToolbar();}
}
$('#review-apply').onclick=()=>guard(()=>applyReviewIds(eligibleReviewIds()));
function reviewRetryIds(){
  const unfinished=state.rows.filter(r=>['pending','failed'].includes(reviewStatus(r.id))).map(r=>r.id);
  const selected=unfinished.filter(id=>subtitleReview.selected.has(id));
  return selected.length?selected:unfinished;
}
$('#review-resume').onclick=()=>guard(async()=>{
  const {id,version}=await saveProjectDraftsForAction(),meta=reviewMeta();
  const ids=reviewRetryIds();
  if(!meta||!ids.length)return;
  await api(`/api/projects/${id}/subtitle-review/resume`,json('POST',{revision:state.editorRevision,request_id:meta.id,ids}));
  if(id===state.id&&version===state.selectionVersion)await refreshCurrent(false);
});
function renderReviewToolbar(){
  const onProject=state.section==='projects'&&!!state.projectDetail;
  document.body.classList.toggle('subtitle-workbench',onProject&&state.tab==='subtitles');
  document.body.classList.toggle('dubbing-workbench',onProject&&state.tab==='voices');
  if(!state.project)return;ensureReviewProject();const meta=reviewMeta(),busy=active(state.project)||state.saving||subtitleReview.submitting||subtitleReview.applying;
  $('#review-selected-count').textContent=tr('已选 {0} 句（跨页保留）',subtitleReview.selected.size);
  $('#subtitle-review-toolbar').classList.toggle('has-selection',subtitleReview.selected.size>0);
  $('#review-generate').textContent=tr('处理所选 {0} 句',subtitleReview.selected.size);$('#review-generate').disabled=busy||!subtitleReview.selected.size;
  const ready=eligibleReviewIds().length;$('#review-apply').textContent=tr('采用所选候选 {0} 句',ready);$('#review-apply').disabled=busy||!ready;
  $('#review-mode').disabled=busy;$('#review-select-candidates').disabled=!meta?.ready;
  $('#review-summary').textContent=meta?tr('本轮 {0}/{1} · 候选 {2} · 失败 {3} · 已采用 {4}',meta.done,meta.total,meta.ready,meta.failed,meta.applied):tr('勾选多句后统一处理，结果直接在原文和译文下方对照。');
  if(subtitleReview.storageFailed&&Object.keys(subtitleReview.drafts).length)$('#review-summary').textContent=tr('浏览器无法保存候选草稿，刷新前请先采用或复制保存。');
  const unfinished=meta&&Object.values(meta.item_statuses).some(s=>['pending','failed'].includes(s));$('#review-resume').hidden=!unfinished;$('#review-resume').disabled=busy;
  const retry=reviewRetryIds(),selectedRetry=retry.some(id=>subtitleReview.selected.has(id));
  $('#review-resume').textContent=tr(selectedRetry?'重试所选未完成（{0} 句）':'重试全部未完成（{0} 句）',retry.length);
  if(meta)for(const [value,label] of [['candidates',tr('候选待采用')],['review-failed',tr('本轮处理失败')]]){const option=$('#subtitle-filter').querySelector(`option[value="${value}"]`);option.textContent=label;}
}
function candidateMarkup(row,item){
  if(!item)return '';
  if(!reviewMatches(row,item)&&item.status!=='applied')return `<p class="review-stale">${esc(tr('本句已修改，旧候选不能采用，请重新处理。'))}</p>`;
  if(item.status==='failed')return `<p class="review-failure" role="status">${esc(item.error||tr('本轮处理失败，可修改后重试。'))}</p>`;
  if(item.status==='pending')return `<p class="review-pending">${esc(tr(item.audio_repair?.status==='pending'?'等待自动回听修复':item.audio_repair?.status==='done'?'原声已重新识别，正在生成译文':'等待生成候选'))}</p>`;
  if(item.status==='unchanged')return `<p class="review-pending">${esc(tr('校对完成，无需修改'))}</p>`;
  if(item.status!=='ready')return '';
  const draft=reviewDraft(item),source=['asr','source','both'].includes(item.mode),clear=['asr','source'].includes(item.mode)&&(draft.text??item.after_source)!==item.text;
  return {source:source?`<div class="review-candidate"><label>${esc(tr('建议原文'))}<textarea rows="2" dir="auto" data-candidate-id="${row.id}" data-candidate-field="text" aria-label="${esc(tr('第 {0} 句建议原文',row.id))}">${esc(draft.text??item.after_source)}</textarea></label></div>`:'',
    translation:`<div class="review-candidate">${clear?`<p>${esc(tr('采用新原文时清空旧译文，请重新翻译。'))}</p>`:`<label>${esc(tr('建议译文'))}<textarea rows="2" dir="auto" data-candidate-id="${row.id}" data-candidate-field="zh" aria-label="${esc(tr('第 {0} 句建议译文',row.id))}">${esc(draft.zh??item.after)}</textarea></label>`}<p class="review-reason">${esc([item.reason,...(item.checks||[]).map(translationCheckText)].filter(Boolean).join('；'))}</p><div class="cue-refine-tools"><button type="button" class="text-button" data-adopt-review="${row.id}">${esc(tr('采用本句'))}</button>${Object.keys(draft).length?`<button type="button" class="text-button" data-reset-candidate="${row.id}">${esc(tr('还原候选'))}</button>`:''}</div></div>`};
}
function enhanceReviewRows(){
  if(!state.project)return;ensureReviewProject();const busy=active(state.project)||state.saving||subtitleReview.applying;
  for(const trEl of $$('#segment-list tr')){
    const input=trEl.querySelector('[data-review-field="text"]');if(!input)continue;
    const row=state.rows[Number(input.dataset.index)];if(!row)continue;let select=trEl.querySelector('[data-review-check]');
    if(!select){const td=document.createElement('td');td.className='review-selection';td.innerHTML=`<input type="checkbox" data-review-check="${row.id}" aria-label="${esc(tr('选择第 {0} 句',row.id))}">`;trEl.prepend(td);select=td.firstElementChild;}
    select.checked=subtitleReview.selected.has(row.id);
    const item=reviewItem(row.id),signature=JSON.stringify([item,reviewDraft(item||{}),reviewMeta()?.id,I18N.locale()]);
    if(trEl.dataset.reviewSignature!==signature&&!trEl.contains(document.activeElement?.closest('[data-candidate-field]'))){
      trEl.querySelectorAll('.review-candidate,.review-row-status').forEach(el=>el.remove());
      const markup=candidateMarkup(row,item),sourceCell=input.parentElement,translationCell=trEl.querySelector('[data-review-field="zh"]').parentElement;
      if(typeof markup==='string'){if(markup)translationCell.insertAdjacentHTML('beforeend',`<div class="review-row-status">${markup}</div>`);}
      else{sourceCell.insertAdjacentHTML('beforeend',markup.source);translationCell.insertAdjacentHTML('beforeend',markup.translation);}
      trEl.dataset.reviewSignature=signature;
      const status=reviewStatus(row.id),cue=trEl.querySelector('.review-cue');cue.querySelector('.review-candidate-badge')?.remove();
      if(status){const labels={ready:tr('候选待采用'),failed:tr('本轮处理失败'),pending:tr('等待生成候选'),applied:tr('已采用'),unchanged:tr('无需修改')};cue.insertAdjacentHTML('beforeend',`<span class="badge review-candidate-badge status-${status}">${esc(labels[status]||status)}</span>`);}
    }
    trEl.querySelectorAll('[data-candidate-field],[data-reset-candidate]').forEach(el=>el.disabled=state.saving||subtitleReview.applying);
    trEl.querySelectorAll('[data-adopt-review]').forEach(el=>el.disabled=busy);
    trEl.querySelectorAll('[data-retranslate]').forEach(el=>{if(el.textContent===tr('查看本句校对建议'))el.hidden=true;});
  }
  $$('#segment-list textarea').forEach(growTextarea);
  renderReviewToolbar();
}
async function fetchReviewPage(){
  if(!state.project||state.section!=='projects'||state.tab!=='subtitles')return;
  const meta=reviewMeta(),ids=reviewVisibleIds();if(!meta||!ids.length)return;
  const key=JSON.stringify([state.id,meta.id,meta.version,ids]);if(key===subtitleReview.fetchKey)return;subtitleReview.fetchKey=key;
  const id=state.id,version=state.selectionVersion,generation=++subtitleReview.fetchGeneration;
  try{
    const result=await api(`/api/projects/${id}/subtitle-review?ids=${ids.join(',')}`);
    if(id!==state.id||version!==state.selectionVersion||generation!==subtitleReview.fetchGeneration||result.id!==reviewMeta()?.id)return;
    if(subtitleReview.request!==result.id){subtitleReview.items={};subtitleReview.request=result.id;}
    Object.assign(subtitleReview.items,result.items);enhanceReviewRows();
  }catch(error){if(generation===subtitleReview.fetchGeneration){subtitleReview.fetchKey='';$('#review-summary').textContent=error.message;}}
}
const listBeforeWorkbench=renderSentenceList;
renderSentenceList=function(force=false){listBeforeWorkbench(force);enhanceReviewRows();window.SubtitleTools?.render();void fetchReviewPage();};
const renderBeforeWorkbench=render;
render=function(full=false){renderBeforeWorkbench(full);renderReviewToolbar();void fetchReviewPage();};
document.addEventListener('change',event=>{const el=event.target;if(!el.matches('[data-review-check]'))return;const id=Number(el.dataset.reviewCheck);if(el.checked)subtitleReview.selected.add(id);else subtitleReview.selected.delete(id);saveReviewLocal();renderReviewToolbar();});
document.addEventListener('input',event=>{
  const el=event.target;if(!el.matches('[data-candidate-field]'))return;const item=reviewItem(Number(el.dataset.candidateId));if(!item)return;
  const key=reviewDraftKey(item);subtitleReview.drafts[key]={...subtitleReview.drafts[key],[el.dataset.candidateField]:el.value};saveReviewLocal();growTextarea(el);
});
document.addEventListener('click',event=>{
  const adopt=event.target.closest('[data-adopt-review]');if(adopt&&!adopt.disabled){guard(()=>applyReviewIds([Number(adopt.dataset.adoptReview)]));return;}
  const reset=event.target.closest('[data-reset-candidate]');if(reset&&!reset.disabled){const item=reviewItem(Number(reset.dataset.resetCandidate));delete subtitleReview.drafts[reviewDraftKey(item)];saveReviewLocal();reset.closest('tr').dataset.reviewSignature='';enhanceReviewRows();}
});
new ResizeObserver(entries=>{$('#panel-subtitles').style.setProperty('--review-bulk-height',entries[0].target.offsetHeight+'px');}).observe($('#subtitle-review-toolbar'));

document.addEventListener('focusout',event=>{if(event.target.matches('[data-candidate-field]'))requestAnimationFrame(()=>enhanceReviewRows());});

const sectionBeforeWorkbench=showSection;showSection=function(name){sectionBeforeWorkbench(name);renderReviewToolbar();};
const tabBeforeWorkbench=switchTab;switchTab=function(name){tabBeforeWorkbench(name);renderReviewToolbar();if(name==='subtitles')renderSentenceList();};

window.addEventListener('beforeunload',event=>{if(subtitleReview.storageFailed&&Object.keys(subtitleReview.drafts).length){event.preventDefault();event.returnValue='';}});

const growBeforeWorkbench=growTextarea;growTextarea=function(el){if(el.matches('[data-review-field],[data-candidate-field]')){el.style.height='0px';const min=parseFloat(getComputedStyle(el).minHeight)||56;el.style.height=Math.round(Math.max(min,el.scrollHeight+2))+'px';}else growBeforeWorkbench(el);};
