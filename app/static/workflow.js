'use strict';
// Task dispatch, draft saving and navigation share the same selection token.
const projectActions=ProjectWorkflow.coordinator({state,saveText:()=>saveSegments(),saveAudio:()=>saveAudioOptions(),message:tr});
function renderWorkflow(){
  const p=state.project;if(!p)return;
  renderAlignmentSummary();
  const rows=state.rows||p.segments,translated=rows.filter(r=>r.zh?.trim()).length;
  const missing=rows.length-translated,unassigned=rows.filter(r=>r.speaker===UNASSIGNED_SPEAKER).length;
  const ready=rows.length>0&&!missing,busy=active(p)||state.saving||!!state.projectUnavailable;
  const canResume=ProjectWorkflow.canResumeSegmentation(p);
  const status={source:rows.length?tr('{0} 句原文',rows.length):tr('等待识别'),subtitles:rows.length?tr('{0}/{1} 句译文',translated,rows.length):tr('等待原文'),voices:unassigned?tr('{0} 句未分配角色',unassigned):p.outputs.audio?tr('已生成整片配音'):tr('先试听一句'),export:p.outputs.preview?tr('可以观看'):tr('等待生成成片')};
  Object.entries(status).forEach(([key,value])=>$('#step-'+key+'-state').textContent=value);
  $$('.tab').forEach(e=>e.classList.toggle('complete',({source:!!rows.length,subtitles:ready,voices:!!p.outputs.audio,export:!!p.outputs.preview})[e.dataset.tab]));
  $('#workflow-source-summary').textContent=tr('原声 {0} · {1} 句原文',languageName(p.language),rows.length);
  $('#segment-count').textContent=tr('目标 {0} · {1} 句待翻译',languageName(targetLanguage(p)),missing)+' · '+tr('{0}/{1} 句译文',translated,rows.length);
  $('#workflow-voices-summary').textContent=tr('配音 {0} · {1} 个角色 · {2} 句未分配',languageName(targetLanguage(p)),projectSpeakers().length,unassigned);
  $('#workflow-export-summary').textContent=state.dirty?tr('台词或表达有未保存修改，当前预览仍是旧版本；保存并重新配音后再导出。'):p.outputs.audio?tr('整片配音已就绪，设置背景音后生成成片。'):tr('先完成上一阶段的整片配音，再混音导出。');
  $('#workflow-need-source').hidden=!!rows.length;
  $('#workflow-need-translation').hidden=ready;
  $('#workflow-need-audio').hidden=!!p.outputs.audio&&!state.dirty;
  $('#run-dub').disabled=busy||!ready||!!unassigned||!!state.targetSelectionDirty;
  $('#run-translate').disabled=busy||!missing||!rows.length;
  $('#run-retranslate_all').disabled=busy||!translated;
  $('#run-analyze').hidden=!!rows.length||canResume;
  $('#source-state-title').textContent=rows.length?tr('原文字幕已准备'):tr('先生成原文字幕');
  $('#source-actions-note').textContent=rows.length?tr('可进入下一步翻译校对；需要重建时间轴时使用重新断句，或导入替换字幕。'):tr('识别讲话内容，再按完整句意分段。已有字幕可以直接导入。');
  $('#panel-source .source-reprocess').hidden=!rows.length;
  $('#run-resegment').disabled=busy||!rows.length;
  $('#run-optimize').disabled=busy||!translated;
  $('#run-export').disabled=busy||!p.outputs.audio||state.dirty||!!state.targetSelectionDirty;
  $('#open-storage').disabled=busy;
  $('#export-source-srt').disabled=!rows.length||!!state.projectUnavailable;
  $('#export-translation-srt').disabled=!translated||!!state.projectUnavailable;
  $('#subtitle-export-note').textContent=tr('导出全部句子的当前编辑稿，含未保存修改；未采用候选不导出。')+(missing?' '+tr('空白译文将跳过（{0} 句）。',missing):'');
  const voicesReady=projectSpeakers().every(s=>!!p.references?.[s])||state.modelBackendSettings?.tts_provider==='openai_speech';
  const next=canResume?{stage:'resegment',label:'继续断句'}:ProjectWorkflow.nextAction(p,rows,state.tab,voicesReady&&!unassigned);
  const button=$('#next-action');
  button.hidden=!next&&!state.dirty&&!state.audioDirty;
  button.textContent=state.dirty||state.audioDirty?tr('保存全部草稿'):next?tr(next.label,next.count??''):'';
  button.disabled=busy;button.setAttribute('aria-busy',String(state.saving));
  button.onclick=()=>guard(async()=>{
    if(state.dirty||state.audioDirty){await projectActions.prepare({audio:true});renderWorkflow();toast(tr('全部草稿已保存'));}
    else if(next){if(next.tab)switchTab(next.tab);else if(next.download)downloadSubtitleDraft(next.download);else if(canResume)await runResegment();else if(next.stage)await run(next.stage);}
  });
  // Expand incomplete role setup once on entry, then respect the user's choice.
  if(state.workflowRoleProject!==p.id){state.workflowRoleProject=p.id;$('#voice-references').open=projectSpeakers().some(s=>!p.references?.[s])||!projectSpeakers().length;}
  if(typeof renderProjectExperience==='function')renderProjectExperience();
}
document.addEventListener('click',event=>{
  const link=event.target.closest('[data-workflow-go]');if(!link)return;
  const target=link.dataset.workflowGo;
  if(link.dataset.currentCue!==undefined&&state.rows.length){
    $('#segment-search').value='';$('#subtitle-filter').value='all';
    state.subtitlePage=Math.floor(state.selected/Number($('#subtitle-page-size').value||20));state.subtitleKey=null;
  }
  switchTab(target);
  if(link.dataset.currentCue!==undefined){$('#single-voice-editor').close();requestAnimationFrame(()=>$('#segment-list tr.current')?.scrollIntoView({block:'center',behavior:'instant'}));}
});

function storageBytes(value){const power=value>=1024**3?3:value>=1024**2?2:value>=1024?1:0;return new Intl.NumberFormat(I18N.locale(),{maximumFractionDigits:1}).format(value/1024**power)+' '+['B','KiB','MiB','GiB'][power];}
let storageContext=null,storagePlan=null,storageBusy=false,storageGeneration=0;
function invalidateStoragePreview(){
  storageGeneration++;storagePlan=null;$('#storage-preview-files').textContent='';$('#storage-preview-summary').textContent='';$('#storage-clean').disabled=true;
}
function renderStorage(data){
  const labels={source:tr('原始素材'),references:tr('角色参考音'),results:tr('当前结果与字幕'),cache:tr('缓存'),history:tr('历史备份'),other:tr('元数据与其他')};
  $('#storage-summary').textContent=tr('项目文件逻辑大小 {0}；硬链接备份可能共享磁盘空间。',storageBytes(data.bytes));
  $('#storage-totals').innerHTML=Object.entries(data.totals).map(([key,value])=>`<div><span>${esc(labels[key])}</span><b>${esc(storageBytes(value))}</b></div>`).join('');
  for(const [key,item] of Object.entries(data.categories)){$(`[data-storage-size="${key}"]`).textContent=tr('{0} 个文件 · {1}',item.count,storageBytes(item.bytes));$(`[data-storage-category="${key}"]`).disabled=!item.count;}
  $('#storage-links-warning').hidden=!data.skipped;
}
$('#open-storage').onclick=()=>guard(async()=>{
  const context=projectActions.context();if(!projectActions.current(context))return;
  storageContext=context;invalidateStoragePreview();$('#storage-error').textContent='';$('#storage-result').textContent='';
  $$('[data-storage-category]').forEach(e=>{e.checked=false;});
  const data=await api(`/api/projects/${context.id}/storage`);if(!projectActions.current(context))return;
  renderStorage(data);openCueDialog($('#storage-dialog'));
});
$$('[data-storage-category]').forEach(e=>e.onchange=invalidateStoragePreview);
$('#storage-preview').onclick=()=>guard(async()=>{
  if(storageBusy)return;storageBusy=true;$('#storage-preview').disabled=true;$('#storage-error').textContent='';
  invalidateStoragePreview();$('#storage-preview-summary').textContent=tr('正在准备清理预览…');
  const generation=storageGeneration;
  try{
    const context=await projectActions.prepare({audio:true});
    if(context.id!==storageContext?.id||context.version!==storageContext.version)throw Error(tr('项目已切换，请重新操作'));
    const categories=$$('[data-storage-category]:checked').map(e=>e.dataset.storageCategory);
    if(!categories.length)throw Error(tr('请先选择清理范围'));
    const revision=state.editorRevision,plan=await api(`/api/projects/${context.id}/storage/preview`,json('POST',{categories,revision}));
    if(!projectActions.current(context)||generation!==storageGeneration)return;
    storagePlan={...plan,revision,categories};
    $('#storage-preview-summary').textContent=tr('将永久删除 {0} 个文件，逻辑大小 {1}。请核对下列路径；预览十分钟内有效。',plan.count,storageBytes(plan.bytes));
    $('#storage-preview-files').textContent=plan.files.map(item=>`${item.path}  (${storageBytes(item.bytes)})`).join('\n');
    $('#storage-clean').disabled=!plan.count;
  }catch(error){$('#storage-preview-summary').textContent='';$('#storage-error').textContent=error.message;}finally{storageBusy=false;$('#storage-preview').disabled=false;}
});
$('#storage-clean').onclick=()=>guard(async()=>{
  if(storageBusy||!storagePlan)return;
  if(!projectActions.current(storageContext)||state.dirty||state.audioDirty){invalidateStoragePreview();$('#storage-error').textContent=tr('草稿或项目已变化，请保存后重新预览');return;}
  const plan=storagePlan,context=storageContext;storageBusy=true;$('#storage-clean').disabled=true;$('#storage-preview').disabled=true;
  try{
    const result=await api(`/api/projects/${context.id}/storage/clean`,json('POST',{token:plan.token,revision:plan.revision}));
    if(!projectActions.current(context))return;
    invalidateStoragePreview();renderStorage(result.inventory);
    $('#storage-result').textContent=tr('已清理 {0} 个文件，逻辑大小 {1}。',result.removed,storageBytes(result.bytes));
    $('#storage-error').textContent=result.error;
  }catch(error){invalidateStoragePreview();$('#storage-error').textContent=error.message;}finally{storageBusy=false;$('#storage-preview').disabled=false;}
});
$('#storage-dialog').addEventListener('cancel',event=>{if(storageBusy)event.preventDefault();});
$('#storage-close').onclick=()=>{if(!storageBusy)$('#storage-dialog').close();};
$$('[data-target-select]').forEach(select=>select.addEventListener('change',renderWorkflow));
if(state.project)renderWorkflow();


// Export an in-memory snapshot. Never save drafts, invalidate audio or contact a model.
function downloadSubtitleDraft(kind){
  if(!state.project||state.projectUnavailable)return;
  let result;
  try{result=ProjectWorkflow.subtitleDownload(state.rows,kind,state.project);}
  catch(error){if(error.key)throw Error(tr(error.key,...error.args));throw error;}
  const url=URL.createObjectURL(new Blob([result.text],{type:'application/x-subrip;charset=utf-8'}));
  const link=document.createElement('a');link.href=url;link.download=result.filename;
  document.body.append(link);link.click();link.remove();
  setTimeout(()=>URL.revokeObjectURL(url),30000);
  toast(result.skipped?tr('已导出 {0} 句译文字幕，跳过 {1} 句空白译文。',result.count,result.skipped):tr('已导出 {0} 句字幕。',result.count));
}
$('#export-source-srt').onclick=()=>guard(()=>downloadSubtitleDraft('source'));
$('#export-translation-srt').onclick=()=>guard(()=>downloadSubtitleDraft('translation'));


function alignmentProblemIds(){return new Set((state.project?.alignment_report?.conflicts||[]).map(item=>item.id));}
function renderAlignmentSummary(){
  const report=state.project?.alignment_report;
  if(state.alignmentProject!==state.id){state.alignmentProject=state.id;state.alignmentOnly=false;$('#alignment-only').checked=false;}
  $('#alignment-summary').hidden=!report;if(!report)return;
  const blocked=report.status==='blocked',limits=report.effective||report.normal;
  const stale=report.revision!==state.editorRevision||state.dirty||(report.status==='ready'&&!state.project.outputs.audio);
  $('#alignment-summary-title').textContent=stale?tr('上次对齐结果，当前修改后需重新检查'):blocked?tr('语音已保留，{0} 句需要调整时间',report.conflicts.length):report.status==='ready'?tr('时间对齐已完成'):tr('正在对齐已生成语音');
  $('#alignment-summary-text').textContent=limits?tr('本轮上限 {0} 倍速 · 提前 {1} 秒 · 延后 {2} 秒；收紧 {3} 段两端静音，共 {4} 秒。',limits.speed,limits.lead,limits.lag,report.trimmed_count||0,Number(report.trimmed_seconds||0).toFixed(1)):'';
  $('#alignment-only').disabled=!report.conflicts?.length;
  if(!report.conflicts?.length){state.alignmentOnly=false;$('#alignment-only').checked=false;}
}
$('#alignment-only').onchange=event=>{state.alignmentOnly=event.target.checked;state.voicePage=0;state.boardKey=null;renderVoiceBoard(true);};
