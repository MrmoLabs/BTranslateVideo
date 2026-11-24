const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const root=path.resolve(__dirname,'..');
const read=file=>fs.readFileSync(path.join(root,'app/static',file),'utf8');
const page=read('index.html');
const css=read('workbench-layout.css');
const workspace=read('workspace.js');
const review=read('subtitle-review.js');

// Both working tables keep a pager above and below the rows.
assert.equal((page.match(/data-subtitle-pager/g)||[]).length,2,'subtitle pagers');
assert.equal((page.match(/data-voice-pager/g)||[]).length,2,'voice pagers');
assert.equal((page.match(/id="voice-table-count"/g)||[]).length,1,'one dubbing counter');

// The compact dubbing controls exist and are wired to the render loop.
for(const id of ['voice-row-search','voice-speaker-filter','voice-page-size','voice-density','voice-check-page','bulk-speaker','bulk-emotion','bulk-strength','apply-voice-batch','voice-table-count'])
  assert.ok(page.includes(`id="${id}"`),`missing #${id}`);
for(const field of ['data-board-field="speaker"','data-board-field="emotion"','data-board-field="emotion_strength"','data-board-detail'])
  assert.ok(workspace.includes(field),`missing ${field}`);
assert.ok(workspace.includes("$('#voice-density').onchange"),'density toggle handler');
assert.ok(workspace.includes("classList.toggle('comfortable-voices'"),'comfortable voices class');
assert.ok(review.includes("classList.toggle('dubbing-workbench'"),'dubbing workbench class');

// The compact layout stylesheet is loaded and keeps the key density rules.
assert.ok(page.includes('/static/workbench-layout.css'),'stylesheet linked');
assert.ok(css.includes('body.dubbing-workbench .voice-setup'),'three-up setup strip');
assert.ok(css.includes('body.subtitle-workbench .review-table textarea'),'compact subtitle rows');
assert.ok(css.includes('top:var(--voice-pager-height'),'pinned pager offset');
assert.ok(css.includes('@container dubbing-list'),'stacked dubbing rows stay compact');
console.log('Compact workbench layout: pagers, controls and density rules OK');
