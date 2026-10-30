"use strict";
let recordingEpoch=0,recordingToken=null,recordingTimer=null,recordingDevices=null,recordingSource='upload',recordingFetching=false;
function setRecordingBusy(value){state.voiceRecorderBusy=value;$('#save-voice-preset').disabled=value||!!state.presetSaving||(recordingSource!=='upload'&&!state.recordedVoiceFile&&!state.presetContext?.voice&&!state.presetContext?.source);$('#voice-record-start').disabled=value;$('#voice-record-device').disabled=value;$$('[data-record-source]').forEach(button=>button.disabled=value);$('#voice-record-stop').hidden=!value;$('#voice-record-cancel').hidden=!value;}
function resetVoiceRecorder(){
  recordingEpoch++;if(recordingTimer)clearTimeout(recordingTimer);recordingTimer=null;recordingFetching=false;
  const token=recordingToken;recordingToken=null;if(token)fetch('/api/recording/'+token+'/cancel',{method:'POST',keepalive:true}).catch(()=>{});
  recordingSource='upload';recordingDevices=null;state.voiceRecorderBusy=false;$('#voice-acquisition').hidden=!!state.presetContext?.source;
  $('#voice-record-panel').hidden=true;$('#voice-record-status').textContent='';$('#voice-record-time').textContent='0.0 / 29 s';$('#voice-record-level').value=0;
  $$('[data-record-source]').forEach(button=>{button.disabled=false;button.classList.toggle('selected',button.dataset.recordSource==='upload');});setRecordingBusy(false);
}
async function cancelVoiceRecording(){
  recordingEpoch++;if(recordingTimer)clearTimeout(recordingTimer);recordingTimer=null;recordingFetching=false;
  const token=recordingToken;recordingToken=null;setRecordingBusy(false);$('#voice-record-level').value=0;
  if(token)await api('/api/recording/'+token+'/cancel',{method:'POST'}).catch(()=>{});
}
async function selectRecordingSource(source){
  if(state.voiceRecorderBusy)return;recordingSource=source;$('#voice-preset-form').elements.audio.required=source==='upload'&&!state.recordedVoiceFile&&!state.presetContext?.voice&&!state.presetContext?.source;setRecordingBusy(false);
  $$('[data-record-source]').forEach(button=>button.classList.toggle('selected',button.dataset.recordSource===source));
  $('#voice-upload-label').hidden=source!=='upload';$('#voice-record-panel').hidden=source==='upload';
  if(source==='upload')return;
  const epoch=recordingEpoch;$('#voice-record-status').textContent=tr('正在读取录音设备…');$('#voice-record-start').disabled=true;
  const devices=recordingDevices||await api('/api/recording/devices');if(epoch!==recordingEpoch||source!==recordingSource)return;recordingDevices=devices;
  $('#voice-record-device').innerHTML=(devices[source]||[]).map(d=>`<option value="${d.id}" ${d.default?'selected':''}>${esc(d.name)}</option>`).join('');
  const available=devices.available&&(devices[source]||[]).length;
  $('#voice-record-start').disabled=!available;$('#voice-record-status').textContent=available?tr(source==='system'?'录制所选扬声器或耳机正在播放的声音，不包含画面。':'请对着所选话筒录制一段清晰的参考声音。'):tr(devices.error||'没有可用的录音设备');
}
$$('[data-record-source]').forEach(button=>button.onclick=()=>guard(()=>selectRecordingSource(button.dataset.recordSource)));
$('#voice-record-start').onclick=()=>guard(async()=>{
  if(state.voiceRecorderBusy)return;if(recordingToken)await cancelVoiceRecording();const epoch=++recordingEpoch;setRecordingBusy(true);state.presetDirty=true;$('#voice-record-status').textContent=tr('正在启动录音…');$('#voice-preset-error').textContent='';
  try{
    const result=await api('/api/recording/start',json('POST',{kind:recordingSource,device:Number($('#voice-record-device').value)}));
    if(epoch!==recordingEpoch){await api('/api/recording/'+result.id+'/cancel',{method:'POST'});return;}
    recordingToken=result.id;pollRecording(epoch);
  }catch(error){if(epoch===recordingEpoch){setRecordingBusy(false);$('#voice-record-status').textContent=error.message;}}
});
async function pollRecording(epoch){
  if(epoch!==recordingEpoch||!recordingToken||recordingFetching)return;recordingFetching=true;
  try{
    const token=recordingToken,result=await api('/api/recording/'+token);if(epoch!==recordingEpoch)return;
    $('#voice-record-time').textContent=Number(result.duration).toFixed(1)+' / 29 s';$('#voice-record-level').value=result.level;
    $('#voice-record-status').textContent=result.status==='recording'?tr('正在录音，停止后可试听'):result.status==='starting'?tr('正在启动录音…'):result.status==='stopping'?tr('正在整理录音…'):tr(result.error||'');
    if(result.status==='ready'){
      const response=await fetch('/api/recording/'+token+'/audio');if(!response.ok)throw Error(tr('录音文件读取失败，请重新录制'));
      const blob=await response.blob();if(epoch!==recordingEpoch)return;
      state.recordedVoiceFile=new File([blob],'reference-recording.wav',{type:'audio/wav'});$('#voice-preset-form').elements.audio.required=false;
      if(state.presetObjectUrl)URL.revokeObjectURL(state.presetObjectUrl);state.presetObjectUrl=URL.createObjectURL(blob);
      const player=$('#voice-upload-preview');player.pause();player.src=state.presetObjectUrl;player.hidden=false;
      state.presetDirty=true;setRecordingBusy(false);$('#voice-record-level').value=0;$('#voice-record-status').textContent=tr('录音已准备好，请试听后保存音色。');return;
    }
    if(['failed','cancelled'].includes(result.status)){setRecordingBusy(false);$('#voice-record-level').value=0;return;}
    recordingTimer=setTimeout(()=>pollRecording(epoch),250);
  }catch(error){if(epoch===recordingEpoch){$('#voice-record-status').textContent=error.message;await cancelVoiceRecording();}}
  finally{if(epoch===recordingEpoch)recordingFetching=false;}
}
$('#voice-record-stop').onclick=()=>guard(async()=>{if(recordingToken)await api('/api/recording/'+recordingToken+'/stop',{method:'POST'});});
$('#voice-record-cancel').onclick=()=>guard(async()=>{await cancelVoiceRecording();$('#voice-record-status').textContent=tr('已取消录音，原先选择的参考音保持不变。');});
window.addEventListener('beforeunload',()=>{if(recordingToken)fetch('/api/recording/'+recordingToken+'/cancel',{method:'POST',keepalive:true}).catch(()=>{});});
