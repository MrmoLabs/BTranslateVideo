'use strict';
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const state={id:null,project:null,projects:[],dirty:false,audioDirty:false,selectionVersion:0,tab:'source',rows:[],selected:0,listPage:0,poll:false,editorGeneration:0,saving:false};
const labels={idle:'等待开始',queued:'排队中',running:'处理中',cancelling:'正在取消',cancelled:'已取消',interrupted:'上次运行中断',done:'已完成',failed:'需要处理'};
const stages={analyze:'识别原文',resegment:'恢复完整断句',translate:'翻译为目标语言',dub:'生成整片配音',export:'生成成片',background:'分离背景音',preview:'生成页面预览',all:'完整译制',optimize:'优化译文建议',audition:'生成本句试听'};
const emotions={inherit:'沿用参考音',happy:'开心',angry:'愤怒',sad:'悲伤',afraid:'害怕',disgusted:'厌恶',melancholic:'低落',surprised:'惊讶',calm:'平静'};
const sourceLanguages={"": "自动检测", "en": "英语", "ja": "日语", "ko": "韩语", "fr": "法语", "de": "德语", "es": "西班牙语", "pt": "葡萄牙语", "it": "意大利语", "ru": "俄语", "ar": "阿拉伯语", "hi": "印地语", "zh": "中文"};
function languageName(code){return tr(sourceLanguages[code]||code||tr("自动检测"));}
const strengths={low:'轻度',medium:'中度',high:'较强'};
const active=p=>!!p&&['queued','running','cancelling'].includes(p.status);
const esc=t=>String(t??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clone=x=>JSON.parse(JSON.stringify(x));
const fileUrl=(name,download=false)=>`/api/projects/${state.id}/files/${encodeURIComponent(name)}${download?'?download=true':''}`;
const clock=s=>`${Math.floor(s/60)}:${String(Math.floor(s%60)).padStart(2,'0')}`;
const currentRow=()=>state.rows[state.selected];
const targetLanguage=p=>(p||state.project)?.target_language||'zh';
const targetFile=kind=>({subtitles:targetLanguage()==='zh'?'chinese.srt':'translated.srt',captions:targetLanguage()==='zh'?'chinese.vtt':'translated.vtt'})[kind];
let toastTimer;
function toast(text){$('#toast').textContent=text;$('#toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').hidden=true,6500);}
async function api(path,options={}){const response=await fetch(path,options);if(!response.ok){let value;try{value=await response.json();}catch{value={detail:tr("请求失败 ({0})",response.status)};}const error=Error(typeof value.detail==='string'?value.detail:JSON.stringify(value.detail));Object.assign(error,{status:response.status,code:value.code,projectId:value.project_id,revision:value.revision});throw error;}return response.json();}
const json=(method,data)=>({method,headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
async function guard(fn){try{await fn();}catch(error){if(typeof markProjectUnavailable==='function')markProjectUnavailable(error);toast(error.message);}}
function setDirty(value){state.dirty=value;state.editorGeneration++;$('#dirty-banner').hidden=!value;renderNextAction();if(state.proposal)updateProposalControls();renderAudition();}
function switchTab(name){state.tab=name;$$('.tab').forEach(e=>{e.classList.toggle('active',e.dataset.tab===name);e.setAttribute('aria-current',e.dataset.tab===name?'step':'false');});$$('.tab-panel').forEach(e=>e.hidden=e.id!==`panel-${name}`);if(state.project){renderEditors();renderVoices();}}
$$('.tab').forEach(e=>e.onclick=()=>switchTab(e.dataset.tab));$$('[data-go]').forEach(e=>e.onclick=()=>switchTab(e.dataset.go));

function confirmChoice(message,{save=false,title='请确认操作',proceed='继续'}={}){return new Promise(resolve=>{const d=$('#confirm-dialog');$('#confirm-title').textContent=title;$('#confirm-message').textContent=message;$('#confirm-save').hidden=!save;$('#confirm-discard').textContent=save?tr("放弃修改并继续"):proceed;$('#confirm-cancel').textContent=save?tr("继续编辑"):tr("取消");let settled=false;function done(value){if(settled)return;settled=true;d.close();resolve(value);}$('#confirm-save').onclick=()=>done('save');$('#confirm-discard').onclick=()=>done('discard');$('#confirm-cancel').onclick=()=>done('cancel');d.oncancel=e=>{e.preventDefault();done('cancel');};d.showModal();});}
async function allowLeave(){if(!state.dirty&&!state.audioDirty)return true;const result=await confirmChoice(tr("当前校对或声音设置尚未保存。保存后再切换，或继续留在这里编辑。"),{save:true,title:tr("保留当前修改")});if(result==='cancel')return false;if(result==='save'){if(state.dirty)await saveSegments();if(state.audioDirty)await saveAudioOptions();if(state.dirty||state.audioDirty)throw Error(tr("还有新输入的修改未保存，请再保存一次"));}return true;}
async function diagnostics(){const d=await api('/api/diagnostics');const ready=!!d.ffmpeg&&!!d.ffprobe&&d.asr_ready&&d.translation_ready&&d.tts_ready&&d.separation_ready;$('#environment-summary').textContent=ready?tr("本地模型已就绪"):tr("部分环境待准备 · 查看设置");$('#environment-settings').title=ready?tr("全程本地处理，点击查看设置"):tr("打开设置检查模型与工具路径");}
async function refreshList(){state.projects=await api('/api/projects');renderProjectList();}
function renderProjectList(){const query=$('#project-search').value.trim().toLowerCase();$('#project-count').textContent=state.projects.length;const projects=state.projects.filter(p=>p.name.toLowerCase().includes(query));$('#project-list').innerHTML=projects.map(p=>`<button class="project-item ${p.id===state.id?'active':''}" data-id="${p.id}" title="${esc(p.name)}"><b>${esc(p.name)}</b><small><i class="project-state-dot ${esc(p.status)}"></i>${languageName(p.language)} · ${p.mode==='anime'?tr("动漫"):tr("课程")} · ${tr(labels[p.status]||p.status)}</small></button>`).join('')||tr("<p class=\"subtle\">没有匹配的项目</p>");$$('.project-item').forEach(e=>e.onclick=()=>guard(()=>selectProject(e.dataset.id)));$('#recent-projects').innerHTML=state.projects.slice(0,3).map(p=>tr("<button class=\"recent-card\" data-id=\"{0}\"><b>{1}</b><small>{2} → {4} · {3} <span>↗</span></small></button>",p.id,esc(p.name),languageName(p.language),tr(labels[p.status]||p.status),languageName(targetLanguage(p)))).join('')||tr("<p class=\"subtle\">导入一个视频，就可以开始第一个项目。</p>");$$('.recent-card').forEach(e=>e.onclick=()=>guard(()=>selectProject(e.dataset.id)));}
$('#project-search').oninput=renderProjectList;
function pauseMedia(){['result-video','source-video','editor-video','dub-audio','sentence-audio'].forEach(id=>$('#'+id).pause());}
async function selectProject(id){if(id===state.id)return;if(state.selecting)return;state.selecting=true;try{if(!await allowLeave())return;const version=++state.selectionVersion;const p=await api(`/api/projects/${id}`);if(version!==state.selectionVersion)return;pauseMedia();state.id=id;state.project=p;state.projectUnavailable=null;state.rows=[];state.selected=0;state.listPage=0;state.audioDirty=false;state.outputRenderKey=null;state.voiceKey=null;state.proposal=null;state.auditionPending=null;$('#segment-search').value='';state.tab='source';setDirty(false);$('#welcome').hidden=true;$('#workspace').hidden=false;$('#source-video').src=fileUrl('source.media');$('#source-video').load();$('#editor-video').removeAttribute('src');try{localStorage.setItem('btv-project',id);}catch{}render(true);switchTab(p.outputs.preview?'export':p.segments.length?(p.segments.some(s=>!s.zh)?'subtitles':'voices'):'source');await refreshList();}finally{state.selecting=false;}}
$('#go-home').onclick=()=>guard(async()=>{if(!await allowLeave())return;++state.selectionVersion;pauseMedia();state.id=null;state.project=null;state.rows=[];state.proposal=null;state.audioDirty=false;setDirty(false);$('#workspace').hidden=true;$('#welcome').hidden=false;$('#page-name').textContent=tr("项目首页");try{localStorage.removeItem('btv-project');}catch{}renderProjectList();});

function render(full=false){const p=state.project;if(!p)return;$('#page-name').textContent=tr("视频译制");$('#project-title').textContent=p.name;$('#project-meta').textContent=tr("{0} → {1} · {2}",languageName(p.language),languageName(targetLanguage(p)),p.mode==='anime'?tr("动漫对白"):tr("课程讲解"));$('#project-overview').textContent=tr("{0} 分 {1} 秒 · {2} 句字幕 · {3} 个角色",Math.floor(p.media.duration/60),Math.round(p.media.duration%60),p.segments.length,new Set(p.segments.map(r=>r.speaker)).size);$('#source-file-info').textContent=p.source_name||tr("已导入本地视频副本");$('#job-status').textContent=`${tr(labels[p.status]||p.status)}${p.stage?' · '+(tr(stages[p.stage]||p.stage)):''}`;$('#job-guidance').textContent=active(p)?tr("可以切换页面查看进度，已完成片段会保留"):p.outputs.preview?tr("译制成片已可观看"):p.segments.length?tr("按步骤继续，已有结果会复用"):tr("从识别原文或导入字幕开始");$('#job-progress').value=p.progress;$('#job-error').textContent=p.error;$('#job-error').hidden=!p.error;$('#cancel-job').hidden=!active(p);$('#segment-count').textContent=p.segments.length?tr("{0} 句 · {1} 句已有译文",p.segments.length,p.segments.filter(r=>r.zh).length):tr("尚无字幕，先识别原文或导入 SRT");if(full&&!state.dirty){state.rows=clone(p.segments);state.editorRevision=p.edit_revision||0;state.selected=Math.min(state.selected,Math.max(0,state.rows.length-1));renderSentenceList();renderEditors();}$('#activity-log').innerHTML=p.logs.slice(-30).map(r=>`<div>${new Date(r.time).toLocaleTimeString(I18N.locale(),{hour12:false})} · ${esc(r.message)}</div>`).join('');$('#process-log').textContent=p.process_log||tr("尚无模型日志");renderNextAction();renderEditingControls();renderVoices();renderOutputs();renderProposalState();renderTtsRuntime();renderAudition();}
function renderNextAction(){const p=state.project;if(!p)return;const zh=p.segments.filter(r=>r.zh).length,ready=p.segments.length>0&&zh===p.segments.length;const status={source:tr("已导入"),subtitles:!p.segments.length?tr("等待原文"):ready?tr("译文已齐全"):tr("{0}/{1} 句译文",zh,p.segments.length),voices:p.outputs.audio?tr("已生成整片配音"):p.audition?tr("已有本句试听"):tr("先试听一句"),export:p.outputs.preview?tr("可以观看"):p.outputs.video?tr("可生成页面预览"):tr("等待生成成片")};Object.entries(status).forEach(([k,v])=>$('#step-'+k+'-state').textContent=v);$$('.tab').forEach(e=>e.classList.toggle('complete',e.dataset.tab==='source'||e.dataset.tab==='subtitles'&&ready||e.dataset.tab==='voices'&&!!p.outputs.audio||e.dataset.tab==='export'&&!!p.outputs.preview));let caption,action;if(state.dirty){caption=tr("保存校对 →");action=saveSegments;}else if(state.audioDirty){caption=tr("保存声音设置 →");action=saveAudioOptions;}else if(!p.segments.length){caption=tr("识别原文 →");action=()=>run('analyze');}else if(!ready){caption=tr("翻译为目标语言 →");action=()=>run('translate');}else if(!p.outputs.audio){caption=tr("试听与配音 →");action=()=>switchTab('voices');}else if(!p.outputs.preview){caption=tr("生成译制成片 →");action=()=>run('export');}else{caption=tr("观看译制成片 →");action=()=>switchTab('export');}const b=$('#next-action');b.textContent=active(p)?tr("正在处理…"):caption;b.disabled=active(p)||state.saving||!!state.projectUnavailable;b.setAttribute('aria-busy',String(active(p)||state.saving));b.onclick=()=>guard(action);}
function renderEditingControls(){const busy=active(state.project);$('#release-tts').disabled=busy||!state.ttsSession?.alive||state.ttsSession?.busy;['run-all','run-analyze','run-translate','run-resegment','run-optimize','run-dub','run-export','run-preview','save-segments','save-draft-banner','save-audio-options'].forEach(id=>$('#'+id).disabled=busy||state.saving||!!state.projectUnavailable);['source-srt','chinese-srt','background-file','background-mode','background-gain','source-text','chinese-text','voice-text','segment-start','segment-end','segment-speaker','emotion-select','emotion-strength'].forEach(id=>$('#'+id).disabled=busy);$('#emotion-strength').disabled=busy||(currentRow()?.emotion||'inherit')==='inherit';if(!currentRow())['source-text','chinese-text','voice-text','segment-start','segment-end','segment-speaker','emotion-select','emotion-strength'].forEach(id=>$('#'+id).disabled=true);$('#run-audition').disabled=busy||state.saving||!!state.projectUnavailable||!currentRow()?.zh?.trim();$('#run-audition').textContent=busy&&state.project.stage==='audition'?tr("正在生成试听…"):tr("▷ 生成本句试听");$('#run-audition').setAttribute('aria-busy',String(busy&&state.project.stage==='audition'));updateAudioForm();}

function filteredRows(){const q=$('#segment-search').value.trim().toLowerCase();return state.rows.map((r,i)=>({r,i})).filter(({r})=>!q||`${r.text}\n${r.zh}`.toLowerCase().includes(q));}
function renderSentenceList(){const items=filteredRows(),pages=Math.max(1,Math.ceil(items.length/20));state.listPage=Math.min(state.listPage,pages-1);$('#list-count').textContent=tr("{0} 句",items.length);$('#segment-list').innerHTML=items.slice(state.listPage*20,(state.listPage+1)*20).map(({r,i})=>tr("<button class=\"sentence-item {0}\" data-index=\"{1}\" aria-label=\"选择第 {2} 句\"><span class=\"sentence-number\"><span>第 {3} 句 · {4}</span><span>{5}</span></span><span class=\"sentence-snippet\">{6}</span></button>",i===state.selected?'active':'',i,r.id,r.id,clock(r.start),r.zh?tr("已翻译"):tr("待翻译"),esc(r.zh||r.text))).join('')||tr("<p class=\"empty-inline\">没有匹配的句子</p>");$$('.sentence-item').forEach(e=>e.onclick=()=>chooseSentence(Number(e.dataset.index)));$('#segments-page').textContent=`${state.listPage+1} / ${pages}`;$('#segments-prev').disabled=state.listPage===0;$('#segments-next').disabled=state.listPage>=pages-1;}
function chooseSentence(index){state.selected=Math.max(0,Math.min(index,state.rows.length-1));$('#sentence-audio').pause();$('#editor-video').pause();renderSentenceList();renderEditors();}
$('#segment-search').oninput=()=>{state.listPage=0;renderSentenceList();};$('#segments-prev').onclick=()=>{state.listPage--;renderSentenceList();};$('#segments-next').onclick=()=>{state.listPage++;renderSentenceList();};$('#sentence-prev').onclick=()=>chooseSentence(state.selected-1);$('#sentence-next').onclick=()=>chooseSentence(state.selected+1);
function renderEditors(){const r=currentRow();$('#editor-empty').hidden=!!r;$('#editor-fields').hidden=!r;$('#voice-select').innerHTML=state.rows.map((s,i)=>tr("<option value=\"{0}\">第 {1} 句 · {2} · {3}</option>",i,s.id,clock(s.start),esc((s.zh||s.text).slice(0,42)))).join('')||tr("<option value=\"\">先生成字幕和译文</option>");$('#voice-select').value=String(state.selected);$('#sentence-prev').disabled=!r||state.selected===0;$('#sentence-next').disabled=!r||state.selected>=state.rows.length-1;if(!r){$('#source-text').value='';$('#chinese-text').value='';$('#voice-text').value='';$('#voice-source').textContent=tr("识别并翻译后，即可逐句调整情绪。");renderEditingControls();return;}$('#selected-segment-label').textContent=tr("第 {0} 句 / 共 {1} 句",r.id,state.rows.length);$('#selected-segment-time').textContent=`${clock(r.start)} — ${clock(r.end)} · ${r.speaker}`;$('#source-text').value=r.text;$('#chinese-text').value=r.zh;$('#voice-text').value=r.zh;$('#voice-source').textContent=r.text;$('#segment-start').value=r.start;$('#segment-end').value=r.end;$('#segment-speaker').value=r.speaker;$('#emotion-select').value=r.emotion||'inherit';$('#emotion-strength').value=r.emotion_strength||'medium';$('#source-text').setAttribute('aria-label',tr("第 {0} 句原文",r.id));$('#chinese-text').setAttribute('aria-label',tr("第 {0} 句译文",r.id));$('#emotion-help').textContent=(r.emotion||'inherit')==='inherit'?tr("使用参考声音的自然表达，不额外指定情绪。"):tr("只调整当前句的表达，音色仍使用该角色的参考声音。");renderEditingControls();renderAudition();}
function renderEmotionOptions(){const value=$('#emotion-select').value;$('#emotion-select').innerHTML=Object.entries(emotions).map(([id,name])=>`<option value="${id}">${esc(tr(name))}</option>`).join('');$('#emotion-select').value=value||'inherit';}
function edit(field,value){const r=currentRow();if(!r)return;r[field]=value;if(field==='emotion'&&value==='inherit'){delete r.emotion;delete r.emotion_strength;}else if(field==='emotion'&&!r.emotion_strength)r.emotion_strength='medium';setDirty(true);renderEditingControls();}
[['source-text','text'],['chinese-text','zh'],['voice-text','zh'],['segment-speaker','speaker']].forEach(([id,field])=>{$('#'+id).oninput=e=>edit(field,e.target.value);$('#'+id).onchange=()=>{renderSentenceList();if(field==='speaker'){state.voiceKey=null;renderVoices();}};});['start','end'].forEach(k=>$('#segment-'+k).oninput=e=>edit(k,Number(e.target.value)));$('#emotion-select').onchange=e=>{edit('emotion',e.target.value);renderEditors();};$('#emotion-strength').onchange=e=>edit('emotion_strength',e.target.value);$('#voice-select').onchange=e=>chooseSentence(Number(e.target.value));
$('#play-source-cue').onclick=()=>guard(async()=>{const r=currentRow();if(!r)return;const video=$('#editor-video'),id=state.id;$('#original-cue').open=true;const play=async()=>{if(state.id!==id)return;video.currentTime=r.start;state.cueEnd=r.end;await video.play();};if(video.getAttribute('src')!==fileUrl('source.media')){video.onloadedmetadata=()=>guard(play);video.src=fileUrl('source.media');video.load();}else await play();});$('#editor-video').ontimeupdate=()=>{if(state.cueEnd&&$('#editor-video').currentTime>=state.cueEnd)$('#editor-video').pause();};$('#editor-video').onerror=()=>toast(tr("原视频编码暂不支持网页播放，可用本地播放器核对"));

async function saveSegments(){if(state.savingPromise)return state.savingPromise;if(!state.rows.length)throw Error(tr("请先识别或导入原文字幕"));const id=state.id,version=state.selectionVersion,generation=state.editorGeneration,rows=clone(state.rows);state.saving=true;renderEditingControls();const task=(async()=>{try{const p=await api(`/api/projects/${id}/segments`,json('PUT',{segments:rows,revision:state.editorRevision}));if(id!==state.id||version!==state.selectionVersion)return;state.editorRevision=p.edit_revision||0;state.project=p;if(state.editorGeneration===generation){setDirty(false);await refreshCurrent(true);toast(tr("校对与情绪设置已保存"));}else toast(tr("已保存提交时的内容，之后输入的修改仍待保存"));}finally{state.saving=false;state.savingPromise=null;if(id===state.id){renderNextAction();renderEditingControls();}}})();state.savingPromise=task;return task;}
$('#save-segments').onclick=()=>guard(saveSegments);$('#save-draft-banner').onclick=()=>guard(saveSegments);
async function refreshCurrent(full=false){if(!state.id)return;const id=state.id,version=state.selectionVersion;let p;try{p=await api(`/api/projects/${id}`);}catch(error){if(id===state.id&&version===state.selectionVersion&&markProjectUnavailable(error))return;throw error;}if(id!==state.id||version!==state.selectionVersion)return;state.projectUnavailable=null;renderProjectAvailability();const changed=p.status!==state.project?.status||p.stage!==state.project?.stage||p.edit_revision!==state.project?.edit_revision;state.project=p;render((full||changed)&&!state.dirty);}
async function run(stage){const id=state.id;if(state.dirty)await saveSegments();if(state.id!==id)throw Error(tr("项目已切换，请重新操作"));if(state.dirty)throw Error(tr("仍有新输入的校对，请保存后重试"));if(state.audioDirty)await saveAudioOptions();if(state.audioDirty)throw Error(tr("仍有新的声音设置未保存，请保存后重试"));if(state.id!==id)throw Error(tr("项目已切换，请重新操作"));await api(`/api/projects/${id}/run/${stage}`,{method:'POST'});await refreshCurrent(true);switchTab(['translate','analyze','optimize','resegment'].includes(stage)?'subtitles':['export','preview'].includes(stage)?'export':'voices');}
['all','analyze','translate','dub','export','preview','optimize'].forEach(stage=>$('#run-'+stage).onclick=()=>guard(()=>run(stage)));
$('#run-resegment').onclick=()=>guard(async()=>{if(state.project.segments.length&&await confirmChoice(tr("将按原视频重新断句。现有字幕、配音与成片会备份，新字幕需要重新翻译和配音。"),{title:tr("重新断句"),proceed:tr("备份并重新断句")})==='cancel')return;await run('resegment');});$('#cancel-job').onclick=()=>guard(async()=>{await api(`/api/projects/${state.id}/cancel`,{method:'POST'});await refreshCurrent();});
async function runAudition(){const row=currentRow();if(!row?.zh?.trim())throw Error(tr("先填写这句的译文配音稿"));const id=state.id,version=state.selectionVersion;const result=await api(`/api/projects/${id}/audition`,json('POST',{segment:clone(row),revision:state.editorRevision}));if(id!==state.id||version!==state.selectionVersion)return;state.auditionPending={project:id,request:result.request_id};await refreshCurrent(false);toast(tr("正在生成本句原速试听，当前草稿不会替换成片"));}
$('#run-audition').onclick=()=>guard(runAudition);
function expressionSignature(r){return JSON.stringify([r?.id,String(r?.zh||'').trim(),String(r?.speaker||'').trim(),r?.emotion||'inherit',(r?.emotion||'inherit')==='inherit'?'medium':r?.emotion_strength||'medium']);}
function renderAudition(){if(!state.project)return;const sample=state.project.audition,row=currentRow(),visible=!!sample&&sample.segment.id===row?.id;$('#audition-result').hidden=!visible;if(!visible){$('#sentence-audio').pause();return;}const same=expressionSignature(row)===expressionSignature(sample.segment);$('#audition-label').textContent=tr("第 {0} 句 · {1}{2}",sample.segment.id,tr(emotions[sample.segment.emotion||'inherit']),sample.segment.emotion?' / '+tr(strengths[sample.segment.emotion_strength||'medium']):'');$('#audition-state').textContent=same?tr("与当前配音稿一致"):tr("试听的是之前版本");$('#audition-note').textContent=same?tr("点击播放核对情绪；整片仍需保存设置并重新生成。"):tr("当前文字、角色或情绪已有修改，请重新生成试听。");const url=fileUrl(sample.file);if($('#sentence-audio').getAttribute('src')!==url){$('#sentence-audio').src=url;$('#sentence-audio').load();}}

function renderVoices(){const p=state.project;if(!p)return;const speakers=[...new Set((state.rows.length?state.rows:p.segments).map(s=>s.speaker))];const key=JSON.stringify([state.id,speakers,p.references,active(p)]);if(key!==state.voiceKey){state.voiceKey=key;$('#speaker-cards').innerHTML=speakers.map((s,i)=>tr("<div class=\"speaker-card\"><div class=\"card-heading\"><h3>角色 {0}</h3><span class=\"badge\">{1}</span></div>{2}<label class=\"file-button\">{3}<input type=\"file\" accept=\"audio/*,video/*\" data-speaker=\"{4}\" {5}></label></div>",i+1,p.references[s]?tr("参考音已就绪"):tr("待准备"),'<div class="speaker-reference">'+(p.references[s]?`<audio controls preload="none" src="${fileUrl(p.references[s])}"></audio>`:`<p class="subtle">${speakers.length===1?tr("首次试听或配音时自动从原片取样。"):tr("请上传这个角色的干净录音。")}</p>`)+'</div>',p.references[s]?tr("更换参考音"):tr("上传参考音"),esc(s),active(p)?'disabled':'')).join('')||tr("<p class=\"subtle\">生成字幕后，这里会显示角色。</p>");$$('#speaker-cards input').forEach(el=>el.onchange=()=>guard(async()=>{if(!el.files[0])return;const id=state.id,version=state.selectionVersion,body=new FormData();body.append('audio',el.files[0]);await api(`/api/projects/${id}/reference/${el.dataset.speaker}`,{method:'POST',body});if(id!==state.id||version!==state.selectionVersion)return;await refreshCurrent(false);toast(tr("参考音已保存，相关角色需重新生成配音"));}));}$('#reference-count').textContent=tr("{0} / {1} 已准备",speakers.filter(s=>p.references[s]).length,speakers.length);const audio=$('#dub-audio'),has=!!p.outputs.audio;audio.hidden=!has;$('#audio-placeholder').hidden=has;if(has){const url=fileUrl(p.outputs.audio)+`?v=${p.dub_signature||''}`;if(audio.getAttribute('src')!==url)audio.src=url;}else if(audio.getAttribute('src')){audio.pause();audio.removeAttribute('src');audio.load();}$('#background-state').textContent=p.background?tr("已上传独立背景音"):tr("未上传");$('#remove-background').hidden=!p.background;if(!state.audioDirty){$('#background-mode').value=p.background_mode||(p.background?'custom':'none');$('#background-gain').value=p.background_gain??1;$('#audio-dirty').hidden=true;}updateAudioForm();}
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

function openImport(){$('#import-error').textContent='';$('#import-dialog').showModal();}$('#new-project').onclick=openImport;$('#welcome-import').onclick=openImport;$$('.close-dialog').forEach(e=>e.onclick=()=>e.closest('dialog').close());
$('#import-form').onsubmit=async e=>{e.preventDefault();const b=$('#import-submit');b.disabled=true;b.setAttribute('aria-busy','true');b.textContent=tr("正在导入视频…");$('#import-error').textContent='';try{const form=new FormData(e.target),video=form.get('video');let p;if(video&&video.size){form.delete('path');p=await api('/api/projects/upload',{method:'POST',body:form});}else{const value=Object.fromEntries(form);delete value.video;p=await api('/api/projects/import',json('POST',value));}$('#import-dialog').close();await selectProject(p.id);switchTab('source');}catch(error){$('#import-error').textContent=error.message;}finally{b.disabled=false;b.setAttribute('aria-busy','false');b.textContent=tr("创建项目");}};
async function srtUpload(input,kind){if(!input.files[0])return;if(state.dirty&&await confirmChoice(tr("导入会替换当前字幕，并放弃未保存的校对。"),{title:tr("导入字幕"),proceed:tr("导入并替换")})==='cancel'){input.value='';return;}const id=state.id,version=state.selectionVersion,body=new FormData();body.append('subtitle',input.files[0]);body.append('kind',kind);await api(`/api/projects/${id}/subtitles`,{method:'POST',body});if(id!==state.id||version!==state.selectionVersion)return;setDirty(false);input.value='';await refreshCurrent(true);switchTab('subtitles');toast(tr("字幕已导入"));}$('#source-srt').onchange=()=>guard(()=>srtUpload($('#source-srt'),'source'));$('#chinese-srt').onchange=()=>guard(()=>srtUpload($('#chinese-srt'),'zh'));
async function openSettings(){if(state.settingsLoaded){showSection('settings');return;}const s=await api('/api/settings');state.settings=s;Object.entries(s).forEach(([k,v])=>{const el=$('#settings-form').elements[k];if(el)el.value=v;});$('#settings-error').textContent='';state.settingsLoaded=true;state.settingsDirty=false;showSection('settings');updateSettingsDraft();}$('#open-settings').onclick=()=>guard(openSettings);$('#environment-settings').onclick=()=>guard(openSettings);
$('#settings-form').onsubmit=async e=>{e.preventDefault();try{const data={...state.settings,...Object.fromEntries(new FormData(e.target))};['max_speed','max_lead','max_lag','audio_workers','tts_batch_size','tts_idle_minutes','translation_batch'].forEach(k=>data[k]=Number(data[k]));data.translation_compact=[true,'true'].includes(data.translation_compact);state.settings=await api('/api/settings',json('PUT',data));state.settingsDirty=false;updateSettingsDraft();await diagnostics();toast(tr("设置已保存，下次任务生效"));}catch(error){$('#settings-error').textContent=error.message;}};
window.addEventListener('beforeunload',e=>{if(state.dirty||state.audioDirty){e.preventDefault();e.returnValue='';}});
async function poll(){if(state.poll)return;state.poll=true;try{await refreshCurrent();await refreshList();await refreshTtsSession();}catch{$('#job-status').textContent=tr("连接本地服务中…");}finally{state.poll=false;}}

function renderTtsRuntime(){
  const r=state.project?.tts_runtime;$('#tts-runtime').hidden=!r;if(!r)return;
  $('#tts-phase').textContent=tr(r.phase||tr("等待配音"));
  $('#tts-batch').textContent=tr("{0} 个模型实例 · 当前批量 {1} / 上限 {2} · 本次最大批量 {3}",r.instances??1,r.actual_batch||'—',r.requested_batch||'—',r.max_actual_batch||'—');
  $('#tts-counts').textContent=tr("新生成 {0} / {1} 段 · 复用 {2} 段",r.completed||0,r.total||0,r.reused||0);
  $('#tts-reason').textContent=tr(r.fallback_reason||r.last_fallback_reason||"同角色、相近长度的文本优先合批；显存不足自动减小批量。");
  const phases=Object.entries(r.stage_seconds||{}).map(([k,v])=>tr("{0} {1} 秒",tr(k),Number(v).toFixed(1)));
  if(r.phase==='合成完成'&&r.seconds!=null){const other=Math.max(0,r.seconds-Object.values(r.stage_seconds||{}).reduce((a,b)=>a+Number(b),0));phases.push(tr("其他准备与读写 {0} 秒",other.toFixed(1)));}
  $('#tts-timing').textContent=[r.model_reused?tr("模型已复用，无需重新加载"):r.model_load_seconds!=null?tr("模型加载 {0} 秒",Number(r.model_load_seconds).toFixed(1)):'',r.model_load_reason?tr("加载原因：{0}",tr(r.model_load_reason)):'',...phases,r.seconds!=null?tr("合成累计 {0} 秒",Number(r.seconds).toFixed(1)):'',r.peak_allocated_mb?tr("PyTorch 峰值 {0} GiB",(r.peak_allocated_mb/1024).toFixed(1)):''].filter(Boolean).join(' · ');
}
function renderProposalState(){
  const p=state.project,m=p?.translation_proposal;
  $('#translation-review').hidden=!m;
  $('#run-optimize').disabled=active(p)||!p.segments.some(s=>s.zh);
  if(!m){state.proposal=null;return;}
  if(state.proposalProject!==state.id||state.proposal?.id!==m.id){
    state.proposal=null;state.proposalProject=state.id;state.proposalSelected=new Set();$('#proposal-body').hidden=true;
  }
  $('#proposal-summary').textContent=m.status==='applied'?tr("所选建议已应用。请重新生成译制配音和成片；其余建议需重新审校。"):tr("已审校 {0} 段，其中 {1} 段有调整。原译文和成片尚未替换。",m.total,m.changed);
  $('#show-proposal').disabled=active(p);
  if(state.proposal)updateProposalControls();
}
async function showProposal(){
  const id=state.id,version=state.selectionVersion,meta=state.project.translation_proposal;
  const proposal=await api(`/api/projects/${id}/translation-proposal`);
  if(state.id!==id||state.selectionVersion!==version||state.project.translation_proposal?.id!==meta.id)return;
  if(!proposal)throw Error(tr("尚未生成译文优化建议"));
  if(state.proposal?.id!==proposal.id){
    state.proposalSelected=new Set(proposal.items.filter(r=>r.before!==r.after).map(r=>r.id));state.proposalPage=0;
  }
  state.proposal=proposal;state.proposalProject=id;$('#proposal-body').hidden=false;renderProposalItems();if(!$('#proposal-dialog').open)$('#proposal-dialog').showModal();
}
function renderProposalItems(){
  const p=state.proposal;if(!p)return;
  const items=p.items.filter(r=>r.before!==r.after),pages=Math.max(1,Math.ceil(items.length/12));
  state.proposalPage=Math.max(0,Math.min(state.proposalPage||0,pages-1));
  $('#proposal-items').innerHTML=items.slice(state.proposalPage*12,(state.proposalPage+1)*12).map(r=>tr("<article class=\"proposal-item\"><div class=\"proposal-item-title\"><label class=\"check-label\"><input type=\"checkbox\" data-proposal-id=\"{0}\" aria-label=\"采用第 {1} 段优化\" {2}>第 {3} 段</label><small class=\"subtle\">预计朗读 {4} 秒 / 字幕窗口 {5} 秒</small></div><p class=\"proposal-source\">{6}</p><div class=\"proposal-grid\"><div><b>当前译文</b><p>{7}</p></div><div class=\"proposal-after\"><b>优化建议</b><p>{8}</p></div></div><p class=\"subtle\">{9}</p></article>",r.id,r.id,state.proposalSelected.has(r.id)?'checked':'',r.id,r.estimated_seconds,r.seconds,esc(r.source),esc(r.before),esc(r.after),esc(r.reason||tr("调整译文语序与表达；请结合原文核对。")))).join('')||tr("<p class=\"subtle\">本次审校没有需要修改的译文。</p>");
  $$('[data-proposal-id]').forEach(input=>input.onchange=()=>{const id=Number(input.dataset.proposalId);if(input.checked)state.proposalSelected.add(id);else state.proposalSelected.delete(id);updateProposalControls();});
  $('#proposal-page').textContent=`${state.proposalPage+1} / ${pages}`;
  $('#proposal-prev').disabled=state.proposalPage===0;$('#proposal-next').disabled=state.proposalPage>=pages-1;
  updateProposalControls();
}
function updateProposalControls(){
  const p=state.proposal;if(!p)return;
  const meta=state.project.translation_proposal,stale=p.stale||meta?.status!=='ready'||p.base_revision!==(state.project.edit_revision||0);
  const total=p.items.filter(r=>r.before!==r.after).length,count=state.proposalSelected.size;
  const applied=meta?.status==='applied',busy=!!state.applyingProposal;
  $('#proposal-selected').textContent=applied?tr("所选建议已应用"):stale?tr("建议已过期，请重新生成。"):tr("已选择 {0} / {1} 段",count,total);
  $('#proposal-all').checked=total>0&&count===total;$('#proposal-all').indeterminate=count>0&&count<total;
  $('#proposal-all').disabled=stale||active(state.project)||state.applyingProposal;
  $$('[data-proposal-id]').forEach(input=>input.disabled=stale||active(state.project)||state.applyingProposal);
  $('#apply-proposal').disabled=stale||!count||state.dirty||active(state.project)||state.applyingProposal;
  const button=$('#apply-proposal');
  button.textContent=busy?tr("正在应用…"):applied?tr("已应用"):stale?tr("建议已过期"):tr("应用所选优化（{0}）",count);
  button.setAttribute('aria-busy',String(busy));
  const hint=busy?tr("正在保存所选译文，请稍候。"):active(state.project)?tr("当前项目正在处理，完成后可应用建议。"):applied?tr("已应用成功。点击“一键完成”重新生成译制配音和成片。"):stale?tr("字幕已经更新，请重新生成优化建议。"):state.dirty?tr("有未保存的校对，请先保存；译文发生变化后需重新生成建议。"):!count?tr("请先勾选需要采用的优化建议。"):tr("应用后需重新生成译制配音和成片；未修改片段可复用。");
  button.title=hint;
  $('#proposal-action-hint').textContent=hint;
}
$('#show-proposal').onclick=()=>guard(showProposal);
$('#proposal-all').onchange=()=>{state.proposalSelected=$('#proposal-all').checked?new Set(state.proposal.items.filter(r=>r.before!==r.after).map(r=>r.id)):new Set();renderProposalItems();};
$('#proposal-prev').onclick=()=>{state.proposalPage--;renderProposalItems();};
$('#proposal-next').onclick=()=>{state.proposalPage++;renderProposalItems();};
$('#apply-proposal').onclick=()=>guard(async()=>{
  if(state.dirty)throw Error(tr("请先保存校对，再重新生成建议，避免覆盖未保存的内容"));
  const id=state.id,proposal=state.proposal;state.applyingProposal=true;updateProposalControls();
  try{
    await api(`/api/projects/${id}/translation-proposal/apply`,json('POST',{proposal_id:proposal.id,revision:proposal.base_revision,ids:[...state.proposalSelected]}));
    if(state.id!==id)return;
    await refreshCurrent(true);$('#proposal-dialog').close();toast(tr("所选优化已应用，请重新生成译制配音和成片；未修改片段继续复用"));
  }finally{state.applyingProposal=false;if(state.id===id)updateProposalControls();}
});

guard(async()=>{await I18N.ready();renderEmotionOptions();await diagnostics();await refreshList();await refreshTtsSession();let remembered;try{remembered=localStorage.getItem('btv-project');}catch{}showSection('projects');});setInterval(poll,2000);

async function refreshTtsSession(){
  const s=await api('/api/tts-session');state.ttsSession=s;
  $('#tts-session-state').textContent=s.alive?(s.busy?tr(s.phase):tr("保温中 · 空闲 {0} 分钟后释放",Math.ceil(s.idle_seconds/60))):tr("未加载 · {0}",tr(s.reason));
  $('#release-tts').disabled=!s.alive||s.busy||active(state.project);
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
  else $('#page-name').textContent=tr(tr("项目首页"));
  if(state.proposal)renderProposalItems();
  guard(async()=>{await diagnostics();await refreshTtsSession();});
};
