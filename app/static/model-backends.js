"use strict";
function settingsDraft(){
  const data={...state.settings,...Object.fromEntries(new FormData($('#settings-form')))};
  for(const name of ['max_speed','max_lead','max_lag','alignment_fallback_speed','alignment_fallback_lead','alignment_fallback_lag','audio_workers','tts_batch_size','tts_idle_minutes','translation_batch','translation_parallel','model_service_timeout'])data[name]=Number(data[name]);
  if(Object.hasOwn(data,'translation_remote_batch'))data.translation_remote_batch=Number(data.translation_remote_batch);
  for(const name of ['translation_compact','translation_json_mode','translation_api_stream','tts_keep_loaded'])data[name]=[true,'true'].includes(data[name]);
  if($('#clear-translation-api-key').checked)data.translation_api_key='';
  else if(!data.translation_api_key?.trim())delete data.translation_api_key;
  return data;
}
function updateLocalTranslationPicker(){
  const picker=$('#local-translation-models'),path=$('#settings-form').elements.translation_model.value;
  picker.value=Array.from(picker.options).some(option=>option.value===path)?path:'';
  $('#custom-translation-model').hidden=!!picker.value;
}
$('#local-translation-models').oninput=e=>{
  const picker=e.target,path=$('#settings-form').elements.translation_model;
  if(picker.value)path.value=picker.value;
  else {path.value='';$('#custom-translation-model').hidden=false;path.focus();}
};
async function refreshLocalTranslationModels(){
  const picker=$('#local-translation-models'),custom=picker.querySelector('option[value=""]');
  try{
    const paths=await api('/api/model-backends/local-translation',{cache:'no-store'});
    picker.replaceChildren(...paths.map(path=>new Option(path.slice('models/'.length),path)),custom);
    $('#local-translation-model-status').textContent=paths.length?'':tr('models 目录中没有找到 GGUF 文件');
  }catch{
    $('#local-translation-model-status').textContent=tr('无法加载本地模型列表，可手动填写 GGUF 路径');
  }
  updateLocalTranslationPicker();
}
function updateModelControls(){
  const form=$('#settings-form');
  const promptSupported=Object.hasOwn(state.settings||{},'translation_system_prompt');
  $('#translation-system-prompt-field').hidden=!promptSupported;
  form.elements.translation_system_prompt.disabled=!promptSupported;
  for(const stage of ['asr','translation','tts']){
    const value=form.elements[stage+'_provider'].value;
    const native=stage==='tts'?value==='indextts':value!=='openai';
    for(const group of $$(`[data-backend-stage="${stage}"]`)){
      if(group.dataset.backendProvider)group.hidden=group.dataset.backendProvider!==value;
      else if(stage==='translation'&&group.dataset.backendKind==='native'&&form.elements.segmentation_provider.value==='llama_cpp')group.hidden=false;
      else if(['bilibili','openai_remote'].includes(value))group.hidden=true;
      else group.hidden=value!=='auto'&&(group.dataset.backendKind==='native'?!native:native);
    }
  }
  const translation=form.elements.translation_provider.value;
  const remoteBatch=translation==='openai_remote'&&Object.hasOwn(state.settings||{},'translation_remote_batch');
  $('#remote-translation-batch-field').hidden=!remoteBatch;
  form.elements.translation_remote_batch.disabled=!remoteBatch;
  $('#subtitle-translation-batch-field').hidden=remoteBatch;
  const managedTranslation=translation==='llama_cpp'||(translation==='auto'&&!form.elements.translation_url.value.trim());
  $('[data-local-translation-parallel]').hidden=!managedTranslation&&translation!=='openai_remote';
  $('[data-local-translation-layout]').hidden=!managedTranslation&&translation!=='openai_remote';
  updateLocalTranslationPicker();
  const savedKey=state.settings?.translation_api_key_configured&&form.elements.translation_api_url.value.trim()===state.settings.translation_api_url;
  $('#clear-translation-api-key-label').hidden=!savedKey;
  if(!savedKey)$('#clear-translation-api-key').checked=false;
  form.elements.translation_api_key.placeholder=tr(savedKey?'已保存密钥，留空沿用':'填写 API 密钥');
  const tts=form.elements.tts_provider.value;
  $('[data-service-voice]').hidden=tts!=='openai_speech';
  if(typeof renderTtsControls==='function')renderTtsControls(state.ttsSession);
  $('#tts-capability-note').textContent=tr(tts==='indextts'?'IndexTTS 使用本地参考音，支持音色克隆与情绪。':tts==='openai_speech'?'普通语音接口使用服务音色 ID，所有角色使用同一音色；不上传参考音，不支持情绪控制。':'参考音桥接通过 BTV v1 协议传递参考音与情绪，需要服务端适配。');
}
const openSettingsBeforeBackends=openSettings;
openSettings=async function(){await openSettingsBeforeBackends();updateModelControls();await refreshLocalTranslationModels();};
$('#open-settings').onclick=()=>guard(openSettings);$('#environment-settings').onclick=()=>guard(openSettings);
const updateSettingsBeforeBackends=updateSettingsDraft;
updateSettingsDraft=function(){updateSettingsBeforeBackends();if(state.settingsLoaded)updateModelControls();};
$('#settings-form').addEventListener('input',()=>{
  for(const node of $$('[data-backend-result]'))node.textContent=tr('设置已经改变，请重新检查');
});
for(const stage of ['asr','translation','tts']){
  const input=$('#settings-form').elements[stage+'_name'];input.setAttribute('list',stage+'-model-options');
}
for(const button of $$('[data-check-backend]'))button.onclick=async()=>{
  if(button.disabled)return;
  const stage=button.dataset.checkBackend,result=$(`[data-backend-result="${stage}"]`),generation=state.settingsGeneration||0;
  const draft=settingsDraft(),draftKey=JSON.stringify(draft);
  const current=()=>generation===(state.settingsGeneration||0)&&draftKey===JSON.stringify(settingsDraft());
  button.disabled=true;result.textContent=tr('检查中…');
  try{
    const data=await api('/api/model-backends/check',json('POST',{stage,settings:draft}));
    if(!current()){result.textContent=tr('设置已经改变，请重新检查');return;}
    result.textContent=tr(data.message);result.dataset.ok=String(data.ok);
    $('#'+stage+'-model-options').replaceChildren(...(data.models||[]).map(id=>{const option=document.createElement('option');option.value=id;return option;}));
  }catch(error){result.textContent=current()?error.message:tr('设置已经改变，请重新检查');result.dataset.ok='false';}
  finally{button.disabled=false;}
};
function renderBackendNotices(){
  for(const [id,anchor] of [['video-backend-notice',$('#speaker-cards')],['novel-backend-notice',$('#novel-voice-help')]]){
    let node=$('#'+id);if(!node){node=document.createElement('p');node.id=id;node.className='backend-capabilities';anchor.before(node);}
    node.hidden=!fixedServiceVoice();node.textContent=tr('普通语音接口已启用：使用设置中的服务音色，参考音不会用于克隆；显式情绪设置会被拒绝。');
  }
}
async function refreshBackendStatus(){state.modelBackendSettings=await api('/api/settings');renderBackendNotices();if(typeof renderNovelVoices==='function')renderNovelVoices();}
const backendEmotionsBefore=renderEmotionOptions;
renderEmotionOptions=function(){backendEmotionsBefore();renderBackendNotices();if(state.settingsLoaded)updateModelControls();};
guard(refreshBackendStatus);
