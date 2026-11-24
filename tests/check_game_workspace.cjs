const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const page = fs.readFileSync(path.join(root, 'app/static/index.html'), 'utf8');
const script = fs.readFileSync(path.join(root, 'app/static/game-voices-ui.js'), 'utf8');
const css = fs.readFileSync(path.join(root, 'app/static/game-voices-ui.css'), 'utf8');
const base = fs.readFileSync(path.join(root, 'app/static/game-voices.js'), 'utf8');

// The redesign layer is linked after the game voice data layer.
assert.ok(page.includes('/static/game-voices-ui.css'), 'redesign stylesheet linked');
assert.ok(page.includes('/static/game-voices-ui.js'), 'redesign script linked');
assert.ok(page.indexOf('game-voices-ui.css') > page.indexOf('game-voices.css'), 'redesign stylesheet loads after base styles');
assert.ok(page.indexOf('game-voices-ui.js') > page.indexOf('game-voices.js'), 'redesign script loads after base script');

// Data layer stays the single source for API calls and editor state.
for (const token of ['/rows', 'renderGameEditor', 'renderGameProject', 'loadGameCandidate', 'selectGameLine'])
  assert.ok(script.includes(token), 'overlay keeps using ' + token);
for (const token of ['function renderGameRows', 'function renderGameEditor', 'function loadGameRows', 'function saveGameLine'])
  assert.ok(base.includes(token), 'data layer still defines ' + token);

// Lines table: filters, result count, pagination and row actions.
for (const token of ['game-chip', 'game-page-input', 'game-page-size', 'game-line-text', 'game-line-source', 'game-line-meta', 'game-state', 'data-preview-line', 'game-filter-clear'])
  assert.ok(script.includes(token) || css.includes(token), 'missing table token ' + token);
// Inspector: previous/next, candidate list, collapsed source reference and pinned actions.
for (const token of ['game-line-prev', 'game-line-next', 'game-candidate-list', 'game-source-details', 'game-actions', 'game-candidate-item'])
  assert.ok(script.includes(token) || css.includes(token), 'missing inspector token ' + token);
assert.ok(css.includes('.game-project-open > .project-heading'), 'index heading hidden inside a project');
assert.ok(css.includes('grid-template-areas: "filters filters" "lines inspector"'), 'filter bar above the table');
assert.ok(css.includes('.game-inspector') && css.includes('position: sticky'), 'inspector stays in view');
assert.ok(css.includes('.game-table-scroll') && css.includes('max-height'), 'lines table scrolls internally');
console.log('Game audio workspace: redesign wiring, filters, pager and inspector OK');
