const assert=require('node:assert/strict');
const {measuredPercent,displayProgressPercent,elapsedSeconds,progressDuration}=require('../app/static/progress-ui.js');
assert.equal(measuredPercent({done:3,total:8}),37.5);
for(const p of [{},{done:0,total:0},{done:null,total:20},{done:NaN,total:2},{done:1,total:Infinity}])assert.equal(measuredPercent(p),null);
assert.equal(measuredPercent({done:12,total:10}),100);
assert.equal(elapsedSeconds({queued_at:0,started_at:null,finished_at:null},12),12);
assert.equal(elapsedSeconds({queued_at:0,started_at:4,finished_at:9},100),5);
assert.equal(elapsedSeconds({phase_started_at:5,finished_at:8},100,true),3);
assert.equal(progressDuration(3661),'1:01:01');
assert.equal(progressDuration(59.9),'0:59');
console.log('Measured percentages, indeterminate work and frozen elapsed times: OK');

assert.equal(displayProgressPercent({status:'done',done:null,total:null}),100);
assert.equal(displayProgressPercent({status:'cancelled',done:null,total:null}),null);
assert.equal(displayProgressPercent({status:'failed',done:2,total:8}),25);
