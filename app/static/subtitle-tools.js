'use strict';
window.SubtitleTools=(()=>{
  const core=SubtitleEditing,history=new core.History();
  const tool={id:null,version:null,index:null,generation:0,wave:null,abort:null,marking:false};
  const dialog=$('#subtitle-tools-dialog'),audio=$('#tools-wave-audio');
  const names={unreviewed:'未核对',checked:'已核对',question:'存疑'};
  const historyKey=()=>JSON.stringify([state.id,state.editorRevision]);
  const busy=()=>active(state.project)||state.saving||!!state.projectUnavailable||tool.marking;
  const current=()=>dialog.open&&state.id===tool.id&&state.selectionVersion===tool.version&&state.editorGeneration===tool.generation;
  function check(){if(busy())throw Error(tr('请等待当前操作结束后再编辑。'));if(!current())throw Error(tr('字幕草稿已变化，请关闭并重新打开时间轴工具。'));}
  function invalidateMark(row){core.clearMark(row);}
  function matches(row,filter){return !filter.startsWith('human-')||(row.review_status||'unreviewed')===filter.slice(6);}
  function render(){
    if(!state.project)return;
    history.sync(historyKey(),state.rows);
    $('#subtitle-edit-undo').disabled=busy()||!history.undoItems.length;
    $('#subtitle-edit-redo').disabled=busy()||!history.redoItems.length;
    $('#human-review-summary').textContent=tr('人工核对 {0}/{1} · 存疑 {2}',state.rows.filter(r=>r.review_status==='checked').length,state.rows.length,state.rows.filter(r=>r.review_status==='question').length);
    $('#mark-selected-review').disabled=busy()||!subtitleReview.selected.size;
    for(const element of $$('#segment-list tr')){
      const index=Number(element.querySelector('[data-review-field="text"]')?.dataset.index),row=state.rows[index];if(!row)continue;
      let badge=element.querySelector('.review-human-badge');
      if(!badge){badge=document.createElement('span');badge.className='badge review-human-badge';element.querySelector('.review-cue').append(badge);}
      badge.textContent=tr(names[row.review_status||'unreviewed']);badge.dataset.humanStatus=row.review_status||'unreviewed';
      const more=element.querySelector('.cue-more>div');
      if(!more.querySelector('[data-subtitle-tools]'))more.insertAdjacentHTML('afterbegin',`<button type="button" data-subtitle-tools="${index}">${esc(tr('时间轴与拆合句'))}</button><label class="human-mark-label">${esc(tr('人工标记'))}<select data-human-mark="${row.id}" aria-label="${esc(tr('第 {0} 句人工标记',row.id))}">${Object.entries(names).map(([value,name])=>`<option value="${value}">${esc(tr(name))}</option>`).join('')}</select></label>`);
      const select=more.querySelector('[data-human-mark]');select.value=row.review_status||'unreviewed';select.disabled=busy();more.querySelector('[data-subtitle-tools]').disabled=busy();
    }
    if(dialog.open){
      const locked=busy()||!current();
      for(const id of ['tools-apply-time','tools-split','tools-merge'])$('#'+id).disabled=locked;
      if(!current())$('#tools-status').textContent=tr('字幕草稿已变化，请关闭并重新打开时间轴工具。');
    }
  }
  function commit(rows,selected=state.selected){
    if(busy())throw Error(tr('请等待当前操作结束后再编辑。'));
    if(JSON.stringify(rows)===JSON.stringify(state.rows))return false;
    history.push(historyKey(),state.rows,rows,selected);state.rows=rows;state.selected=Math.min(selected,rows.length-1);
    subtitleReview.selected=new Set([...subtitleReview.selected].filter(id=>rows.some(r=>r.id===id)));saveReviewLocal();
    stopSourceCue();state.subtitleKey=null;state.boardKey=null;setDirty(true);renderSentenceList(true);renderEditors();return true;
  }
  function moveHistory(direction){
    if(busy())return;history.sync(historyKey(),state.rows);const value=history.move(direction,state.rows);if(!value)return;
    state.rows=value.rows;state.selected=Math.min(value.selected,value.rows.length-1);stopSourceCue();state.subtitleKey=null;state.boardKey=null;
    subtitleReview.selected=new Set([...subtitleReview.selected].filter(id=>value.rows.some(r=>r.id===id)));saveReviewLocal();
    setDirty(true);renderSentenceList(true);renderEditors();
  }
  async function mark(ids,status){
    if(busy())throw Error(tr('请等待当前操作结束后再编辑。'));
    const token={id:state.id,version:state.selectionVersion,generation:state.editorGeneration},rows=clone(state.rows);tool.marking=true;render();
    try{
      await Promise.all(rows.filter(r=>ids.includes(r.id)).map(async row=>{
        core.clearMark(row);if(status==='unreviewed')return;
        const digest=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(core.basis(row)));
        row.review_signature=Array.from(new Uint8Array(digest),b=>b.toString(16).padStart(2,'0')).join('');row.review_status=status;
      }));
      if(state.id!==token.id||state.selectionVersion!==token.version||state.editorGeneration!==token.generation)throw Error(tr('字幕草稿已变化，请重新标记。'));
      tool.marking=false;commit(rows);toast(tr('人工标记已加入草稿，保存后写入项目。'));
    }finally{tool.marking=false;render();}
  }
  function referenceId(row){return state.project.segments.some(r=>r.id===row.id)?row.id:state.project.segments[0]?.id;}
  function draw(){
    const canvas=$('#tools-waveform'),ctx=canvas.getContext('2d'),wave=tool.wave;if(!ctx)return;
    ctx.clearRect(0,0,canvas.width,canvas.height);if(!wave)return;
    const x=t=>(t-wave.start)/(wave.end-wave.start)*canvas.width,start=Number($('#tools-start').value),end=Number($('#tools-end').value);
    ctx.fillStyle='#e4f1ed';ctx.fillRect(x(start),0,x(end)-x(start),canvas.height);
    const max=Math.max(.05,...wave.peaks);ctx.strokeStyle='#21755f';ctx.lineWidth=1;
    ctx.beginPath();wave.peaks.forEach((p,i)=>{const px=i/wave.peaks.length*canvas.width,h=p/max*(canvas.height/2-8);ctx.moveTo(px,canvas.height/2-h);ctx.lineTo(px,canvas.height/2+h);});ctx.stroke();
    for(const [time,color] of [[start,'#12694e'],[end,'#12694e'],[Number($('#tools-split-time').value),'#b46d18']]){ctx.strokeStyle=color;ctx.beginPath();ctx.moveTo(x(time),0);ctx.lineTo(x(time),canvas.height);ctx.stroke();}
    if(audio.currentTime>0){ctx.strokeStyle='#171e25';ctx.beginPath();ctx.moveTo(x(wave.start+audio.currentTime),0);ctx.lineTo(x(wave.start+audio.currentTime),canvas.height);ctx.stroke();}
  }
  async function open(index){
    if(busy())throw Error(tr('请等待当前操作结束后再编辑。'));const row=state.rows[index];if(!row)return;
    stopSourceCue();audio.pause();pendingSeek=null;tool.abort?.abort();tool.abort=new AbortController();
    Object.assign(tool,{id:state.id,version:state.selectionVersion,index,generation:state.editorGeneration,wave:null});
    $('#tools-title').textContent=tr('第 {0} 句 · 时间轴与拆合句',row.id);
    $('#tools-source').value=row.text;$('#tools-translation').value=row.zh;$('#tools-translation-label').hidden=!row.zh.trim();
    for(const [id,value] of [['tools-start',row.start],['tools-end',row.end],['tools-split-time',Number(((row.start+row.end)/2).toFixed(3))]])$('#'+id).value=value;
    $('#tools-source').setSelectionRange(Math.floor(row.text.length/2),Math.floor(row.text.length/2));
    $('#tools-translation').setSelectionRange(Math.floor(row.zh.length/2),Math.floor(row.zh.length/2));
    $('#tools-status').textContent=tr('正在读取原声波形…');$('#tools-wave-range').textContent='';audio.removeAttribute('src');audio.load();
    if(!dialog.open)dialog.showModal();draw();render();
    const id=state.id,version=state.selectionVersion,generation=tool.generation,abort=tool.abort;
    const start=Math.max(0,row.start-2),end=Math.min(state.project.media.duration,row.end+2),ref=referenceId(row);
    if(!ref){$('#tools-status').textContent=tr('请先保存字幕，再读取原声波形。');return;}
    const query=new URLSearchParams({start,end});
    try{
      const wave=await api(`/api/projects/${id}/source-waveform/${ref}?${query}`,{signal:abort.signal});
      if(!dialog.open||id!==tool.id||version!==tool.version||generation!==tool.generation||abort!==tool.abort)return;
      tool.wave=wave;audio.src=`/api/projects/${id}/source-audio/${ref}?${query}`;
      const range=$('#tools-playhead');range.min=wave.start;range.max=wave.end;range.value=row.start;
      $('#tools-wave-range').textContent=tr('波形范围 {0}–{1}',clock(wave.start),clock(wave.end));
      $('#tools-status').textContent=tr('点击或拖动波形定位；也可直接输入时间。应用后先保留为草稿。');draw();
    }catch(error){if(error.name!=='AbortError'&&abort===tool.abort&&dialog.open)$('#tools-status').textContent=tr('波形读取失败，仍可用时间输入框编辑。')+' '+error.message;}
  }
  function apply(fn){return guard(()=>{check();let rows;try{rows=fn();}catch(error){throw Error(tr(error.message));}dialog.close();if(commit(rows,tool.index))toast(tr(history.undoItems.length?'时间轴修改已加入草稿，可撤销；保存后更新字幕与输出。':'草稿较大，本次操作未保留撤销记录，请确认后再保存。'));});}
  $('#tools-apply-time').onclick=()=>apply(()=>core.timing(state.rows,tool.index,Number($('#tools-start').value),Number($('#tools-end').value),state.project.media.duration));
  $('#tools-split').onclick=()=>apply(()=>core.split(state.rows,tool.index,$('#tools-source').selectionStart,Number($('#tools-split-time').value),$('#tools-translation').selectionStart));
  $('#tools-merge').onclick=()=>apply(()=>core.merge(state.rows,tool.index));
  $('#tools-close').onclick=()=>dialog.close();dialog.addEventListener('close',()=>{tool.abort?.abort();audio.pause();audio.removeAttribute('src');audio.load();});
  $('#subtitle-edit-undo').onclick=()=>guard(()=>moveHistory('undo'));$('#subtitle-edit-redo').onclick=()=>guard(()=>moveHistory('redo'));
  $('#mark-selected-review').onclick=()=>guard(()=>mark([...subtitleReview.selected],$('#selected-human-mark').value));
  $('#open-subtitle-tools').onclick=()=>guard(()=>open(state.selected));
  for(const id of ['tools-start','tools-end','tools-split-time'])$('#'+id).addEventListener('input',draw);
  audio.addEventListener('timeupdate',draw);
  let pendingSeek=null;
  function seek(value){
    if(!tool.wave)return;const time=Math.max(tool.wave.start,Math.min(tool.wave.end,value));$('#tools-playhead').value=time;
    const mode=$('#tools-wave-mode').value;if(mode!=='listen')$('#'+mode).value=Number(time.toFixed(3));
    if(audio.readyState)audio.currentTime=time-tool.wave.start;else pendingSeek=time-tool.wave.start;draw();
  }
  audio.addEventListener('loadedmetadata',()=>{if(pendingSeek!==null){audio.currentTime=pendingSeek;pendingSeek=null;}});
  $('#tools-playhead').oninput=event=>seek(Number(event.target.value));
  const canvas=$('#tools-waveform');let dragging=false;
  function pointer(event){if(!tool.wave)return;const rect=canvas.getBoundingClientRect();seek(tool.wave.start+Math.max(0,Math.min(1,(event.clientX-rect.left)/rect.width))*(tool.wave.end-tool.wave.start));}
  canvas.addEventListener('pointerdown',event=>{dragging=true;canvas.setPointerCapture(event.pointerId);pointer(event);});
  canvas.addEventListener('pointermove',event=>{if(dragging)pointer(event);});canvas.addEventListener('pointerup',()=>dragging=false);canvas.addEventListener('pointercancel',()=>dragging=false);
  document.addEventListener('click',event=>{const button=event.target.closest('[data-subtitle-tools]');if(button)guard(()=>open(Number(button.dataset.subtitleTools)));});
  document.addEventListener('change',event=>{const select=event.target.closest('[data-human-mark]');if(select)guard(()=>mark([Number(select.dataset.humanMark)],select.value));});
  function navigate(direction){
    const items=filteredRows();const focused=document.activeElement?.closest('[data-review-field]');
    const currentId=sourceCue.project===state.id?sourceCue.row?.id:focused?state.rows[Number(focused.dataset.index)]?.id:currentRow()?.id;
    const position=items.findIndex(({r})=>r.id===currentId),next=items[position+direction];if(!next)return false;
    chooseSentence(next.i);state.listPage=Math.floor(items.indexOf(next)/Number($('#subtitle-page-size').value));state.subtitleKey=null;renderSentenceList(true);
    playSourceCue(next.r);$(`#segment-list [data-source-audio="${next.r.id}"]`)?.closest('tr')?.scrollIntoView({block:'nearest',behavior:'instant'});return true;
  }
  document.addEventListener('keydown',event=>{
    if(event.isComposing||event.repeat||state.section!=='projects'||!state.projectDetail||state.tab!=='subtitles')return;
    if(document.querySelector('dialog[open]'))return;
    if(event.altKey&&!event.ctrlKey&&!event.metaKey&&['ArrowLeft','ArrowRight'].includes(event.key)){event.preventDefault();guard(()=>navigate(event.key==='ArrowRight'?1:-1));}
    if((event.ctrlKey||event.metaKey)&&event.key==='Enter'){
      event.preventDefault();const focused=document.activeElement?.closest('[data-review-field]'),row=focused?state.rows[Number(focused.dataset.index)]:state.rows.find(r=>r.id===sourceCue.row?.id)||currentRow();guard(()=>playSourceCue(row));
    }
  });
  return {render,open,navigate,matches,invalidateMark,referenceId};
})();
