"use strict";
function formatTtsSession(s){
  if(s.control?.status==='cancelling')return tr('正在取消模型加载…');
  if(s.control?.status==='queued')return tr('模型加载已排队');
  if(s.control?.status==='loading')return tr('正在准备模型 · {0}',tr(s.phase==='未加载'?'启动配音进程':s.phase));
  if(s.service_managed)return tr(s.alive?'本地服务自行管理；内置模型仍驻留':'本地服务自行管理模型加载与释放');
  if(s.busy)return tr(s.phase);
  if(!s.loaded)return tr('未加载 · {0}',tr(s.reason));
  if(s.keep_loaded)return tr(!s.retention?.keep_loaded&&s.retention?.minutes===0?'已预加载 · 下次配音完成后卸载':'已加载 · 持续保温，可手动卸载');
  const seconds=Math.max(0,Math.ceil(s.idle_seconds||0));
  return tr('已加载 · 空闲倒计时 {0}',Math.floor(seconds/60)+':'+String(seconds%60).padStart(2,'0'));
}
function modelPathsDirty(){
  if(!state.settingsLoaded)return false;
  const saved=state.modelBackendSettings||state.settings,form=$('#settings-form');
  return ['tts_provider','index_root','index_model','index_version'].some(name=>form.elements[name].value!==String(saved[name]));
}
function renderTtsControls(s=state.ttsSession){
  if(!s)return;
  const loading=['queued','loading','cancelling'].includes(s.control?.status),pending=!!state.ttsControlPending;
  const dirty=modelPathsDirty(),nativeDraft=!state.settingsLoaded||$('#settings-form').elements.tts_provider.value==='indextts';
  const blocked=s.busy||s.application_busy||pending;
  $('#settings-tts-state').textContent=formatTtsSession(s);
  const model=s.model||s.control;
  $('#settings-tts-model').textContent=model?.model?model.model+' · IndexTTS '+model.version:'';
  $('#settings-tts-error').textContent=s.control?.status==='failed'?s.control.error||tr('手动加载失败'):'';
  for(const id of ['settings-load-tts','load-tts']){$('#'+id).disabled=blocked||s.service_managed||dirty||!nativeDraft;$('#'+id).title=tr(dirty?'先保存模型路径，再加载模型':'仅加载模型权重，不生成试听音频。');}
  for(const id of ['settings-unload-tts','release-tts'])$('#'+id).disabled=blocked||!s.alive;
  for(const id of ['settings-cancel-tts','cancel-tts-load']){$('#'+id).hidden=!loading;$('#'+id).disabled=pending||s.control?.status==='cancelling';}
  $('#tts-load-hint').textContent=tr(!nativeDraft||s.service_managed?'外部接口未提供标准模型加载卸载协议，请在对应服务中管理。':dirty?'先保存模型路径，再加载模型':'加载使用已保存的模型路径；修改路径后请先保存设置。');
  $('#tts-retention-fields').hidden=!nativeDraft;
  if(state.settingsLoaded){
    const form=$('#settings-form'),keep=form.elements.tts_keep_loaded.value==='true';
    form.elements.tts_idle_minutes.readOnly=keep;
  }
  $('#save-tts-retention').disabled=!!state.ttsRetentionSaving||!!state.settingsSaving;
}
async function ttsControl(action){
  if(state.ttsControlPending)return;
  state.ttsControlPending=true;renderTtsControls();
  try{
    state.ttsSession=await api('/api/tts-session/'+action,{method:'POST'});
    toast(tr(action==='load'?'模型准备请求已提交':action==='cancel-load'?'已请求取消加载':'模型已卸载'));
    await refreshTtsSession();
  }finally{state.ttsControlPending=false;renderTtsControls();}
}
for(const id of ['settings-load-tts','load-tts'])$('#'+id).onclick=()=>guard(()=>ttsControl('load'));
for(const id of ['settings-unload-tts','release-tts'])$('#'+id).onclick=()=>guard(()=>ttsControl('release'));
for(const id of ['settings-cancel-tts','cancel-tts-load'])$('#'+id).onclick=()=>guard(()=>ttsControl('cancel-load'));
$('#settings-refresh-tts').onclick=()=>guard(refreshTtsSession);
$('#read-tts-control-logs').onclick=()=>guard(async()=>{const log=await api('/api/tts-session/logs');$('#tts-control-log').textContent=log.content||tr('暂无加载日志');});
$('#save-tts-retention').onclick=()=>guard(async()=>{
  if(state.ttsRetentionSaving||state.settingsSaving||!state.settingsLoaded)return;
  const form=$('#settings-form'),minutes=form.elements.tts_idle_minutes;
  if(!minutes.value.trim())throw Error(tr('请填写保温时长，0 表示任务后卸载'));
  if(!minutes.reportValidity())return;
  const payload={tts_idle_minutes:Number(minutes.value),tts_keep_loaded:form.elements.tts_keep_loaded.value==='true'};
  state.ttsRetentionSaving=true;renderTtsControls();$('#tts-retention-status').textContent=tr('正在应用保温设置…');
  try{
    const data=await api('/api/tts-session/retention',json('PUT',payload));
    for(const name of ['tts_idle_minutes','tts_keep_loaded'])state.settings[name]=data.settings[name];
    state.modelBackendSettings=data.settings;
    const current=settingsDraft();state.settingsDirty=Object.keys(current).some(name=>JSON.stringify(current[name])!==JSON.stringify(state.settings[name]));
    updateSettingsDraft();state.ttsSession=data.session;
    $('#tts-retention-status').textContent=tr(data.applied?'保温设置已应用':'保温设置已保存，当前任务保持原策略，下次任务生效');
    await refreshTtsSession();
  }finally{state.ttsRetentionSaving=false;renderTtsControls();}
});
guard(refreshTtsSession);
