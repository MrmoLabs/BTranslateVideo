/* Pure subtitle editing operations, shared with regression checks. */
(function(root){
  'use strict';
  const copy=value=>JSON.parse(JSON.stringify(value));
  function clearMark(row){delete row.review_status;delete row.review_signature;return row;}
  function clean(row){clearMark(row);for(const key of ['words','dub_timing'])delete row[key];return row;}
  function basis(row){return JSON.stringify([row.text.trim(),(row.zh||'').trim(),row.speaker,Math.round(row.start*1000),Math.round(row.end*1000)]);}
  function validTime(rows,index,start,end,duration){
    if(!Number.isFinite(start)||!Number.isFinite(end)||start<0||end<=start||end>duration+.001)throw Error('起止时间必须在原片范围内，且结束晚于开始。');
    if(index>0&&start<rows[index-1].start||index+1<rows.length&&start>rows[index+1].start)throw Error('开始时间不能越过相邻字幕的开始时间。');
  }
  function atCursor(text,position){
    if(!Number.isInteger(position)||position<=0||position>=text.length||!text.slice(0,position).trim()||!text.slice(position).trim())throw Error('请将光标放在文字中间，拆分后两段都需要有内容。');
    // DOM text offsets use UTF-16; a cursor must never cut an emoji in half.
    if(/[\uD800-\uDBFF]/.test(text[position-1])&&/[\uDC00-\uDFFF]/.test(text[position]))throw Error('拆分位置不能位于同一个字符内部。');
    return [text.slice(0,position).trim(),text.slice(position).trim()];
  }
  function split(rows,index,position,time,translationPosition){
    const row=rows[index];if(!row)throw Error('请先选择一句字幕。');
    if(rows.length>=30000)throw Error('字幕已达 30000 句，请先分割项目。');
    if(!Number.isFinite(time)||time-row.start<.02||row.end-time<.02)throw Error('拆分点须在本句内部，两段至少各保留 0.02 秒。');
    if(index+1<rows.length&&time>rows[index+1].start)throw Error('拆分点不能越过下一句的开始时间。');
    const text=atCursor(row.text,position),zh=row.zh?.trim()?atCursor(row.zh,translationPosition):['',''];
    const id=Math.max(...rows.map(r=>r.id))+1;
    const left=clean({...row,end:time,text:text[0],zh:zh[0]}),right=clean({...row,id,start:time,text:text[1],zh:zh[1]});
    return [...copy(rows.slice(0,index)),left,right,...copy(rows.slice(index+1))];
  }
  function merge(rows,index){
    const a=rows[index],b=rows[index+1];if(!a||!b)throw Error('本句后面没有可合并的字幕。');
    if(a.speaker!==b.speaker||(a.emotion||'inherit')!==(b.emotion||'inherit')||(a.emotion_strength||'medium')!==(b.emotion_strength||'medium'))throw Error('合并前请先统一两句的角色与情绪。');
    if(!!a.zh?.trim()!==!!b.zh?.trim())throw Error('两句译文完成情况不同，请先补齐译文或清空两句译文。');
    const row=clean({...a,end:Math.max(a.end,b.end),text:a.text+'\n'+b.text,zh:a.zh?.trim()?a.zh+'\n'+b.zh:''});
    if(row.text.length>6000||row.zh.length>6000)throw Error('合并后文字过长，请缩小合并范围。');
    return [...copy(rows.slice(0,index)),row,...copy(rows.slice(index+2))];
  }
  function timing(rows,index,start,end,duration){
    validTime(rows,index,start,end,duration);const result=copy(rows);if(start!==rows[index].start||end!==rows[index].end)result[index]=clean({...result[index],start,end});return result;
  }
  class History{
    constructor(){this.reset();}
    reset(){this.undoItems=[];this.redoItems=[];this.key=null;this.expected=null;}
    sync(key,rows){
      if(this.key!==key){this.reset();this.key=key;}
      if((this.undoItems.length||this.redoItems.length)&&this.expected!==JSON.stringify(rows)){this.reset();this.key=key;}
    }
    push(key,before,after,selected){
      this.sync(key,before);const a=JSON.stringify(before),b=JSON.stringify(after);this.redoItems=[];
      if(a.length+b.length<=4*1024*1024)this.undoItems.push({before:JSON.parse(a),after:JSON.parse(b),selected,size:a.length+b.length});
      else this.undoItems=[];
      while(this.undoItems.length>20||this.undoItems.reduce((sum,item)=>sum+item.size,0)>4*1024*1024)this.undoItems.shift();
      this.expected=b;
    }
    move(direction,rows){
      if(JSON.stringify(rows)!==this.expected){this.reset();return null;}
      const from=direction==='undo'?this.undoItems:this.redoItems,to=direction==='undo'?this.redoItems:this.undoItems,item=from.pop();if(!item)return null;
      to.push(item);const result=copy(direction==='undo'?item.before:item.after);this.expected=JSON.stringify(result);return {rows:result,selected:item.selected};
    }
  }
  root.SubtitleEditing={clearMark,basis,split,merge,timing,History};
  if(typeof module!=='undefined')module.exports=root.SubtitleEditing;
})(typeof window==='undefined'?globalThis:window);
