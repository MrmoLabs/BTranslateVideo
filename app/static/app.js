'use strict';
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const state={id:null,project:null,projects:[],dirty:false,audioDirty:false,selectionVersion:0,tab:'source',rows:[],selected:0,listPage:0,poll:false,editorGeneration:0,saving:false};
const labels={idle:'等待开始',queued:'排队中',running:'处理中',cancelling:'正在取消',cancelled:'已取消',interrupted:'上次运行中断',done:'已完成',failed:'需要处理'};
const stages={analyze:'识别原文',resegment:'复用识别结果断句',retranscribe:'重新识别原声',check_asr:'短音频试识别',translate:'翻译为目标语言',dub:'生成整片配音',export:'生成成片',background:'分离背景音',preview:'生成页面预览',all:'完整译制',optimize:'优化译文建议',retranslate_all:'重新翻译全部',audition:'生成本句试听',retranslate:'单句校对',subtitle_review:'批量字幕校对',reference:'从原片取样',diarize:'识别说话人'};
const emotions={inherit:'沿用参考音',source:'跟随本句原声',happy:'开心',angry:'愤怒',sad:'悲伤',afraid:'害怕',disgusted:'厌恶',melancholic:'低落',surprised:'惊讶',calm:'平静'};
const sourceLanguages={"": "自动检测", "en": "英语", "ja": "日语", "ko": "韩语", "fr": "法语", "de": "德语", "es": "西班牙语", "pt": "葡萄牙语", "it": "意大利语", "ru": "俄语", "ar": "阿拉伯语", "hi": "印地语", "zh": "中文"};
function languageName(code){return tr(sourceLanguages[code]||code||tr("自动检测"));}
const strengths={low:'轻度',medium:'中度',high:'较强'};
const active=p=>!!p&&['queued','running','cancelling'].includes(p.status);
const esc=t=>String(t??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clone=x=>JSON.parse(JSON.stringify(x));
const fileUrl=(name,download=false)=>`/api/projects/${state.id}/files/${encodeURIComponent(name)}${download?'?download=true':''}`;
const clock=s=>ProjectWorkflow.preciseTime(s);
const currentRow=()=>state.rows[state.selected];
const targetLanguage=p=>(p||state.project)?.target_language||'zh';
const UNASSIGNED_SPEAKER='__unassigned__';
const targetFile=kind=>({subtitles:targetLanguage()==='zh'?'chinese.srt':'translated.srt',captions:targetLanguage()==='zh'?'chinese.vtt':'translated.vtt'})[kind];
function roleName(speaker){if(speaker===UNASSIGNED_SPEAKER)return tr('未分配角色');const name=state.project?.roles?.[speaker]?.name;if(name)return name;const match=/^speaker_(\d+)$/.exec(speaker);return match?tr('角色 {0}',match[1]):speaker;}
function roleVoiceName(speaker){
  if(speaker===UNASSIGNED_SPEAKER)return tr('请先指定角色');
  if(state.modelBackendSettings?.tts_provider==='openai_speech')return tr('服务音色：{0}',state.modelBackendSettings.tts_voice||'default');
  const assignment=state.project?.voice_assignments?.[speaker];
  if(assignment?.name)return assignment.name;
  const origin=state.project?.reference_origins?.[speaker];
  if(origin?.kind==='project')return tr('原片取样 {0}–{1}',clock(origin.start),clock(origin.end));
  if(state.project?.references?.[speaker])return tr('项目参考音');
  if(!state.rows.some(row=>row.speaker===speaker))return tr('待设置音色');
  return new Set((state.rows.length?state.rows:state.project?.segments||[]).map(row=>row.speaker)).size===1?tr('原片自动取样'):tr('待设置音色');
}
function roleOptionText(speaker){return roleName(speaker)+' · '+roleVoiceName(speaker);}
function projectSpeakers(){return [...new Set([...Object.keys(state.project?.roles||{}),...Object.keys(state.project?.references||{}),...(state.rows.length?state.rows:state.project?.segments||[]).map(row=>row.speaker)])].filter(s=>s!==UNASSIGNED_SPEAKER).sort((a,b)=>a.localeCompare(b,undefined,{numeric:true}));}
function newProjectSpeaker(){let index=1;const used=new Set([...projectSpeakers(),...Object.keys(state.project?.references||{})]);while(used.has('speaker_'+index))index++;return 'speaker_'+index;}
function roleOptions(speakers,value,allowNew=false){return [UNASSIGNED_SPEAKER,...speakers.filter(s=>s!==UNASSIGNED_SPEAKER)].map(s=>`<option value="${esc(s)}" ${s===value?'selected':''}>${esc(roleOptionText(s))}</option>`).join('')+(allowNew?`<option value="__new_role__">${esc(tr('新增角色'))}</option>`:'');}
let toastTimer;
function toast(text){$('#toast').textContent=text;$('#toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').hidden=true,6500);}
async function api(path,options={}){const response=await fetch(path,options);if(!response.ok){let value;try{value=await response.json();}catch{value={detail:tr("请求失败 ({0})",response.status)};}const error=Error(typeof value.detail==='string'?value.detail:JSON.stringify(value.detail));Object.assign(error,{status:response.status,code:value.code,projectId:value.project_id,revision:value.revision});throw error;}return response.json();}
const json=(method,data)=>({method,headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
async function guard(fn){try{await fn();}catch(error){if(typeof markProjectUnavailable==='function')markProjectUnavailable(error);toast(error.message);}}
function setDirty(value){state.dirty=value;state.editorGeneration++;if(value&&typeof scheduleProjectDraft==='function')scheduleProjectDraft();$('#dirty-banner').hidden=!value;renderNextAction();if(state.proposal)updateProposalControls();renderAudition();window.SubtitleTools?.render();}
function switchTab(name){state.tab=name;$$('.tab').forEach(e=>{e.classList.toggle('active',e.dataset.tab===name);e.setAttribute('aria-current',e.dataset.tab===name?'step':'false');});$$('.tab-panel').forEach(e=>e.hidden=e.id!==`panel-${name}`);if(state.project){renderEditors();renderVoices();}renderNextAction();}
$$('.tab').forEach(e=>e.onclick=()=>switchTab(e.dataset.tab));$$('[data-go]').forEach(e=>e.onclick=()=>switchTab(e.dataset.go));

function confirmChoice(message,{save=false,title='请确认操作',proceed='继续'}={}){return new Promise(resolve=>{const d=$('#confirm-dialog');$('#confirm-title').textContent=title;$('#confirm-message').textContent=message;$('#confirm-save').hidden=!save;$('#confirm-discard').textContent=save?tr("放弃修改并继续"):proceed;$('#confirm-cancel').textContent=save?tr("继续编辑"):tr("取消");let settled=false;function done(value){if(settled)return;settled=true;d.close();resolve(value);}$('#confirm-save').onclick=()=>done('save');$('#confirm-discard').onclick=()=>done('discard');$('#confirm-cancel').onclick=()=>done('cancel');d.oncancel=e=>{e.preventDefault();done('cancel');};d.showModal();});}
async function allowLeave(){if(!state.dirty&&!state.audioDirty)return true;const result=await confirmChoice(tr("当前校对或声音设置尚未保存。保存后再切换，或继续留在这里编辑。"),{save:true,title:tr("保留当前修改")});if(result==='cancel')return false;if(result==='discard'&&typeof clearProjectDraft==='function')clearProjectDraft(state.id);if(result==='save'){if(state.dirty)await saveSegments();if(state.audioDirty)await saveAudioOptions();if(state.dirty||state.audioDirty)throw Error(tr("还有新输入的修改未保存，请再保存一次"));}return true;}
async function diagnostics(){
  const d=await api('/api/diagnostics');
  const ready=!!d.ffmpeg&&!!d.ffprobe&&d.asr_ready&&d.translation_ready&&d.tts_ready&&d.separation_ready;
  const location=$('#processing-location');
  location.dataset.i18n=d.local_only?'全程本地处理':d.backends?.translation==='openai_remote'?'网络 API 处理字幕文字':'B 站 API 处理字幕文字';
  location.textContent=tr(location.dataset.i18n);
  $('#environment-summary').textContent=ready?tr(d.service_configured?'模型接口已配置 · 推理需验证':'模型文件已准备 · 推理需验证'):tr('部分环境待准备 · 查看设置');
  $('#environment-settings').title=tr('打开设置检查模型与工具路径');
}
async function refreshList(){state.projects=await api('/api/projects');renderProjectList();}
function renderProjectList(){const query=$('#project-search').value.trim().toLowerCase();$('#project-count').textContent=state.projects.length;const projects=state.projects.filter(p=>p.name.toLowerCase().includes(query));$('#project-list').innerHTML=projects.map(p=>`<button class="project-item ${p.id===state.id?'active':''}" data-id="${p.id}" title="${esc(p.name)}"><b>${esc(p.name)}</b><small><i class="project-state-dot ${esc(p.status)}"></i>${languageName(p.language)} · ${p.mode==='anime'?tr("动漫"):tr("课程")} · ${tr(labels[p.status]||p.status)}</small></button>`).join('')||tr("<p class=\"subtle\">没有匹配的项目</p>");$$('.project-item').forEach(e=>e.onclick=()=>guard(()=>selectProject(e.dataset.id)));$('#recent-projects').innerHTML=state.projects.slice(0,3).map(p=>tr("<button class=\"recent-card\" data-id=\"{0}\"><b>{1}</b><small>{2} → {4} · {3} <span>↗</span></small></button>",p.id,esc(p.name),languageName(p.language),tr(labels[p.status]||p.status),languageName(targetLanguage(p)))).join('')||tr("<p class=\"subtle\">导入一个视频，就可以开始第一个项目。</p>");$$('.recent-card').forEach(e=>e.onclick=()=>guard(()=>selectProject(e.dataset.id)));}
$('#project-search').oninput=renderProjectList;
function pauseMedia(){['result-video','source-video','editor-video','dub-audio','sentence-audio','sentence-video','generated-cue-audio'].forEach(id=>$('#'+id).pause());}
async function selectProject(id){if(id===state.id)return;if(state.selecting)return;state.selecting=true;try{if(!await allowLeave())return;const version=++state.selectionVersion;const p=await api(`/api/projects/${id}`);if(version!==state.selectionVersion)return;pauseMedia();state.id=id;state.project=p;state.projectUnavailable=null;state.rows=[];state.selected=0;state.listPage=0;state.audioDirty=false;state.outputRenderKey=null;state.voiceKey=null;state.proposal=null;state.auditionPending=null;$('#segment-search').value='';state.tab='source';setDirty(false);$('#welcome').hidden=true;$('#workspace').hidden=false;$('#source-video').src=fileUrl('source.media');$('#source-video').load();$('#editor-video').removeAttribute('src');try{localStorage.setItem('btv-project',id);}catch{}render(true);switchTab(ProjectWorkflow.initialTab(p));if(typeof offerProjectDraft==='function')offerProjectDraft();await refreshList();}finally{state.selecting=false;}}
$('#go-home').onclick=()=>guard(async()=>{if(!await allowLeave())return;++state.selectionVersion;pauseMedia();state.id=null;state.project=null;state.rows=[];state.proposal=null;state.audioDirty=false;setDirty(false);$('#workspace').hidden=true;$('#welcome').hidden=false;$('#page-name').textContent=tr("项目首页");try{localStorage.removeItem('btv-project');}catch{}renderProjectList();});

function render(full=false){const p=state.project;if(!p)return;$('#page-name').textContent=tr("视频译制");$('#project-title').textContent=p.name;$('#project-meta').textContent=tr("{0} → {1} · {2}",languageName(p.language),languageName(targetLanguage(p)),p.mode==='anime'?tr("动漫对白"):tr("课程讲解"));$('#project-overview').textContent=tr("{0} 分 {1} 秒 · {2} 句字幕 · {3} 个角色",Math.floor(p.media.duration/60),Math.round(p.media.duration%60),p.segments.length,projectSpeakers().length);$('#source-file-info').textContent=p.source_name||tr("已导入本地视频副本");$('#job-status').textContent=`${tr(labels[p.status]||p.status)}${p.stage?' · '+(tr(stages[p.stage]||p.stage)):''}`;$('#job-guidance').textContent=active(p)?tr("可以切换页面查看进度，已完成片段会保留"):p.outputs.preview?tr("译制成片已可观看"):p.segments.length?tr("按步骤继续，已有结果会复用"):tr("从识别原文或导入字幕开始");$('#job-progress').value=p.progress;$('#job-error').textContent=p.error;$('#job-error').hidden=!p.error;$('#cancel-job').hidden=!active(p);$('#segment-count').textContent=p.segments.length?tr("{0} 句 · {1} 句已有译文",p.segments.length,p.segments.filter(r=>r.zh).length):tr("尚无字幕，先识别原文或导入 SRT");if(full&&!state.dirty){state.rows=clone(p.segments);state.editorRevision=p.edit_revision||0;state.selected=Math.min(state.selected,Math.max(0,state.rows.length-1));renderSentenceList();renderEditors();}$('#activity-log').innerHTML=p.logs.slice(-30).map(r=>`<div>${new Date(r.time).toLocaleTimeString(I18N.locale(),{hour12:false})} · ${esc(r.message)}</div>`).join('');$('#process-log').textContent=p.process_log||tr("尚无模型日志");renderNextAction();renderEditingControls();renderVoices();renderOutputs();renderProposalState();renderTtsRuntime();renderAudition();}
function renderNextAction(){if(typeof renderWorkflow==='function')renderWorkflow();}
function renderEditingControls(){const busy=active(state.project);$('#release-tts').disabled=busy||!state.ttsSession?.alive||state.ttsSession?.busy;['run-all','run-analyze','run-translate','run-resegment','run-optimize','run-dub','run-export','run-preview','save-segments','save-draft-banner','save-audio-options'].forEach(id=>$('#'+id).disabled=busy||state.saving||!!state.projectUnavailable);['source-srt','chinese-srt','background-file','background-mode','background-gain','source-text','chinese-text','voice-text','segment-start','segment-end','segment-speaker','emotion-select','emotion-strength'].forEach(id=>$('#'+id).disabled=busy);$('#emotion-strength').disabled=busy||(currentRow()?.emotion||'inherit')==='inherit';if(!currentRow())['source-text','chinese-text','voice-text','segment-start','segment-end','segment-speaker','emotion-select','emotion-strength'].forEach(id=>$('#'+id).disabled=true);$('#run-audition').disabled=busy||state.saving||!!state.projectUnavailable||!currentRow()?.zh?.trim();$('#run-audition').textContent=busy&&state.project.stage==='audition'?tr("正在生成试听…"):tr("▷ 生成本句试听");$('#run-audition').setAttribute('aria-busy',String(busy&&state.project.stage==='audition'));updateAudioForm();if(typeof renderWorkflow==='function')renderWorkflow();}

function chooseSentence(index){state.selected=Math.max(0,Math.min(index,state.rows.length-1));$('#sentence-audio').pause();$('#editor-video').pause();renderSentenceList();renderEditors();}
$('#sentence-prev').onclick=()=>chooseSentence(state.selected-1);$('#sentence-next').onclick=()=>chooseSentence(state.selected+1);
function renderEditors(){const r=currentRow();$('#editor-empty').hidden=!!r;$('#editor-fields').hidden=!r;$('#voice-select').innerHTML=state.rows.map((s,i)=>tr("<option value=\"{0}\">第 {1} 句 · {2} · {3}</option>",i,s.id,clock(s.start),esc((s.zh||s.text).slice(0,42)))).join('')||tr("<option value=\"\">先生成字幕和译文</option>");$('#voice-select').value=String(state.selected);$('#sentence-prev').disabled=!r||state.selected===0;$('#sentence-next').disabled=!r||state.selected>=state.rows.length-1;if(!r){$('#source-text').value='';$('#chinese-text').value='';$('#voice-text').value='';$('#voice-source').textContent=tr("识别并翻译后，即可逐句调整情绪。");renderEditingControls();return;}$('#selected-segment-label').textContent=tr("第 {0} 句 / 共 {1} 句",r.id,state.rows.length);$('#selected-segment-time').textContent=`${clock(r.start)} — ${clock(r.end)} · ${roleOptionText(r.speaker)}`;$('#source-text').value=r.text;$('#chinese-text').value=r.zh;$('#voice-text').value=r.zh;$('#voice-source').textContent=r.text;$('#segment-start').value=r.start;$('#segment-end').value=r.end;$('#segment-speaker').innerHTML=roleOptions(projectSpeakers(),r.speaker);$('#segment-speaker').value=r.speaker;$('#emotion-select').value=r.emotion||'inherit';$('#emotion-strength').value=r.emotion_strength||'medium';$('#source-text').setAttribute('aria-label',tr("第 {0} 句原文",r.id));$('#chinese-text').setAttribute('aria-label',tr("第 {0} 句译文",r.id));$('#emotion-help').textContent=(r.emotion||'inherit')==='inherit'?tr("使用参考声音的自然表达，不额外指定情绪。"):tr("只调整当前句的表达，音色仍使用该角色的参考声音。");renderEditingControls();renderAudition();window.SubtitleTools?.render();}
function renderEmotionOptions(){const value=$('#emotion-select').value;$('#emotion-select').innerHTML=Object.entries(emotions).map(([id,name])=>`<option value="${id}">${esc(tr(name))}</option>`).join('');$('#emotion-select').value=value||'inherit';}
function edit(field,value){const r=currentRow();if(!r)return;if(['text','zh','start','end','speaker'].includes(field)&&r[field]!==value)SubtitleEditing.clearMark(r);r[field]=value;if(field==='emotion'&&value==='inherit'){delete r.emotion;delete r.emotion_strength;}else if(field==='emotion'&&!r.emotion_strength)r.emotion_strength='medium';setDirty(true);renderEditingControls();}
[['source-text','text'],['chinese-text','zh'],['voice-text','zh'],['segment-speaker','speaker']].forEach(([id,field])=>{$('#'+id).oninput=e=>edit(field,e.target.value);$('#'+id).onchange=()=>{renderSentenceList();if(field==='speaker'){state.voiceKey=null;renderVoices();}};});['start','end'].forEach(k=>$('#segment-'+k).oninput=e=>edit(k,Number(e.target.value)));$('#emotion-select').onchange=e=>{edit('emotion',e.target.value);renderEditors();};$('#emotion-strength').onchange=e=>edit('emotion_strength',e.target.value);$('#voice-select').onchange=e=>chooseSentence(Number(e.target.value));
$('#play-source-cue').onclick=()=>guard(async()=>{const r=currentRow();if(!r)return;const video=$('#editor-video'),id=state.id;$('#original-cue').open=true;const play=async()=>{if(state.id!==id)return;video.currentTime=r.start;state.cueEnd=r.end;await video.play();};if(video.getAttribute('src')!==fileUrl('source.media')){video.onloadedmetadata=()=>guard(play);video.src=fileUrl('source.media');video.load();}else await play();});$('#editor-video').ontimeupdate=()=>{if(state.cueEnd&&$('#editor-video').currentTime>=state.cueEnd)$('#editor-video').pause();};$('#editor-video').onerror=()=>toast(tr("原视频编码暂不支持网页播放，可用本地播放器核对"));

async function saveSegments(){if(state.savingPromise)return state.savingPromise;if(!state.rows.length)throw Error(tr("请先识别或导入原文字幕"));const id=state.id,version=state.selectionVersion,generation=state.editorGeneration,rows=clone(state.rows);state.saving=true;renderEditingControls();const task=(async()=>{try{const p=await api(`/api/projects/${id}/segments`,json('PUT',{segments:rows,revision:state.editorRevision}));if(id!==state.id||version!==state.selectionVersion)return;state.editorRevision=p.edit_revision||0;state.project=p;if(state.editorGeneration===generation){setDirty(false);if(typeof clearProjectDraft==='function')clearProjectDraft(id);await refreshCurrent(true);toast(tr("校对与情绪设置已保存"));}else {if(typeof scheduleProjectDraft==='function')scheduleProjectDraft();toast(tr("已保存提交时的内容，之后输入的修改仍待保存"));}}finally{state.saving=false;state.savingPromise=null;if(id===state.id){renderNextAction();renderEditingControls();}}})();state.savingPromise=task;return task;}
$('#save-segments').onclick=()=>guard(saveSegments);$('#save-draft-banner').onclick=()=>guard(saveSegments);
async function refreshCurrent(full=false){
if(!state.id)return;const id=state.id,version=state.selectionVersion,request=state.refreshVersion=(state.refreshVersion||0)+1;let p;
try{
  if(!full&&state.project?.content_version){
    const packet=await api(`/api/projects/${id}/runtime`);
    if(id!==state.id||version!==state.selectionVersion||request!==state.refreshVersion)return;
    p=ProjectWorkflow.mergeRuntime(state.project,packet);
  }
  if(!p)p=await api(`/api/projects/${id}`);
}catch(error){if(id!==state.id||version!==state.selectionVersion||request!==state.refreshVersion)return;if(markProjectUnavailable(error))return;throw error;}
if(id!==state.id||version!==state.selectionVersion||request!==state.refreshVersion)return;
if(!ProjectWorkflow.acceptsProject(state.project,p,state.editorRevision))return;
state.projectUnavailable=null;renderProjectAvailability();
const changed=p.content_version!==state.project?.content_version||p.edit_revision!==state.project?.edit_revision;
state.project=p;render((full||changed)&&!state.dirty);
}
async function run(stage){return projectActions.task('run:'+stage,async token=>{
await api(`/api/projects/${token.id}/run/${stage}`,{method:'POST'});
if(!projectActions.current(token))return;
await refreshCurrent(true);if(!projectActions.current(token))return;
switchTab(['analyze','resegment','retranscribe','check_asr'].includes(stage)?'source':['translate','optimize','retranslate_all'].includes(stage)?'subtitles':['export','preview'].includes(stage)?'export':stage==='all'?state.tab:'voices');
},{audio:true});}
['all','analyze','translate','dub','export','preview','optimize','retranslate_all'].forEach(stage=>$('#run-'+stage).onclick=()=>guard(()=>run(stage)));
async function runResegment(){
  const token=projectActions.context();
  if(state.project.segments.length&&await confirmChoice(tr("将复用上次识别的原词和时间戳重新断句，不调用当前识别模型。现有字幕、译文和成片先备份。"),{title:tr("重新断句"),proceed:tr("备份并重新断句")})==='cancel')return;
  if(projectActions.current(token))await run('resegment');
}
$('#run-resegment').onclick=()=>guard(runResegment);
$('#cancel-job').onclick=()=>guard(async()=>{await api(`/api/projects/${state.id}/cancel`,{method:'POST'});await refreshCurrent();});
async function runAudition(video=false){const row=currentRow();if(!row?.zh?.trim())throw Error(tr("先填写这句的译文配音稿"));const id=state.id,version=state.selectionVersion;const result=await api(`/api/projects/${id}/audition`,json('POST',{segment:clone(row),revision:state.editorRevision,video}));if(id!==state.id||version!==state.selectionVersion)return;state.auditionPending={project:id,request:result.request_id};await refreshCurrent(false);toast(video?tr("正在生成本句视频预览，当前草稿不会替换成片"):tr("正在生成本句原速试听，当前草稿不会替换成片"));}
$('#run-audition').onclick=()=>guard(runAudition);
function expressionSignature(r){return JSON.stringify([r?.id,String(r?.zh||'').trim(),String(r?.speaker||'').trim(),r?.emotion||'inherit',(r?.emotion||'inherit')==='inherit'?'medium':r?.emotion_strength||'medium']);}
function renderAudition(){if(typeof renderGeneratedCue==='function')renderGeneratedCue();if(!state.project)return;const sample=state.project.audition,row=currentRow(),visible=!!sample&&sample.segment.id===row?.id;$('#audition-result').hidden=!visible;if(!visible){$('#sentence-audio').pause();return;}const same=expressionSignature(row)===expressionSignature(sample.segment);$('#audition-label').textContent=tr("第 {0} 句 · {1}{2}",sample.segment.id,tr(emotions[sample.segment.emotion||'inherit']),sample.segment.emotion?' / '+tr(strengths[sample.segment.emotion_strength||'medium']):'');$('#audition-state').textContent=same?tr("与当前配音稿一致"):tr("试听的是之前版本");$('#audition-note').textContent=same?tr("生成后自动播放，可暂停或重播；整片仍需重新生成。"):tr("当前文字、角色或情绪已有修改，请重新生成试听。");const url=fileUrl(sample.file);if($('#sentence-audio').getAttribute('src')!==url){$('#sentence-audio').src=url;$('#sentence-audio').load();}}

function renderVoices(){
  const p=state.project;if(!p)return;
  const speakers=projectSpeakers();
  const key=JSON.stringify([state.id,speakers,p.roles,p.reference_origins,p.references,p.voice_assignments,active(p),I18N.locale(),state.modelBackendSettings?.tts_provider,state.modelBackendSettings?.tts_voice]);
  if(key!==state.voiceKey){state.voiceKey=key;
    $('#speaker-cards').innerHTML=speakers.map(s=>`<div class="speaker-card"><div class="card-heading"><h3>${esc(roleName(s))}</h3><span class="badge">${esc(tr(p.references[s]?'参考音已就绪':'待准备'))}</span><button type="button" class="role-delete-button" data-delete-role="${esc(s)}" ${active(p)?'disabled':''}>${esc(tr('删除角色'))}</button></div><p class="role-voice-name" title="${esc(roleOptionText(s))}">${esc(tr('音色：{0}',roleVoiceName(s)))}</p><div class="speaker-reference">${p.references[s]?`<audio controls preload="none" src="${fileUrl(p.references[s])}"></audio>`:''}</div><small class="role-assigned-count">${esc(state.rows.some(row=>row.speaker===s)?tr('{0} 句台词',state.rows.filter(row=>row.speaker===s).length):tr('未分配台词'))}</small><div class="role-card-actions"><label class="file-button">${esc(tr(p.references[s]?'更换参考音':'上传参考音'))}<input type="file" accept="audio/*,video/*" data-speaker="${esc(s)}" ${active(p)?'disabled':''}></label><button type="button" data-auto-reference="${esc(s)}" ${active(p)||!state.rows.some(row=>row.speaker===s)?'disabled':''} title="${esc(tr('按已分配台词取样，不会自动区分多人身份'))}">${esc(tr('从原片取样'))}</button></div></div>`).join('')||(state.rows.length?`<p class="subtle">${esc(tr('还没有角色，可新增角色后批量分配台词。'))}</p>`:tr("<p class=\"subtle\">生成字幕后，这里会显示角色。</p>"));
    $$('#speaker-cards input').forEach(el=>el.onchange=()=>guard(async()=>{if(!el.files[0])return;const id=state.id,version=state.selectionVersion,body=new FormData();body.append('audio',el.files[0]);await api(`/api/projects/${id}/reference/${el.dataset.speaker}`,{method:'POST',body});if(id!==state.id||version!==state.selectionVersion)return;await refreshCurrent(false);toast(tr("参考音已保存，相关角色需重新生成配音"));}));
  }
  $('#reference-count').textContent=tr("{0} / {1} 已准备",speakers.filter(s=>p.references[s]).length,speakers.length);const audio=$('#dub-audio'),has=!!p.outputs.audio;audio.hidden=!has;$('#audio-placeholder').hidden=has;if(has){const url=fileUrl(p.outputs.audio)+`?v=${p.dub_signature||''}`;if(audio.getAttribute('src')!==url)audio.src=url;}else if(audio.getAttribute('src')){audio.pause();audio.removeAttribute('src');audio.load();}$('#background-state').textContent=p.background?tr("已上传独立背景音"):tr("未上传");$('#remove-background').hidden=!p.background;if(!state.audioDirty){$('#background-mode').value=p.background_mode||(p.background?'custom':'none');$('#background-gain').value=p.background_gain??1;$('#audio-dirty').hidden=true;}updateAudioForm();}
function updateAudioForm(){if(!state.project)return;$('#custom-background').hidden=$('#background-mode').value!=='custom';$('#background-gain').disabled=active(state.project)||$('#background-mode').value==='none';}
function audioDirty(){state.audioGeneration=(state.audioGeneration||0)+1;state.audioDirty=true;renderNextAction();$('#audio-dirty').hidden=false;updateAudioForm();}$('#background-mode').onchange=audioDirty;$('#background-gain').oninput=audioDirty;
async function saveAudioOptions(){const id=state.id,version=state.selectionVersion,generation=state.audioGeneration||0;await api(`/api/projects/${id}/audio-options`,json('PUT',{mode:$('#background-mode').value,gain:Number($('#background-gain').value)}));if(id!==state.id||version!==state.selectionVersion)return;if((state.audioGeneration||0)===generation)state.audioDirty=false;await refreshCurrent(false);toast(state.audioDirty?tr("已保存提交时的声音设置，后续修改仍待保存"):tr("背景声音设置已保存，重新生成成片即可"));}$('#save-audio-options').onclick=()=>guard(saveAudioOptions);
$('#background-file').onchange=()=>guard(async()=>{const input=$('#background-file');if(!input.files[0])return;const id=state.id,version=state.selectionVersion,body=new FormData();body.append('audio',input.files[0]);await api(`/api/projects/${id}/background`,{method:'POST',body});if(id!==state.id||version!==state.selectionVersion)return;input.value='';state.audioDirty=false;await refreshCurrent(false);toast(tr("独立背景音已保存"));});$('#remove-background').onclick=()=>guard(async()=>{const id=state.id;await api(`/api/projects/${id}/background`,{method:'DELETE'});if(id!==state.id)return;state.audioDirty=false;await refreshCurrent(false);});

function renderOutputs(){const p=state.project;if(!p)return;const hasVideo=!!p.outputs.video,hasPreview=!!p.outputs.preview,key=JSON.stringify([state.id,p.outputs,p.preview_info,p.segments.length,p.segments.some(s=>s.zh)]);if(state.outputRenderKey!==key){state.outputRenderKey=key;const groups=[];function link(file,label,detail='',primary=false){return `<a class="download-link ${primary?'primary-download':''}" href="${fileUrl(file,true)}" download>${label}<span>${detail} ↓</span></a>`;}if(hasPreview)groups.push(link(p.outputs.preview,tr("译制视频"),'MP4',true));if(hasVideo)groups.push(link(p.outputs.video,tr("原画质双音轨视频"),'MKV'));if(p.outputs.mixed_audio||p.outputs.audio)groups.push(tr("<div class=\"download-group\"><b>音频</b>")+(p.outputs.mixed_audio?link(p.outputs.mixed_audio,tr("译制配音与背景音"),'FLAC'):'')+(p.outputs.audio?link(p.outputs.audio,tr("纯译制人声"),'WAV'):'')+'</div>');if(p.segments.length)groups.push(tr("<div class=\"download-group\"><b>字幕</b>")+link('original.srt',tr("原文字幕"),'SRT')+(p.segments.some(s=>s.zh)?link(targetFile('subtitles'),tr("译文字幕"),'SRT')+link('bilingual.srt',tr("双语字幕"),'SRT'):'')+'</div>');$('#output-links').innerHTML=groups.join('')||tr("<p class=\"empty-inline\">完成译制后，下载文件会出现在这里。</p>");}$('#caption-controls').hidden=!hasPreview;const video=$('#result-video');video.hidden=!hasPreview;$('#preview-placeholder').hidden=hasPreview;$('#run-preview').hidden=!hasVideo;$('#run-preview').textContent=hasPreview?tr("更新页面预览"):tr("生成页面预览");if(hasPreview){const v=encodeURIComponent(p.preview_info?.generated_at||p.export_signature||''),url=fileUrl(p.outputs.preview)+`?v=${v}`;if(video.getAttribute('src')!==url){$('#preview-error').hidden=true;video.src=url;$('#result-captions').srclang=targetLanguage();$('#result-captions').label=languageName(targetLanguage());$('#result-captions').src=fileUrl(targetFile('captions'))+`?v=${v}`;video.load();}}else if(video.getAttribute('src')){video.pause();video.removeAttribute('src');$('#result-captions').removeAttribute('src');video.load();}$('#preview-placeholder').textContent=hasVideo?tr("已有成片，点击“生成页面预览”后即可观看，无需重新配音。"):tr("完成译制配音后，点击“生成成片”。保留背景音的译制视频会显示在这里。");}
// Keep the explicit checkbox and the player's native subtitle menu in sync.
let captionsEnabled=true;
try{captionsEnabled=localStorage.getItem('btv-captions')!=='hidden';}catch{}
$('#show-captions').checked=captionsEnabled;
function rememberCaptionVisibility(){
  try{localStorage.setItem('btv-captions',captionsEnabled?'showing':'hidden');}catch{}
}
function applyCaptionVisibility(){
  $('#result-captions').track.mode=captionsEnabled?'showing':'hidden';
}
$('#show-captions').onchange=()=>{
  captionsEnabled=$('#show-captions').checked;
  rememberCaptionVisibility();
  applyCaptionVisibility();
};
$('#result-captions').addEventListener('load',applyCaptionVisibility);
$('#result-video').addEventListener('loadedmetadata',applyCaptionVisibility);
$('#result-video').textTracks.addEventListener('change',()=>{
  if($('#result-captions').readyState!==2||$('#result-video').readyState<1)return;
  captionsEnabled=$('#result-captions').track.mode==='showing';
  $('#show-captions').checked=captionsEnabled;
  rememberCaptionVisibility();
});
$('#result-video').onerror=()=>{if(state.project?.outputs?.preview)$('#preview-error').hidden=false;};

async function openImport(){$('#import-error').textContent='';$('#import-dialog').showModal();if(typeof prepareImportExperience==='function')await prepareImportExperience();}$('#new-project').onclick=()=>guard(openImport);$('#welcome-import').onclick=()=>guard(openImport);$$('.close-dialog').forEach(e=>e.onclick=()=>e.closest('dialog').close());
$('#import-form').onsubmit=async e=>{e.preventDefault();const b=$('#import-submit');b.disabled=true;b.setAttribute('aria-busy','true');b.textContent=tr("正在导入视频…");$('#import-error').textContent='';try{const form=new FormData(e.target),video=form.get('video');let p;if(video&&video.size){form.delete('path');p=await api('/api/projects/upload',{method:'POST',body:form});}else{const value=Object.fromEntries(form);delete value.video;p=await api('/api/projects/import',json('POST',value));}$('#import-dialog').close();await selectProject(p.id);switchTab('source');}catch(error){$('#import-error').textContent=error.message;}finally{b.disabled=false;b.setAttribute('aria-busy','false');b.textContent=tr("创建项目");}};
async function srtUpload(input,kind){if(!input.files[0])return;if(state.dirty&&await confirmChoice(tr("导入会替换当前字幕，并放弃未保存的校对。"),{title:tr("导入字幕"),proceed:tr("导入并替换")})==='cancel'){input.value='';return;}const id=state.id,version=state.selectionVersion,body=new FormData();body.append('subtitle',input.files[0]);body.append('kind',kind);await api(`/api/projects/${id}/subtitles`,{method:'POST',body});if(id!==state.id||version!==state.selectionVersion)return;setDirty(false);input.value='';await refreshCurrent(true);switchTab('subtitles');toast(tr("字幕已导入"));}$('#source-srt').onchange=()=>guard(()=>srtUpload($('#source-srt'),'source'));$('#chinese-srt').onchange=()=>guard(()=>srtUpload($('#chinese-srt'),'zh'));
async function openSettings(){if(state.settingsLoaded){showSection('settings');return;}const s=await api('/api/settings');state.settings=s;Object.entries(s).forEach(([k,v])=>{const el=$('#settings-form').elements[k];if(el)el.value=v;});$('#settings-error').textContent='';state.settingsLoaded=true;state.settingsDirty=false;showSection('settings');updateSettingsDraft();}$('#open-settings').onclick=()=>guard(openSettings);$('#environment-settings').onclick=()=>guard(openSettings);
$('#settings-form').onsubmit=async e=>{e.preventDefault();try{const data={...state.settings,...Object.fromEntries(new FormData(e.target))};['max_speed','max_lead','max_lag','alignment_fallback_speed','alignment_fallback_lead','alignment_fallback_lag','audio_workers','tts_batch_size','tts_idle_minutes','translation_batch','translation_parallel'].forEach(k=>data[k]=Number(data[k]));data.translation_compact=[true,'true'].includes(data.translation_compact);data.tts_keep_loaded=[true,'true'].includes(data.tts_keep_loaded);state.settings=await api('/api/settings',json('PUT',data));state.settingsDirty=false;updateSettingsDraft();await diagnostics();toast(tr("设置已保存，下次任务生效"));}catch(error){$('#settings-error').textContent=error.message;}};
window.addEventListener('beforeunload',e=>{if(state.dirty||state.audioDirty){e.preventDefault();e.returnValue='';}});
async function poll(){if(state.poll)return;state.poll=true;try{await refreshCurrent();await showProposal(false);await refreshList();await refreshTtsSession();}catch{$('#job-status').textContent=tr("连接本地服务中…");}finally{state.poll=false;}}

function renderTtsRuntime(){
  const r=state.project?.tts_runtime;$('#tts-runtime').hidden=!r;if(!r)return;
  $('#tts-phase').textContent=tr(r.phase||tr("等待配音"));
  $('#tts-batch').textContent=r.provider?tr('用户管理的本地服务 · 按句请求'):tr("{0} 个模型实例 · 当前批量 {1} / 上限 {2} · 本次最大批量 {3}",r.instances??1,r.actual_batch||'—',r.requested_batch||'—',r.max_actual_batch||'—');
  $('#tts-counts').textContent=tr("新生成 {0} / {1} 段 · 复用 {2} 段",r.completed||0,r.total||0,r.reused||0);
  $('#tts-reason').textContent=tr(r.fallback_reason||r.last_fallback_reason||"同角色、相近长度的文本优先合批；显存不足自动减小批量。");
  const phases=Object.entries(r.stage_seconds||{}).map(([k,v])=>tr("{0} {1} 秒",tr(k),Number(v).toFixed(1)));
  if(r.phase==='合成完成'&&r.seconds!=null){const other=Math.max(0,r.seconds-Object.values(r.stage_seconds||{}).reduce((a,b)=>a+Number(b),0));phases.push(tr("其他准备与读写 {0} 秒",other.toFixed(1)));}
  $('#tts-timing').textContent=[r.model_reused?tr("模型已复用，无需重新加载"):r.model_load_seconds!=null?tr("模型加载 {0} 秒",Number(r.model_load_seconds).toFixed(1)):'',r.model_load_reason?tr("加载原因：{0}",tr(r.model_load_reason)):'',...phases,r.seconds!=null?tr("合成累计 {0} 秒",Number(r.seconds).toFixed(1)):'',r.peak_allocated_mb?tr("PyTorch 峰值 {0} GiB",(r.peak_allocated_mb/1024).toFixed(1)):''].filter(Boolean).join(' · ');
}
function proposalMeta(){
  const p=state.project,preview=p?.translation_preview;
  return preview?.base_revision===(p?.edit_revision||0)?preview:p?.translation_proposal;
}
function proposalVersion(meta){
  return meta?JSON.stringify([meta.id,meta.status,meta.done,state.project?.edit_revision]):null;
}
function inlineRetranslation(row){
  const meta=proposalMeta(),proposal=state.proposal;
  if(meta?.kind!=='retranslate_all'||meta.status==='applied'||proposal?.id!==meta.id||proposal.stale||
      proposal.base_revision!==(state.project.edit_revision||0))return null;
  const item=state.proposalItemsById?.get(row.id);
  return item&&item.source===row.text&&item.before===row.zh&&item.after!==item.before?item:null;
}
function loadProposalEdits(id){
  const key='btv-translation-edits:'+state.id+':'+id;
  if(state.proposalEditKey===key)return;
  state.proposalEditKey=key;state.proposalEdits={};
  try{
    const saved=JSON.parse(localStorage.getItem(key)||'{}');
    if(saved&&typeof saved==='object'&&!Array.isArray(saved))state.proposalEdits=saved;
  }catch{}
}
function saveProposalEdits(){
  try{
    if(Object.keys(state.proposalEdits).length)localStorage.setItem(state.proposalEditKey,JSON.stringify(state.proposalEdits));
    else localStorage.removeItem(state.proposalEditKey);
  }catch{}
}
function proposalEditText(item){
  return item?(state.proposalEdits?.[item.id]??item.after):'';
}
function canEditProposal(){
  const p=state.proposal,meta=proposalMeta();
  return !!p?.allow_edits&&meta?.id===p.id&&meta.status==='ready'&&!active(state.project)&&!state.applyingProposal;
}
function proposalTextMarkup(item,inline=false){
  const marker=inline?` data-retranslated-cue="${item.id}"`:'';
  const text=esc(proposalEditText(item));
  if(!state.proposal.allow_edits||item.before===item.after)return `<p dir="auto"${marker}>${text}</p>`;
  return `<textarea dir="auto" rows="2" maxlength="6000" data-proposal-edit="${item.id}"${marker}
      aria-label="${esc(tr('编辑第 {0} 句新译文',item.id))}" ${canEditProposal()?'':'disabled'}>${text}</textarea>`;
}
function renderProposalState(){
  const p=state.project,m=proposalMeta(),partial=m===p?.translation_preview;
  $('#translation-review').hidden=!m;
  $('#run-optimize').disabled=active(p)||!p.segments.some(s=>s.zh);
  if(!m){state.proposal=null;return;}
  if(state.proposalProject!==state.id||state.proposal?.id!==m.id){
    state.proposal=null;state.proposalProject=state.id;state.proposalSelected=new Set();$('#proposal-body').hidden=true;
  }
  const retranslated=m.kind==='retranslate_all';
  const loaded=state.proposalVersion===proposalVersion(m)?state.proposal:null;
  for(const id of ['proposal-title','proposal-dialog-title']){
    const title=$('#'+id);
    title.dataset.i18n=retranslated?(id==='proposal-title'?'重新翻译结果':'重新翻译对比'):id==='proposal-title'?'译文优化建议':'译文优化对比';
    title.textContent=tr(title.dataset.i18n);
  }
  $('#proposal-summary').textContent=partial?tr("已处理 {0} / {1} 段，可查看已生成的译文。",m.done,m.total):m.status==='applied'?tr("所选建议已应用。请重新生成译制配音和成片；其余建议需重新审校。"):retranslated?tr("已生成 {0} 条新译文，{1} 条待采用。",m.total-(m.failed||0),m.changed):tr("已审校 {0} 段，其中 {1} 段有调整。原译文和成片尚未替换。",m.total,m.changed);
  if(loaded?.needs_retranslation)$('#proposal-summary').textContent=tr(
    '保留 {0} 条新译文，{1} 条字幕已调整，需重新翻译。',loaded.total-(loaded.failed||0),loaded.needs_retranslation);
  if(m.status==='ready'&&state.proposal?.allow_edits)$('#proposal-summary').textContent+=' '+tr('可直接编辑下方新译文，采用时一起保存。');
  $('#show-proposal').disabled=active(p)&&!partial;
  $('#show-proposal-label').dataset.i18n=partial?'查看已完成译文':'查看逐句对比';
  $('#show-proposal-label').textContent=tr($('#show-proposal-label').dataset.i18n);
  if(state.proposal)updateProposalControls();
}
async function showProposal(open=true){
  const id=state.id,version=state.selectionVersion,meta=proposalMeta(),key=proposalVersion(meta);
  const dialog=$('#proposal-dialog'),inline=meta?.kind==='retranslate_all'&&meta.status!=='applied'&&state.section==='projects'&&state.tab==='subtitles';
  if(!open&&!dialog.open&&!inline)return;
  if(!meta||(!open&&state.proposal?.id===meta.id&&state.proposalVersion===key))return;
  const request=state.proposalRequest=(state.proposalRequest||0)+1;
  const proposal=await api(`/api/projects/${id}/translation-proposal`,{cache:'no-store'});
  if(state.id!==id||state.selectionVersion!==version||state.proposalRequest!==request||
      proposalVersion(proposalMeta())!==key||(!open&&!dialog.open&&!inline))return;
  if(!proposal||proposal.id!==meta.id)return;
  if(state.proposal?.id!==proposal.id){
    state.proposalPage=0;
  }
  const eligible=new Set(proposal.partial?[]:proposal.items.filter(r=>r.before!==r.after).map(r=>r.id));
  state.proposalSelected=state.proposal?.id!==proposal.id||(state.proposal?.partial&&!proposal.partial)
    ?eligible:new Set([...state.proposalSelected].filter(id=>eligible.has(id)));
  state.proposal=proposal;state.proposalVersion=key;state.proposalProject=id;
  loadProposalEdits(proposal.id);
  if(meta.kind==='retranslate_all'){
    state.proposalItemsById=new Map(proposal.items.map(item=>[item.id,item]));
    if(state.section==='projects'&&state.tab==='subtitles')renderSentenceList();
  }
  renderProposalState();
  if(open||dialog.open){$('#proposal-body').hidden=false;renderProposalItems();}
  if(open&&!dialog.open)dialog.showModal();
}
function renderProposalItems(){
  const p=state.proposal;if(!p)return;
  const items=p.partial?p.items:p.items.filter(r=>r.before!==r.after||(r.checks||[]).some(check=>check.code==='translation_failed')),pages=Math.max(1,Math.ceil(items.length/12));
  state.proposalPage=Math.max(0,Math.min(state.proposalPage||0,pages-1));
  $('#proposal-items').innerHTML=items.slice(state.proposalPage*12,(state.proposalPage+1)*12).map(r=>tr("<article class=\"proposal-item\"><div class=\"proposal-item-title\"><label class=\"check-label\"><input type=\"checkbox\" data-proposal-id=\"{0}\" aria-label=\"采用第 {1} 段优化\" {2}>第 {3} 段</label><small class=\"subtle\">预计朗读 {4} 秒 / 字幕窗口 {5} 秒</small></div><p class=\"proposal-source\">{6}</p><div class=\"proposal-grid\"><div><b>当前译文</b><p>{7}</p></div><div class=\"proposal-after\"><b>优化建议</b>{8}</div></div><p class=\"subtle\">{9}</p></article>",r.id,r.id,r.before===r.after?'disabled':state.proposalSelected.has(r.id)?'checked':'',r.id,r.estimated_seconds,r.seconds,esc(r.source),esc(r.before),proposalTextMarkup(r),esc([r.reason||tr("调整译文语序与表达；请结合原文核对。"),...(r.checks||[]).map(translationCheckText)].join(" ")))).join('')||tr("<p class=\"subtle\">本次审校没有需要修改的译文。</p>");
  $$('[data-proposal-id]').forEach(input=>input.onchange=()=>{const id=Number(input.dataset.proposalId);if(input.checked)state.proposalSelected.add(id);else state.proposalSelected.delete(id);updateProposalControls();});
  $('#proposal-page').textContent=`${state.proposalPage+1} / ${pages}`;
  if(p.partial&&!items.length)$('#proposal-items').textContent=tr("首批译文生成后会显示在这里。");
  $('#proposal-prev').disabled=state.proposalPage===0;$('#proposal-next').disabled=state.proposalPage>=pages-1;
  updateProposalControls();
}
function updateProposalControls(){
  const p=state.proposal;if(!p)return;
  const meta=state.project.translation_proposal,stale=p.stale||meta?.id!==p.id||meta?.status!=='ready'||p.base_revision!==(state.project.edit_revision||0);
  const total=p.items.filter(r=>r.before!==r.after).length,count=state.proposalSelected.size;
  const applied=meta?.status==='applied',busy=!!state.applyingProposal;
  const empty=[...state.proposalSelected].some(id=>state.proposalEdits?.[id]?.trim()==='');
  $('#proposal-selected').textContent=p.partial?tr("已显示 {0} / {1} 段",p.items.length,p.total):applied?tr("所选建议已应用"):stale?tr("建议已过期，请重新生成。"):tr("已选择 {0} / {1} 段",count,total);
  $('#proposal-all').checked=total>0&&count===total;$('#proposal-all').indeterminate=count>0&&count<total;
  $('#proposal-all').disabled=stale||active(state.project)||state.applyingProposal;
  $$('[data-proposal-id]').forEach(input=>input.disabled=stale||active(state.project)||state.applyingProposal);
  $$('[data-proposal-edit]').forEach(input=>input.disabled=!canEditProposal());
  $('#apply-proposal').disabled=stale||!count||empty||state.dirty||active(state.project)||state.applyingProposal;
  const button=$('#apply-proposal');
  button.hidden=!!p.partial;
  button.textContent=busy?tr("正在应用…"):applied?tr("已应用"):stale?tr("建议已过期"):tr(meta?.kind==='retranslate_all'?"应用所选译文（{0}）":"应用所选优化（{0}）",count);
  button.setAttribute('aria-busy',String(busy));
  const hint=busy?tr("正在保存所选译文，请稍候。"):active(state.project)?tr("当前项目正在处理，完成后可应用建议。"):applied?tr("已应用成功。点击“一键完成”重新生成译制配音和成片。"):stale?tr("字幕已经更新，请重新生成优化建议。"):state.dirty?tr("有未保存的校对，请先保存；译文发生变化后需重新生成建议。"):!count?tr("请先勾选需要采用的优化建议。"):tr("应用后需重新生成译制配音和成片；未修改片段可复用。");
  button.title=hint;
  $('#proposal-action-hint').textContent=p.partial?tr(active(state.project)?"候选会自动刷新，完整任务结束后可勾选采用。":"用相同设置再次开始重新翻译，可复用已完成候选。"):empty?tr('请填写空白的新译文，或取消选择对应句子。'):hint;
}
document.addEventListener('input',event=>{
  const input=event.target;
  if(!input.matches('[data-proposal-edit]')||!canEditProposal())return;
  const item=state.proposalItemsById?.get(Number(input.dataset.proposalEdit));
  if(!item)return;
  if(input.value===item.after)delete state.proposalEdits[item.id];
  else state.proposalEdits[item.id]=input.value;
  if(input.closest('#segment-list'))growTextarea(input);
  saveProposalEdits();state.subtitleKey=null;updateProposalControls();
});
$('#proposal-dialog').addEventListener('close',()=>{if(state.tab==='subtitles')renderSentenceList();});
$('#show-proposal').onclick=()=>guard(showProposal);
$('#proposal-all').onchange=()=>{state.proposalSelected=$('#proposal-all').checked?new Set(state.proposal.items.filter(r=>r.before!==r.after).map(r=>r.id)):new Set();renderProposalItems();};
$('#proposal-prev').onclick=()=>{state.proposalPage--;renderProposalItems();};
$('#proposal-next').onclick=()=>{state.proposalPage++;renderProposalItems();};
$('#apply-proposal').onclick=()=>guard(async()=>{
  if(state.dirty)throw Error(tr("请先保存校对，再重新生成建议，避免覆盖未保存的内容"));
  const id=state.id,proposal=state.proposal,ids=[...state.proposalSelected];
  const edits=Object.fromEntries(ids.filter(key=>Object.hasOwn(state.proposalEdits,key)).map(key=>[key,state.proposalEdits[key]]));
  state.applyingProposal=true;updateProposalControls();
  try{
    await api(`/api/projects/${id}/translation-proposal/apply`,json('POST',{proposal_id:proposal.id,revision:proposal.base_revision,ids,edits}));
    if(state.id!==id||state.proposal?.id!==proposal.id)return;
    for(const key of ids)delete state.proposalEdits[key];
    saveProposalEdits();
    await refreshCurrent(true);$('#proposal-dialog').close();toast(tr("所选优化已应用，请重新生成译制配音和成片；未修改片段继续复用"));
  }finally{state.applyingProposal=false;if(state.id===id)updateProposalControls();}
});

guard(async()=>{await I18N.ready();renderEmotionOptions();await diagnostics();await refreshList();await refreshTtsSession();let remembered;try{remembered=localStorage.getItem('btv-project');}catch{}showSection('projects');});setInterval(poll,2000);

async function refreshTtsSession(){
  const s=await api('/api/tts-session');state.ttsSession=s;
  $('#tts-session-state').textContent=typeof formatTtsSession==='function'?formatTtsSession(s):s.service_managed?tr(s.alive?'本地服务自行管理；内置模型仍驻留':'本地服务自行管理模型加载与释放'):s.alive?(s.busy?tr(s.phase):tr("保温中 · 空闲 {0} 分钟后释放",Math.ceil(s.idle_seconds/60))):tr("未加载 · {0}",tr(s.reason));
  $('#release-tts').disabled=!s.alive||s.busy||active(state.project);
  $('#release-tts').title=tr('仅释放应用管理的 IndexTTS，不会停止本地服务');
  if(typeof renderTtsControls==='function')renderTtsControls(s);
}
$('#release-tts').onclick=()=>guard(async()=>{await api('/api/tts-session/release',{method:'POST'});await refreshTtsSession();toast(tr("配音模型已释放，下次合成会重新加载"));});

$('#ui-language').value=I18N.locale();
$('#ui-language').onchange=()=>{
  I18N.setLocale($('#ui-language').value);
  I18N.applyStatic();
  renderEmotionOptions();
  state.outputRenderKey=null;state.voiceKey=null;
  renderProjectList();
  if(state.project){renderSentenceList();renderEditors();render(false);}
  else showSection(state.section);
  if(state.proposal)renderProposalItems();
  guard(async()=>{await diagnostics();await refreshTtsSession();});
};
