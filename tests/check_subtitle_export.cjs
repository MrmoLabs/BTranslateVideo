const assert=require('node:assert/strict');
const {subtitleDownload}=require('../app/static/workflow-core.js');
const rows=[
  {id:3,start:.125,end:2.876,text:'Source one.\nSecond line.',zh:'尚未保存的新译文。'},
  {id:8,start:3,end:4,text:'Source two.',zh:''},
  {id:12,start:62.005,end:64.9996,text:'Source three.',zh:'最后一句。\r\n\r\n第二行。'},
];
const project={name:'A/B: lesson',target_language:'ja',media:{duration:70}};
const before=JSON.stringify(rows),metadata=JSON.stringify(project);
const original=subtitleDownload(rows,'source',project);
assert.equal(original.filename,'A_B_ lesson.original.srt');
assert.equal(original.count,3);assert.equal(original.skipped,0);
assert.match(original.text,/1\n00:00:00,125 --> 00:00:02,876\nSource one\.\nSecond line\./);
assert.match(original.text,/3\n00:01:02,005 --> 00:01:05,000\nSource three\./);
const translated=subtitleDownload(rows,'translation',project);
assert.equal(translated.filename,'A_B_ lesson.ja.srt');
assert.equal(translated.count,2);assert.equal(translated.skipped,1);
assert.match(translated.text,/2\n00:01:02,005 --> 00:01:05,000\n最后一句。\n第二行。/);
assert.ok(!translated.text.includes('Source two.'));
assert.ok(!translated.text.includes('\r')&&!translated.text.startsWith('\ufeff'));
assert.equal(JSON.stringify(rows),before);assert.equal(JSON.stringify(project),metadata);
assert.throws(()=>subtitleDownload(rows.map(row=>({...row,zh:''})),'translation'),/尚无译文/);
assert.throws(()=>subtitleDownload([{...rows[0],text:''}],'source'),/原文为空/);
for(const timing of [{start:NaN},{start:2,end:1},{start:0,end:.0001},{end:72}])assert.throws(()=>subtitleDownload([{...rows[0],...timing}],'source',project),/时间无效/);
assert.throws(()=>subtitleDownload([rows[2],rows[0]],'source',project),/时间无效/);
assert.equal(subtitleDownload([rows[0]],'source',{name:'CON'}).filename,'_CON.original.srt');
console.log('Subtitle exports: draft preservation, sparse translations, timestamps, filenames and UTF-8 OK');
