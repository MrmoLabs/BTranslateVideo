"use strict";
function measuredPercent(progress){
  return typeof progress?.done==='number'&&typeof progress.total==='number'&&Number.isFinite(progress.done)&&Number.isFinite(progress.total)&&progress.total>0
    ?Math.min(100,Math.max(0,100*progress.done/progress.total)):null;
}
function displayProgressPercent(progress){return progress.status==='done'?100:measuredPercent(progress);}
function elapsedSeconds(progress,now=Date.now()/1000,phase=false){
  const start=phase?progress.phase_started_at:progress.started_at??progress.queued_at;
  return start!==null&&start!==undefined?Math.max(0,(progress.finished_at??now)-start):0;
}
function progressDuration(seconds){
  if(!Number.isFinite(seconds))return '—';
  const value=Math.max(0,Math.floor(seconds)),h=Math.floor(value/3600),m=Math.floor(value/60)%60,s=value%60;
  return (h?h+':'+String(m).padStart(2,'0'):String(m))+':'+String(s).padStart(2,'0');
}
if(typeof document==='undefined'){
  module.exports={measuredPercent,displayProgressPercent,elapsedSeconds,progressDuration};
}else{
const progressUI={items:[],active:0,loading:false,offset:0,lastFetch:0,keys:new Map(),openEvents:new Set(),pending:new Set(),logs:new Map(),logsOpen:new Set(),logsRevision:0};
const progressActive=p=>['queued','running','cancelling'].includes(p.status);
const progressStepLabels={resegment:'重新断句',retranscribe:'重新识别原声',check_asr:'短音频试识别',analyze:'识别与断句',translate:'翻译与审校',dub:'生成配音',export:'合成与导出',preview:'页面预览',optimize:'译文优化',retranslate_all:'重新翻译全部',audition:'单句试听',retranslate:'单句校对',subtitle_review:'批量字幕校对',reference:'原片取样',diarize:'识别说话人',model:'加载配音模型',audiobook:'章节配音',book_export:'合并全书'};
const progressPhaseLabels={review_candidates:'批量生成校对候选',review_audio:'准备所选原声',cue_review:'生成校对建议',translation_review_required:'等待人工校对',diarization_audio:'准备说话人音频',diarization_wait:'等待说话人识别设备',diarization_load:'加载说话人模型',diarization_segmentation:'检测人声与切换',diarization_embeddings:'提取说话人声纹',diarization_clustering:'聚类区分说话人',diarization_discrete_diarization:'对齐说话人时间轴',diarization_speaker_counting:'估算说话人数',diarization_match:'匹配字幕角色',diarization_references:'准备角色参考音',diarization_review:'核对说话人分配',diarization_compute:'分析说话人',reference_extract:'提取角色参考音',audio_extract:'提取音频',asr_prepare:'检测语音与语言',asr_load:'加载识别模型',asr_decode:'识别原文',asr_service:'等待本地识别服务',segmentation_model:'准备断句模型',segmentation:'语义断句',segmentation_review:'复核字幕边界',translation_batches:'翻译与语义审校',translation_review:'审校优化建议',tts_generate:'生成配音',tts_cached:'复用已有配音',tts_service:'本地服务生成配音',audio_preflight:'预检已有配音时间',audio_prepare:'检查语音两端静音',alignment_plan:'规划配音时间轴',audio_align:'音频时间对齐',audio_merge:'合并配音音轨',background_separate:'分离原片背景音',background_mix:'混合背景音',video_mux:'封装视频与字幕',preview_encode:'编码页面预览',chapter_prepare:'准备章节音色',chapter_export_wav:'合并章节 WAV',chapter_export_mp3:'编码章节 MP3',book_export:'合并全书音频',model_load:'准备配音模型',queued:'排队等待'};
const unitNames={chunks:'批',segments:'句',words:'词',seconds:'秒',chapters:'章'};
function phaseTitle(p){const phase=p.phase||'';return tr(!progressActive(p)&&['check_asr','retranscribe'].includes(p.action)?progressStepLabels[p.action]:progressPhaseLabels[phase]||progressStepLabels[phase]||phase.replace(/^tts:|^book:/,''));}
function progressNumber(value){return Number.isInteger(value)?String(value):Number(value).toFixed(1);}
function taskKey(task){return task.kind+':'+task.id;}
function currentTask(key){return progressUI.items.find(t=>taskKey(t)===key)||(state.project?.processing&&key==='video:'+state.id?{kind:'video',id:state.id,title:state.project.name,error:state.project.error,processing:state.project.processing}:novels.book?.job?.processing&&key==='audiobook:'+novels.book.id?{kind:'audiobook',id:novels.book.id,title:novels.book.title,error:novels.book.job.error,processing:novels.book.job.processing}:null);}
function nowProgress(){return (Date.now()+progressUI.offset)/1000;}
function clockMarkup(p,phase=false){return `<span data-progress-clock="${phase?'phase':'task'}" data-progress-start="${phase?p.phase_started_at||'':p.started_at||p.queued_at}" data-progress-finish="${p.finished_at||''}">${progressDuration(elapsedSeconds(p,nowProgress(),phase))}</span>`;}
function progressBar(p){
  const percent=displayProgressPercent(p),known=percent!==null,moving=!known&&['running','cancelling'].includes(p.status);
  return `<div class="task-meter ${moving?'indeterminate':''}" role="progressbar" aria-label="${esc(tr('当前阶段进度'))}" ${known?`aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent.toFixed(1)}"`:`aria-valuetext="${esc(tr('该阶段未提供细分进度'))}"`}><span style="width:${known?percent:moving?30:0}%"></span></div>`;
}
function resumeSegmentationButton(task){
  if(task.kind!=='video'||task.id!==state.id||task.processing.id!==state.project?.processing?.id
      ||!ProjectWorkflow.canResumeSegmentation(state.project))return '';
  const disabled=state.saving||!!state.projectUnavailable;
  return `<button type="button" data-resume-segmentation="${esc(task.processing.id)}" ${disabled?'disabled':''}>${esc(tr('继续断句'))}</button>`;
}
function taskCard(task){
  const p=task.processing,active=progressActive(p),known=displayProgressPercent(p),key=taskKey(task),pending=progressUI.pending.has(key);
  const count=p.status==='done'&&p.phase!=='translation_review_required'?tr('全部阶段已完成'):known===null?tr(p.status==='queued'?'等待前方任务':'该阶段未提供细分进度'):tr('{0} / {1} {2}',progressNumber(p.done),progressNumber(p.total),tr(unitNames[p.unit]||p.unit));
  const queued=p.status==='queued',status=tr(p.phase==='translation_review_required'?'待人工处理':labels[p.status]||p.status);
  const eta=p.eta_seconds&&active&&p.status!=='queued'?tr('约 {0}（仅当前阶段）',progressDuration(p.eta_seconds)):tr('样本不足，暂不估算');
  const message=I18N.locale()==='zh-CN'?p.message:tr(p.message)===p.message?phaseTitle(p):tr(p.message);
  return `<article class="task-progress-card task-${esc(p.status)}" data-task-key="${esc(key)}"><div class="task-card-heading"><div><span class="task-kind">${esc(tr(task.kind==='video'?'视频译制':task.kind==='audiobook'?'小说有声书':'配音模型'))}</span><h3>${esc(task.kind==='model'?tr('配音模型'):task.title)}</h3></div><span class="task-state"><i aria-hidden="true"></i>${esc(status)}</span><strong class="task-percent"><small>${esc(tr(p.status==='done'?'任务完成':'当前阶段'))}</small>${known===null?'—':Math.floor(known)+'%'}</strong></div>
  <ol class="task-steps">${p.steps.map(step=>`<li class="${p.completed_steps.includes(step)?'complete':step===p.step?'current':''}" ${step===p.step?'aria-current="step"':''}>${esc(tr(progressStepLabels[step]||step))}</li>`).join('')}</ol>
  <div class="task-phase"><b>${esc(queued?tr('排队等待'):phaseTitle(p))}</b><span>${esc(count)}</span></div>${progressBar(p)}
  <p class="task-message">${esc(queued?tr('前方还有 {0} 个任务',task.queue_ahead||0):message||phaseTitle(p))}</p>
  ${p.current?`<p class="task-context">${esc(tr('当前内容：{0}',p.current))}</p>`:''}
  <dl class="task-metrics"><div><dt>${esc(tr(queued?'排队耗时':'任务耗时'))}</dt><dd>${clockMarkup(p)}</dd></div><div><dt>${esc(tr('本阶段耗时'))}</dt><dd>${p.phase_started_at?clockMarkup(p,true):'—'}</dd></div><div><dt>${esc(tr('片段复用'))}</dt><dd>${esc(p.reused?tr('{0} 句',p.reused):'—')}</dd></div><div><dt>${esc(tr('本阶段预计剩余'))}</dt><dd class="task-eta" data-eta-updated="${p.updated_at}" data-eta-active="${active}">${esc(eta)}</dd></div></dl>
  ${task.overall_total?`<p class="task-overall">${esc(tr('本次任务已完成：{0} / {1} {2}',task.overall_done||0,task.overall_total,tr(unitNames[task.overall_unit]||task.overall_unit)))}</p>`:''}
  <p class="task-wait-note" data-progress-wait="${p.updated_at}" data-progress-status="${p.status}" hidden></p><div class="task-progress-footer"><small>${esc(tr('最近进展'))} <span data-progress-age="${p.updated_at}" data-progress-finish="${p.finished_at||''}"></span></small>${active?`<button class="text-button" data-cancel-progress="${esc(key)}" ${pending||p.status==='cancelling'?'disabled':''}>${esc(tr(pending||p.status==='cancelling'?'正在取消…':'取消任务'))}</button>`:resumeSegmentationButton(task)}</div>
  ${task.kind==='video'&&p.network_translation?`<button type="button" class="text-button" data-task-translation-requests="${esc(key)}">${esc(tr('查看网络翻译请求'))}</button>`:''}
  ${task.error?`<p class="error task-error-summary">${esc(task.error.split('\n')[0].slice(0,200))}</p><details><summary>${esc(tr('错误详情'))}</summary><pre class="task-error">${esc(task.error)}</pre></details>`:''}
  <details class="task-events" data-task-events="${esc(key)}" ${progressUI.openEvents.has(key)?'open':''}><summary>${esc(tr('阶段进展记录'))}</summary><ol>${p.events.map(event=>`<li><time>${esc(new Date(event.time*1000).toLocaleTimeString(I18N.locale(),{hour12:false}))}</time><span>${esc(tr(event.message))}</span></li>`).join('')}</ol></details><details class="task-diagnostic" data-task-log-details="${esc(key)}" ${progressUI.logsOpen.has(key)?'open':''}><summary>${esc(tr('诊断日志'))}</summary><button type="button" data-task-load-log="${esc(key)}">${esc(tr('读取诊断日志'))}</button><pre>${esc(progressUI.logs.get(key)?.run===p.id?progressUI.logs.get(key).content:'')}</pre></details></article>`;
}
// Embedded progress is a summary; the task monitor retains the full diagnostics.
function compactTaskCard(task){
  const p=task.processing,key=taskKey(task),active=progressActive(p),percent=displayProgressPercent(p);
  const count=typeof p.done==='number'&&p.total>0?tr('{0} / {1} {2}',progressNumber(p.done),progressNumber(p.total),tr(unitNames[p.unit]||p.unit)):'';
  const eta=active&&p.eta_seconds&&p.status!=='queued'?`<span class="task-inline-eta" data-eta-updated="${p.updated_at}" data-eta-active="true">${esc(tr('预计还需 {0}',progressDuration(p.eta_seconds)))}</span>`:'';
  const note=task.error?.split('\n')[0]||(p.phase==='translation_review_required'?tr('部分句子已跳过，请在字幕校对中筛选“译文待核对”。'):'')||(p.status==='queued'?tr('前方还有 {0} 个任务',task.queue_ahead||0):p.current?tr('当前内容：{0}',p.current):'');
  return `<article class="task-progress-card task-progress-compact task-${esc(p.status)}" data-task-key="${esc(key)}"><div class="task-inline-heading"><span class="task-state"><i aria-hidden="true"></i>${esc(tr(p.phase==='translation_review_required'?'待人工处理':labels[p.status]||p.status))}</span><b class="task-inline-phase">${esc(phaseTitle(p))}</b>${count?`<span class="task-inline-count">${esc(count)}${percent!==null&&active?' · '+Math.floor(percent)+'%':''}</span>`:''}<span class="task-inline-time">${esc(tr(p.status==='queued'?'已等待':'耗时'))} ${clockMarkup(p)}</span>${eta}<div class="task-inline-actions">${resumeSegmentationButton(task)}<button type="button" class="text-button" data-open-progress="${esc(key)}">${esc(tr('详情与日志'))}</button>${active?`<button type="button" class="text-button" data-cancel-progress="${esc(key)}" ${progressUI.pending.has(key)||p.status==='cancelling'?'disabled':''}>${esc(tr(p.status==='cancelling'?'正在取消…':'取消任务'))}</button>`:''}</div></div>${active?progressBar(p):''}${note?`<p class="task-inline-note ${task.error?'error':''}" title="${esc(note)}">${esc(note)}</p>`:''}</article>`;
}
function updateProgressClocks(){
  const now=nowProgress();
  for(const node of $$('[data-progress-clock]')){const end=Number(node.dataset.progressFinish)||now;node.textContent=progressDuration(Math.max(0,end-Number(node.dataset.progressStart)));}
  for(const node of $$('[data-eta-updated]'))if(node.dataset.etaActive==='true'&&now-Number(node.dataset.etaUpdated)>30)node.textContent=tr('进展尚未更新，暂不估算');
  for(const node of $$('[data-progress-wait]')){
    node.hidden=node.dataset.progressStatus!=='running'||now-Number(node.dataset.progressWait)<30;
    node.textContent=tr('当前阶段尚无新进展；可查看诊断日志，或取消任务。');
  }
  for(const node of $$('[data-progress-age]')){
    const finish=Number(node.dataset.progressFinish),age=Math.max(0,(finish||now)-Number(node.dataset.progressAge));
    node.textContent=finish?tr('任务已结束'):tr('{0} 前',progressDuration(age));
  }
}
function mountTaskCards(node,tasks,compact=false){
  const key=JSON.stringify([tasks,compact,I18N.locale(),[...progressUI.pending],progressUI.logsRevision,state.id,state.project?.processing?.id,state.project?.status,state.saving,!!state.projectUnavailable]);if(progressUI.keys.get(node.id)===key)return;
  const focus= node.contains(document.activeElement)?document.activeElement?.dataset:null;
  node.innerHTML=tasks.map(compact?compactTaskCard:taskCard).join('');progressUI.keys.set(node.id,key);
  if(focus){const button=[...node.querySelectorAll('[data-cancel-progress],[data-open-progress],[data-resume-segmentation]')].find(button=>!button.disabled&&['cancelProgress','openProgress','resumeSegmentation'].some(key=>focus[key]&&button.dataset[key]===focus[key]));button?.focus({preventScroll:true});}
  for(const detail of node.querySelectorAll('[data-task-log-details]'))detail.ontoggle=()=>detail.open?progressUI.logsOpen.add(detail.dataset.taskLogDetails):progressUI.logsOpen.delete(detail.dataset.taskLogDetails);
  for(const detail of node.querySelectorAll('[data-task-events]'))detail.ontoggle=()=>detail.open?progressUI.openEvents.add(detail.dataset.taskEvents):progressUI.openEvents.delete(detail.dataset.taskEvents);
}
function renderDetailedProgress(){
  const project=state.project,node=$('#project-detailed-progress'),has=!!project?.processing;
  $('#workspace').classList.toggle('has-detailed-progress',has);node.hidden=!has;
  if(has){const task=progressUI.items.find(t=>t.kind==='video'&&t.id===project.id&&t.processing.id===project.processing.id&&t.processing.updated_at>=project.processing.updated_at)||{kind:'video',id:project.id,title:project.name,error:project.error,processing:project.processing};mountTaskCards(node,[task],true);}
  const book=novels.book,bookNode=$('#novel-detailed-progress'),hasBook=!!book?.job?.processing;
  $('.novel-job').classList.toggle('has-detailed-progress',hasBook);bookNode.hidden=!hasBook;
  if(hasBook){const task=progressUI.items.find(t=>t.kind==='audiobook'&&t.id===book.id&&t.processing.id===book.job.processing.id&&t.processing.updated_at>=book.job.processing.updated_at)||{kind:'audiobook',id:book.id,title:book.title,error:book.job.error,processing:book.job.processing,overall_done:book.job.completed,overall_total:book.job.total,overall_unit:book.job.kind==='generate'?'segments':'chapters'};mountTaskCards(bookNode,[task],true);}
  updateProgressClocks();
}
function renderGlobalProgress(){
  const active=progressUI.items.filter(task=>progressActive(task.processing));
  $('#tasks-count').textContent=String(progressUI.active);$('#show-task-progress').classList.toggle('has-running-tasks',!!progressUI.active);
  $('#tasks-summary').textContent=tr('{0} 个进行中 · 显示最近 {1} 个任务',progressUI.active,progressUI.items.length);
  $('#task-progress-empty').hidden=!!progressUI.items.length;
  if($('#task-progress-dialog').open){const tasks=[...progressUI.items];const focused=currentTask(progressUI.focusTask);if(focused&&!tasks.some(task=>taskKey(task)===progressUI.focusTask))tasks.unshift(focused);$('#task-progress-empty').hidden=!!tasks.length;const index=tasks.findIndex(task=>taskKey(task)===progressUI.focusTask);if(index>0)tasks.unshift(tasks.splice(index,1)[0]);mountTaskCards($('#task-progress-list'),tasks);}
  $('#task-peek').hidden=!active.length;
  if(active.length){const task=active.find(t=>t.processing.status!=='queued')||active[0],p=task.processing,percent=displayProgressPercent(p);$('#task-peek-title').textContent=task.kind==='model'?tr('配音模型'):task.title;$('#task-peek-phase').textContent=phaseTitle(p);$('#task-peek-count').textContent=percent===null?tr('查看详细进度'):Math.floor(percent)+'%';}
  renderDetailedProgress();
}
async function refreshTaskProgress(){
  if(progressUI.loading)return;progressUI.loading=true;
  try{
    const data=await api('/api/tasks');progressUI.items=data.items;progressUI.active=data.active;progressUI.offset=data.server_time*1000-Date.now();progressUI.lastFetch=Date.now();
    $('#task-connection-state').textContent=tr('更新于 {0}',new Date().toLocaleTimeString(I18N.locale(),{hour12:false}));$('#task-connection-state').classList.remove('error');renderGlobalProgress();
  }catch{
    $('#task-connection-state').textContent=tr('进度连接中断，正在重试；保留最后状态');$('#task-connection-state').classList.add('error');
  }finally{progressUI.loading=false;}
}
function openTaskProgress(key=null){progressUI.focusTask=typeof key==='string'?key:null;if(!$('#task-progress-dialog').open)openCueDialog($('#task-progress-dialog'));renderGlobalProgress();guard(refreshTaskProgress);}
$('#task-progress-dialog').addEventListener('close',()=>{const position=$('#task-progress-dialog')._pagePosition;if(!position)return;requestAnimationFrame(()=>{const opener=position.opener?.isConnected&&!position.opener.hidden?position.opener:$('#show-task-progress');opener.focus({preventScroll:true});window.scrollTo({left:position.x,top:position.y,behavior:'instant'});});});
$('#show-task-progress').onclick=openTaskProgress;$('#task-peek').onclick=openTaskProgress;
new ResizeObserver(entries=>document.documentElement.style.setProperty('--floating-tools-height',entries[0].target.offsetHeight+'px')).observe($('#floating-tools'));
$('#refresh-task-progress').onclick=()=>guard(refreshTaskProgress);
let translationRequestView=null;
async function refreshTranslationRequests(){
  const view=translationRequestView;
  if(!view||view.loading||!$('#translation-request-dialog').open)return;
  const picker=$('#translation-request-picker'),requested=picker.value;
  const query=new URLSearchParams({run:view.run,request_id:requested});
  view.loading=true;
  try{
    const data=await api(`/api/projects/${view.project}/translation-requests?${query}`,{cache:'no-store'});
    if(view!==translationRequestView||requested!==picker.value)return;
    const items=data.requests;
    const key=JSON.stringify([I18N.locale(),items.map(row=>[row.id,!!row.finished,row.http_status])]);
    if(key!==view.optionsKey){
      picker.replaceChildren(new Option(tr('跟随最新请求'),''),
        ...items.map(row=>new Option([
          new Date(row.started*1000).toLocaleTimeString(I18N.locale(),{hour12:false}),
          tr(row.phase||'网络翻译'),
          row.ids.length?tr('第 {0}–{1} 段',row.ids[0],row.ids.at(-1)):'',
          row.http_status?'HTTP '+row.http_status:''
        ].filter(Boolean).join(' · '),row.id)));
      if(items.some(row=>row.id===requested))picker.value=requested;
      view.optionsKey=key;
    }
    const row=data.selected;
    $('#translation-request-data').hidden=!row;
    const status=$('#translation-request-status');
    if(!row){status.textContent=tr('等待网络翻译请求');return;}
    const elapsed=(row.finished??Date.now()/1000)-row.started;
    const latency=row.first_response===null?'':tr('首个响应 {0} 秒',(row.first_response-row.started).toFixed(1));
    status.textContent=[row.http_status?'HTTP '+row.http_status:'',
      tr(row.finished?'请求已结束':row.first_response===null?'等待 API 返回':'正在接收响应'),
      tr('耗时 {0} 秒',Math.max(0,elapsed).toFixed(1)),latency,row.truncated?tr('仅显示部分内容'):''].filter(Boolean).join(' · ');
    for(const [id,value] of [['url',row.url],['body',row.request],['content',row.content],['raw',row.response],['error',row.error]]){
      const target=$('#translation-request-'+id);
      if(target.textContent!==value)target.textContent=value;
    }
  }catch(error){if(view===translationRequestView)$('#translation-request-status').textContent=error.message;}
  finally{view.loading=false;}
}
$('#translation-request-picker').onchange=refreshTranslationRequests;
$('#translation-request-dialog').addEventListener('close',()=>{translationRequestView=null;});
setInterval(refreshTranslationRequests,1000);
document.addEventListener('click',event=>{
  const requests=event.target.closest('[data-task-translation-requests]');
  if(requests){
    const task=currentTask(requests.dataset.taskTranslationRequests);
    if(!task)return;
    translationRequestView={project:task.id,run:task.processing.id};
    $('#translation-request-picker').value='';
    $('#translation-request-data').hidden=true;
    $('#translation-request-status').textContent=tr('等待网络翻译请求');
    $('#translation-request-dialog').showModal();
    refreshTranslationRequests();
    return;
  }
  const resume=event.target.closest('[data-resume-segmentation]');
  if(resume){
    if(!resume.disabled&&resume.dataset.resumeSegmentation===state.project?.processing?.id&&ProjectWorkflow.canResumeSegmentation(state.project))guard(runResegment);
    return;
  }
  const detailButton=event.target.closest('[data-open-progress]');if(detailButton){openTaskProgress(detailButton.dataset.openProgress);return;}
  const logs=event.target.closest('[data-task-load-log]');
  if(logs){const key=logs.dataset.taskLoadLog,task=currentTask(key);if(!task||logs.disabled)return;logs.disabled=true;const run=task.processing.id;
    guard(async()=>{try{const data=await api('/api/tasks/'+task.kind+'/'+task.id+'/logs');if(currentTask(key)?.processing.id!==run)return;progressUI.logs.set(key,{run,content:data.content||tr('暂无诊断日志')});progressUI.logsRevision++;renderGlobalProgress();}finally{logs.disabled=false;}});return;}
  const button=event.target.closest('[data-cancel-progress]');if(!button)return;
  const key=button.dataset.cancelProgress;if(progressUI.pending.has(key))return;
  const task=currentTask(key);
  if(!task)return;
  guard(async()=>{progressUI.pending.add(key);renderGlobalProgress();
    try{await api(task.kind==='video'?'/api/projects/'+task.id+'/cancel':task.kind==='audiobook'?'/api/audiobooks/'+task.id+'/cancel':'/api/tts-session/cancel-load',{method:'POST'});await refreshTaskProgress();}
    finally{progressUI.pending.delete(key);renderGlobalProgress();}
  });
});
const beforeProgressRender=render;render=function(full=false){beforeProgressRender(full);renderDetailedProgress();};
const beforeProgressNovels=renderNovels;renderNovels=function(){beforeProgressNovels();renderDetailedProgress();};
$('#ui-language').addEventListener('change',renderGlobalProgress);
setInterval(updateProgressClocks,1000);setInterval(refreshTaskProgress,2000);guard(refreshTaskProgress);
}
