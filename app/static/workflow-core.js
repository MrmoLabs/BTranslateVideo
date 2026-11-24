/* Shared action boundary; usable without a browser for race-condition checks. */
(function(root){
  'use strict';
  const stages=Object.freeze({source:{next:'subtitles'},subtitles:{next:'voices'},voices:{next:'export'},export:{next:null}});
  function context(state){return {id:state.id,version:state.selectionVersion};}
  function current(state,token){return !!token.id&&token.id===state.id&&token.version===state.selectionVersion;}
  function coordinator(deps){
    const pending=new Map();
    return {
      context:()=>context(deps.state),current:token=>current(deps.state,token),
      async prepare({audio=false}={}){
        const token=context(deps.state);
        const check=()=>{if(!current(deps.state,token))throw Error(deps.message('项目已切换，请重新操作'));};
        check();if(deps.state.dirty)await deps.saveText();check();
        if(deps.state.dirty)throw Error(deps.message('仍有新输入的修改未保存，请保存后重试'));
        if(audio&&deps.state.audioDirty)await deps.saveAudio();check();
        if(audio&&deps.state.audioDirty)throw Error(deps.message('仍有新的声音设置未保存，请保存后重试'));
        if(deps.state.dirty)throw Error(deps.message('仍有新输入的修改未保存，请保存后重试'));
        return token;
      },
      async task(key,fn,options){
        // Coalesce double-clicks only within the same selection, never across projects.
        const token=context(deps.state),identity=JSON.stringify([token.id,token.version,key]);
        if(pending.has(identity))return pending.get(identity);
        const promise=(async()=>fn(await this.prepare(options)))();pending.set(identity,promise);
        try{return await promise;}finally{if(pending.get(identity)===promise)pending.delete(identity);}
      }
    };
  }
  function mergeRuntime(project,packet){
    if(!project?.content_version||project.content_version!==packet.content_version)return null;
    const result={...project};
    for(const key of ['status','stage','progress','processing','error','logs','tts_runtime','subtitle_review','translation_preview','updated','process_log','asr_next'])if(Object.hasOwn(packet,key))result[key]=packet[key];
    return result;
  }
  function acceptsProject(current,incoming,revision=0){
    if((incoming.edit_revision||0)<revision)return false;
    if(current?.id===incoming.id){
      if((incoming.metadata_revision||0)<(current.metadata_revision||0))return false;
      if(current.updated&&incoming.updated&&incoming.updated<current.updated)return false;
    }
    return true;
  }
  const goal=p=>p?.goal||'dub';
  function initialTab(p){return !p?.segments?.length?'source':goal(p)==='transcribe'?'subtitles':p.outputs?.preview?'export':p.segments.some(s=>!s.zh?.trim())?'subtitles':goal(p)==='translate'?'subtitles':'voices';}
  function productLabel(p){
    const rows=p.segments,outputs=p.outputs||{};
    const code=Array.isArray(rows)?(outputs.video||outputs.preview?'video_ready':outputs.audio?'audio_ready':rows.length&&rows.every(r=>r.zh?.trim())?'translation_ready':rows.length?'source_ready':'needs_source'):(p.workflow?.code||'needs_source');
    return ({needs_source:'等待识别',source_ready:goal(p)==='transcribe'?'原文字幕可导出':'原文已识别',translation_ready:'译文字幕可导出',audio_ready:'配音已生成',video_ready:'成片可下载'})[code]||'等待识别';
  }
  function canResumeSegmentation(project){
    const job=project?.processing;
    return ['failed','cancelled','interrupted'].includes(project?.status)
      && ['analyze','resegment','retranscribe'].includes(job?.step)
      && ['segmentation_model','segmentation'].includes(job?.phase);
  }
  function nextAction(p,rows,tab,voicesReady){
    if(!rows.length)return {stage:'analyze',label:'识别原文字幕'};
    if(goal(p)==='transcribe')return tab==='subtitles'?{download:'source',label:'导出原文 SRT'}:{tab:'subtitles',label:'校对与导出字幕 →'};
    const missing=rows.filter(r=>!r.zh?.trim()).length;
    if(missing)return tab==='subtitles'?{stage:'translate',label:'翻译剩余 {0} 句',count:missing}:{tab:'subtitles',label:'准备译文 →'};
    if(goal(p)==='translate')return {download:'translation',label:'导出译文 SRT'};
    if(!voicesReady)return {tab:'voices',label:'准备角色音色 →'};
    if(!p.outputs?.audio)return tab==='voices'?{stage:'dub',label:'生成整片配音'}:{tab:'voices',label:'试听与生成配音 →'};
    if(tab!=='export')return {tab:'export',label:'混音与导出 →'};
    return p.outputs.video?{tab:'export',label:'查看与下载成片'}:{stage:'export',label:'生成成片'};
  }
  function preciseTime(seconds){
    const value=Math.round(Math.max(0,Number(seconds)||0)*100);
    return String(Math.floor(value/6000)).padStart(2,'0')+':'+String(Math.floor(value%6000/100)).padStart(2,'0')+'.'+String(value%100).padStart(2,'0');
  }
  function draftState(saved,project){
    if(!saved||saved.version!==1||saved.id!==project.id||!Array.isArray(saved.rows))return 'none';
    if(JSON.stringify(saved.rows)===JSON.stringify(project.segments))return 'saved';
    return saved.revision===(project.edit_revision||0)&&(!saved.content_version||!project.content_version||saved.content_version===project.content_version)?'recover':'conflict';
  }
  class SubtitleExportError extends Error{
    constructor(key,...args){super(key);this.key=key;this.args=args;}
  }
  function subtitleDownload(rows,kind,project={}){
    if(!['source','translation'].includes(kind))throw new TypeError('Invalid subtitle export kind');
    if(!rows.length)throw new SubtitleExportError('没有可导出的字幕');
    const field=kind==='source'?'text':'zh',blocks=[];let skipped=0,last=-1;
    const stamp=millis=>{
      const ms=millis%1000,secs=Math.floor(millis/1000)%60,minutes=Math.floor(millis/60000)%60,hours=Math.floor(millis/3600000);
      return [hours,minutes,secs].map(n=>String(n).padStart(2,'0')).join(':')+','+String(ms).padStart(3,'0');
    };
    for(const [index,row] of rows.entries()){
      const text=String(row[field]??'').replace(/\r\n?/g,'\n').trim().replace(/\n[ \t]*\n+/g,'\n');
      if(!text){
        if(kind==='source')throw new SubtitleExportError('第 {0} 句原文为空，请补齐后再导出。',row.id??index+1);
        skipped++;continue;
      }
      const start=Math.round(Number(row.start)*1000),end=Math.round(Number(row.end)*1000);
      if(!Number.isFinite(start)||!Number.isFinite(end)||start<0||end<=start||start<last||end>=3600000000||
        (Number.isFinite(project.media?.duration)&&Number(row.end)>project.media.duration+.1)){
        throw new SubtitleExportError('第 {0} 句时间无效，请校对后再导出。',row.id??index+1);
      }
      last=start;blocks.push(`${blocks.length+1}\n${stamp(start)} --> ${stamp(end)}\n${text}\n`);
    }
    if(!blocks.length)throw new SubtitleExportError('尚无译文可导出，请先翻译或填写译文。');
    let name=String(project.name||'BTranslateVideo').replace(/[<>:"/\\|?*\u0000-\u001f]/g,'_').replace(/[. ]+$/g,'').slice(0,100)||'BTranslateVideo';
    if(/^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(name))name='_'+name;
    const language=['zh','en','ja','es','ar'].includes(project.target_language)?project.target_language:'zh';
    return {text:blocks.join('\n'),filename:name+'.'+(kind==='source'?'original':language)+'.srt',count:blocks.length,skipped};
  }
  root.ProjectWorkflow={stages,context,current,coordinator,mergeRuntime,acceptsProject,subtitleDownload,goal,initialTab,productLabel,canResumeSegmentation,nextAction,preciseTime,draftState};
  if(typeof module!=='undefined')module.exports=root.ProjectWorkflow;
})(typeof window!=='undefined'?window:globalThis);
