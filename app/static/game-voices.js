'use strict';

const gameVoice = {
  projects: [], project: null, row: null, page: 0, rows: [], total: 0, checked: new Set(), voices: [],
  dirty: false, saving: false, edition: 0, listRequest: 0, editorRequest: 0, polling: false, candidate: '',
  roleDirty: false,
};
const gamePageSize = 20;
const gameApi = '/api/game-voices';
const gameScenePreview = {rows: [], index: 0, request: 0};
let gamePackageRequest;
const gameNumbers = ['speed', 'gain', 'trim_start', 'trim_end', 'lead', 'tail', 'scene_order', 'target_duration'];
const gameFields = ['ja', 'en', 'text', 'role', 'scene', 'notes', 'voice', 'emotion', 'emotion_strength', 'background_backend',
  'drama3_direction', 'drama3_reference_text', ...gameNumbers];
// 'source' uses the original line itself as an emotion reference (IndexTTS / reference TTS).
const gameEmotions = {inherit: '沿用参考音', source: '跟随本句原声', happy: '开心', angry: '愤怒', sad: '悲伤',
  afraid: '害怕', disgusted: '厌恶', melancholic: '低落', surprised: '惊讶', calm: '平静'};
const gameCandidateEmotion = item => item.synthesis?.direction ||
  (gameEmotions[item.synthesis?.emotion] && (item.synthesis.method !== 'drama3' || item.synthesis.reference)
    ? tr(gameEmotions[item.synthesis.emotion]) : '');
const gameMethod = value => ({direct: '直接合成', conversion: '普通话表演 + Seed-VC', guide: '引导音 + Seed-VC',
  drama3: 'Drama 3', drama3_conversion: 'Drama 3 普通话 + Seed-VC'})[value];
const gameDramaRoute = route => route === 'drama3' || route === 'drama3_conversion';
const effectiveGameRoute = value => value === 'warm' ? (gameVoice.tools?.drama3_warm ? 'drama3' : 'project') : value;
const gameBusy = () => ['queued', 'running', 'cancelling'].includes(gameVoice.project?.job?.status);
const gameProjectPath = () => `${gameApi}/${gameVoice.project.id}`;
const gameLinePath = (id = gameVoice.row?.id) => `${gameProjectPath()}/rows/${id}`;
const gameForm = () => $('#game-line-form');
const gameProvenance = value => value === 'asr' ? tr('语音识别') : value === 'game' ? tr('游戏文本') : value === 'manual' ? tr('手工录入') : '';
const gameBatchVoiceKey = () => `game-batch-voice:${gameVoice.project?.id || ''}`;

function restoreGameBatchVoice() {
  if (!gameVoice.project || !gameVoice.voices) return;
  const select = $('#game-batch-voice');
  let saved = null;
  try { saved = localStorage.getItem(gameBatchVoiceKey()); } catch {}
  if (saved === 'none') { select.value = ''; return; }
  if (saved && gameVoice.voices.some(voice => voice.id === saved)) { select.value = saved; return; }
  if (!select.value && gameVoice.voices.length) select.value = gameVoice.voices[0].id;
}

function renderVcBatchHint() {
  const hint = $('#game-batch-vc-hint');
  if (!hint) return;
  const route = effectiveGameRoute($('#game-batch-route').value);
  const generating = $('#game-action').value === 'generate';
  if (generating && gameDramaRoute(route) && gameVoice.tools?.drama3_status) {
    hint.textContent = tr(gameVoice.tools.drama3_status);
    hint.hidden = false;
    return;
  }
  if (generating && route === 'drama3') {
    const usingSource = $('#game-batch-emotion').value === 'source';
    hint.hidden = !usingSource;
    if (usingSource) hint.textContent = tr('「跟随本句原声」会优先使用每句原声，可能沿用原声语言。');
    return;
  }
  const vc = generating && (route === 'conversion' || route === 'drama3_conversion' ||
    route === 'project' && Boolean(gameVoice.project?.vc));
  const batch = $('#game-batch-voice')?.value || '';
  const targets = new Set(Object.values(gameVoice.project?.role_voices || {}));
  const wrong = vc && batch && targets.has(batch);
  const message = route === 'drama3_conversion' && generating
    ? (!gameVoice.tools?.vc ? 'Seed-VC 未就绪，请先安装变声环境。' :
        wrong ? '请选择普通话参考音，角色绑定音色专用于变声' : '本批音色用普通话参考音，角色绑定音色用于变声。')
    : wrong ? '变声开启时请选中文音色；当前选的是角色原声，可能仍有日语腔' : '';
  hint.hidden = !message;
  if (message) hint.textContent = tr(message);
}

$('#menu-game-voices').onclick = () => guard(async () => {
  pauseMedia();
  showSection('game-voices');
  if (await refreshGameIndex() && gameVoice.project) await refreshGameProject(true);
});

function gameEndpointError(error) {
  if (error.status === 404 && error.message === 'Not Found') {
    return tr('当前服务尚未加载游戏音频接口。请重启 BTranslateVideo 后刷新页面。');
  }
  return error.message;
}

async function refreshGameIndex() {
  const button = $('#game-new');
  const notice = $('#game-index-error');
  button.disabled = true;
  let projects, voicePresets, tools;
  try {
    [projects, voicePresets, tools] = await Promise.all([api(gameApi), api('/api/voices'), api(`${gameApi}/tools`)]);
  } catch (error) {
    notice.textContent = gameEndpointError(error);
    notice.hidden = false;
    return false;
  }
  notice.hidden = true;
  button.disabled = false;
  gameVoice.projects = projects;
  gameVoice.voices = voicePresets;
  gameVoice.tools = tools;
  renderGameTools();
  $('#game-tools').open = !tools.decoder || !tools.encoder || !tools.template;
  renderGameIndex();
  return true;
}

function renderGameTools() {
  const tools = gameVoice.tools;
  if (!tools) return;
  for (const id of ['game-line-route', 'game-batch-route']) {
    const option = $('#' + id).querySelector('[value="warm"]');
    option.removeAttribute('data-i18n');
    option.textContent = tools.warm_model ? tr('当前保温模型 · {0}', tools.warm_model) : tr('跟随保温模型');
  }
  $('#game-tool-status').textContent = [
    tr('原声解码 {0}', tools.decoder ? tr('就绪') : tr('缺少工具')),
    tr('MOD 打包 {0}', tools.encoder && tools.template ? tr('就绪') : tr('缺少工具')),
    tr('Drama 3 {0}', tools.drama3 ? tr('就绪') : tr('未就绪')),
  ].join(' · ');
}

function renderGameIndex() {
  $('#game-projects').hidden = Boolean(gameVoice.project);
  $('#game-workspace').hidden = !gameVoice.project;
  $('#game-new').hidden = Boolean(gameVoice.project);
  $('#game-overview').textContent = gameVoice.project ? '' : tr('从尼尔 Steam 版导入原声，逐句配音并制作中文语音 MOD。');
  $('#game-projects').innerHTML = gameVoice.projects.map(project => `<article class="card project-card">
    <h2>${esc(project.title)}</h2><p class="subtle">${esc(tr('{0} 条语音 · {1} 条已通过实机验收', project.total, project.tested))}</p>
    <button type="button" class="primary" data-game-project="${project.id}">${esc(tr('打开项目'))}</button></article>`).join('') ||
    `<p class="subtle">${esc(tr('还没有游戏项目'))}</p>`;
}

function refreshGameLanguage() {
  renderGameTools();
  renderGameIndex();
  if (!gameVoice.project) return;
  renderGameProject();
  renderGameRows();
  if (!gameVoice.dirty) renderGameEditor();
  renderGameAudioStatus();
}

$('#game-new').onclick = () => {
  $('#game-create-error').textContent = '';
  $('#game-new-dialog').showModal();
};

$('#game-create').onsubmit = async event => {
  event.preventDefault();
  const button = event.submitter || event.currentTarget.querySelector('[type=submit]');
  button.disabled = true;
  $('#game-create-error').textContent = '';
  try {
    const project = await api(gameApi, json('POST', Object.fromEntries(new FormData(event.currentTarget))));
    $('#game-new-dialog').close();
    await openGameProject(project.id);
  } catch (error) {
    $('#game-create-error').textContent = gameEndpointError(error);
  } finally {
    button.disabled = false;
  }
};

$('#game-projects').onclick = event => {
  const button = event.target.closest('[data-game-project]');
  if (button) guard(() => openGameProject(button.dataset.gameProject));
};

function gameOptions(select, values, empty, selected = select.value) {
  const options = [new Option(empty, ''), ...values.map(([value, label]) => new Option(label, value))];
  select.replaceChildren(...options);
  select.value = options.some(option => option.value === selected) ? selected : '';
}

async function openGameProject(id) {
  stopGameScene();
  clearGamePlayer();
  gameVoice.project = await api(`${gameApi}/${id}`);
  gameVoice.page = 0;
  gameVoice.checked.clear();
  gameVoice.row = null;
  gameVoice.dirty = false;
  gameVoice.roleDirty = false;
  $('#game-role-voice-query').value = '';
  $('#game-role-voice-filter').value = '';
  $('#game-force-batch-settings').checked = false;
  $('#game-batch-route').value = $('#game-line-route').value = 'warm';
  gameVoice.pronunciationStamp = null;
  gameVoice.editorRequest++;
  $('#game-query').value = '';
  $('#game-status').value = 'replaceable';
  for (const id of ['game-group', 'game-scene', 'game-role']) $('#' + id).value = '';
  renderGameIndex();
  renderGameProject();
  renderGameEditor();
  await loadGameRows();
}

$('#game-back').onclick = () => guard(async () => {
  if (!await leaveGameEditor()) return;
  stopGameScene();
  stopGameGuide();
  gameVoice.project = gameVoice.row = null;
  gameVoice.listRequest++;
  gameVoice.editorRequest++;
  clearGamePlayer();
  await refreshGameIndex();
});

function renderGameProject() {
  const project = gameVoice.project;
  $('#game-title').textContent = project.title;
  $('#game-project-counts').textContent = tr('{0} 条语音 · {1} 条已采用中文 · {2} 条保留原版 · {3} 条实机通过',
    project.total, project.ready || 0, project.original || 0, project.tested);
  gameOptions($('#game-group'), project.groups.map(name => [name, name]), tr('全部语音包'));
  gameOptions($('#game-role'), project.roles.map(name => [name, name]), tr('全部角色'));
  gameOptions($('#game-scene'), project.scenes.map(name => [name, name]), tr('全部场景'));
  $('#game-scenes-list').replaceChildren(...project.scenes.map(name => new Option(name, name)));
  gameOptions($('#game-batch-voice'), gameVoice.voices.map(voice => [voice.id, voice.name]), tr('使用条目音色或 TTS 服务默认音色'));
  restoreGameBatchVoice();
  const batchEmotion = $('#game-batch-emotion');
  if (batchEmotion) {
    const previousEmotion = batchEmotion.value;
    batchEmotion.replaceChildren(...Object.entries(gameEmotions).map(([value, label]) => new Option(tr(label), value)));
    batchEmotion.value = previousEmotion in gameEmotions ? previousEmotion : 'inherit';
  }
  const pronunciation = JSON.stringify(project.pronunciation);
  if (gameVoice.pronunciationStamp !== pronunciation) {
    $('#game-pronunciation').elements.rules.value = project.pronunciation.map(rule => `${rule.term} = ${rule.spoken}`).join('\n');
    gameVoice.pronunciationStamp = pronunciation;
  }
  renderGameRoleVoices();
  const vcReady = Boolean(gameVoice.tools?.vc);
  if ($('#game-vc')) {
    $('#game-vc').checked = Boolean(project.vc);
    $('#game-vc').disabled = !vcReady;
  }
  if ($('#game-vc-hint')) {
    $('#game-vc-hint').hidden = vcReady && !project.vc;
    $('#game-vc-hint').textContent = vcReady
      ? tr('角色绑定的音色将作为变声目标。')
      : tr('Seed-VC 未就绪，请先安装变声环境。');
  }
  renderGameJob();
}

function renderGameRoleVoices() {
  if (gameVoice.roleDirty) { filterGameRoleVoices(); return; }
  const list = $('#game-role-voice-list');
  const roles = gameVoice.project.roles || [];
  const mapping = gameVoice.project.role_voices || {};
  const options = selected => [
    `<option value="">${esc(tr('未绑定'))}</option>`,
    selected && !gameVoice.voices.some(voice => voice.id === selected)
      ? `<option value="${esc(selected)}" selected disabled>${esc(tr('当前绑定音色不可用'))}</option>` : '',
    ...gameVoice.voices.map(voice => `<option value="${esc(voice.id)}"${voice.id === selected ? ' selected' : ''}>${esc(voice.name)}</option>`),
  ].join('');
  list.innerHTML = roles.map(role => {
    // Placeholder roles are not characters, so mixing their lines would blend speakers.
    const extract = role && role !== '-' && role !== '???' ? `<button type="button" class="game-extract-voice" data-extract-voice="${esc(role)}" title="${esc(tr('从该角色的游戏原声提取参考音并创建音色'))}">${esc(tr('从原声提取'))}</button>` : '';
    const audition = `<button type="button" class="game-audition-voice" data-audition-voice="${esc(role)}" title="${esc(tr('试听所选音色的参考音'))}">${esc(tr('试听'))}</button>`;
    return `<tr><th scope="row">${esc(role)}</th><td><select data-role-voice="${esc(role)}" aria-label="${esc(tr('为 {0} 选择音色', role))}">${options(mapping[role] || '')}</select></td><td><div class="button-group">${audition}${extract}</div></td></tr>`;
  }).join('');
  filterGameRoleVoices();
  renderGameControls();
}

function filterGameRoleVoices() {
  const query = $('#game-role-voice-query').value.trim().toLocaleLowerCase();
  const filter = $('#game-role-voice-filter').value;
  const selects = $$('#game-role-voice-list [data-role-voice]');
  let visible = 0, bound = 0;
  for (const select of selects) {
    const hasVoice = Boolean(select.value && !select.selectedOptions[0]?.disabled);
    if (hasVoice) bound++;
    const voice = select.value ? select.selectedOptions[0].text : '';
    const matches = (!filter || hasVoice === (filter === 'bound')) &&
      `${select.dataset.roleVoice} ${voice}`.toLocaleLowerCase().includes(query);
    select.closest('tr').hidden = !matches;
    select.closest('tr').querySelector('[data-audition-voice]').disabled = !hasVoice;
    if (matches) visible++;
  }
  $('#game-role-voice-query').disabled = !selects.length;
  $('#game-role-voice-filter').disabled = !selects.length;
  $('#game-role-voice-table').hidden = !visible;
  const empty = $('#game-role-voice-empty');
  empty.hidden = Boolean(visible);
  empty.textContent = tr(selects.length ? '没有匹配的角色' : '导入台词并分配角色后，可在这里为角色绑定音色。');
  $('#game-role-voice-count').textContent = selects.length ? tr('显示 {0}/{1} 个角色 · {2} 个已绑定', visible, selects.length, bound) : '';
}

function renderGameJob() {
  const project = gameVoice.project;
  const job = project.job;
  const busy = gameBusy();
  $('#game-job-phase').textContent = job.phase ? tr('{0} · {1}/{2}', tr(job.phase), job.done || 0, job.total || 0) :
    tr('替换语音档 {0}', tr(project.language === 'ja' ? '日语语音档' : '英语语音档'));
  if (job.kind === 'voice_from_selection' && job.status === 'done' && job.voice_result) {
    $('#game-job-phase').textContent = tr('已创建 {0}，可在角色音色中试听并绑定。', job.voice_result.name);
  }
  $('#game-job-error').textContent = job.error || '';
  const skipped = job.skipped_rows || [];
  $('#game-job-skipped').hidden = !skipped.length;
  if (skipped.length) {
    $('#game-job-skipped-text').textContent = tr('已跳过 {0} 条没有可朗读内容的台词', skipped.length);
  }
  $('#game-progress').hidden = !busy;
  if (job.done && job.total) $('#game-progress').value = 100 * job.done / job.total;
  else $('#game-progress').removeAttribute('value');
  $('#game-cancel').hidden = !busy;
  $('#game-retry').hidden = !['failed', 'interrupted', 'cancelled'].includes(job.status) || (job.done || 0) >= (job.total || 0);
  $('#game-packages').replaceChildren(...project.packages.map(pack => {
    const link = document.createElement('a');
    link.href = `${gameProjectPath()}/packages/${pack.id}`;
    link.textContent = tr(pack.release ? '下载正式 MOD（{0} 句）' : '下载测试 MOD（{0} 句）', pack.count);
    link.download = '';
    link.title = pack.created;
    return link;
  }));
  renderGameControls();
}

async function refreshGameProject(withRows = false) {
  if (!gameVoice.project) return;
  const id = gameVoice.project.id;
  const previous = gameVoice.project;
  const detail = await api(gameProjectPath());
  if (gameVoice.project?.id !== id) return;
  gameVoice.project = detail;
  const changed = detail.revision !== previous.revision || detail.job.status !== previous.job.status;
  if (changed && !gameBusy() && ['generate', 'alternate'].includes(previous.job?.kind)) {
    try { await refreshGameDramaTools(); } catch {}
  }
  if (changed && !gameBusy() && ['voice_from_source', 'voice_from_selection'].includes(previous.job?.kind)) {
    try { gameVoice.voices = await api('/api/voices'); } catch {}
  }
  if (changed || withRows) renderGameProject();
  else renderGameJob();
  if (withRows || changed && !gameBusy()) {
    await loadGameRows();
    if (gameVoice.row && !gameVoice.dirty && !gameVoice.saving) await selectGameLine(gameVoice.row.id, true);
  }
}

async function loadGameRows() {
  const request = ++gameVoice.listRequest;
  const values = new URLSearchParams({q: $('#game-query').value, group: $('#game-group').value,
    role: $('#game-role').value, scene: $('#game-scene').value, status: $('#game-status').value,
    offset: String(gameVoice.page * gamePageSize), limit: String(gamePageSize)});
  const data = await api(`${gameProjectPath()}/rows?${values}`);
  if (request !== gameVoice.listRequest) return;
  gameVoice.rows = data.rows;
  gameVoice.total = data.total;
  if (!data.rows.length && gameVoice.page > 0) {
    gameVoice.page = Math.max(0, Math.ceil(data.total / gamePageSize) - 1);
    await loadGameRows();
    return;
  }
  renderGameRows();
}

function renderGameRows() {
  $('#game-rows').innerHTML = gameVoice.rows.map(row => {
    const label = row.text || row.ja || row.en || tr('待识别');
    const status = row.tested ? '实机通过' : row.selected ? '已采用' : row.generated ? '待确认' : row.replaceable ? '可替换' : '仅试听';
    const source = [row.role, row.scene, row.group, row.id].filter(Boolean).join(' · ');
    const time = row.duration == null ? '' : `<small>${esc(tr('{0} 秒', row.duration.toFixed(2)))}</small>`;
    return `<tr><td><input type="checkbox" data-check-line="${row.id}" aria-label="${esc(tr('选择语音 {0}', row.id))}" ${gameVoice.checked.has(row.id) ? 'checked' : ''}></td>
      <td><button type="button" class="game-open-row" data-open-line="${row.id}" aria-current="${row.id === gameVoice.row?.id}">
      <strong class="game-row-source">${esc(label)}</strong><small>${esc(source)}</small></button></td>
      <td><span class="badge">${esc(tr(status))}</span>${time}</td></tr>`;
  }).join('') || `<tr><td colspan="3" class="subtle">${esc(tr('没有匹配的台词'))}</td></tr>`;
  $('#game-page').textContent = `${gameVoice.page + 1} / ${Math.max(1, Math.ceil(gameVoice.total / gamePageSize))}`;
  $('#game-prev').disabled = gameVoice.page === 0;
  $('#game-next').disabled = (gameVoice.page + 1) * gamePageSize >= gameVoice.total;
  renderGameControls();
}

function renderGameControls() {
  const selected = gameVoice.rows.filter(row => gameVoice.checked.has(row.id)).length;
  const busy = gameBusy() || gameVoice.saving;
  $('#game-selection').textContent = tr('已选 {0} 条', gameVoice.checked.size);
  $('#game-check-page').checked = Boolean(selected) && selected === gameVoice.rows.length;
  $('#game-check-page').indeterminate = selected > 0 && selected < gameVoice.rows.length;
  $('#game-run').disabled = busy || !gameVoice.checked.size;
  const action = $('#game-action').value;
  $('#game-run').textContent = tr(action === 'generate' ? '批量生成' :
    action === 'voice_from_selection' ? '提取参考音' : '开始处理');
  $('#game-save-pronunciation').disabled = busy;
  $('#game-save').disabled = busy || !gameVoice.dirty;
  $('#game-generate-one').disabled = busy || !gameVoice.row;
  $('#game-generate-one').textContent = tr(gameVoice.row && Object.keys(gameVoice.row.candidates).length ? '生成备选' : '生成配音');
  const fields = gameForm().elements;
  const lineRoute = effectiveGameRoute($('#game-line-route').value);
  const row = gameVoice.row;
  const conversion = ['conversion', 'drama3_conversion'].includes(lineRoute) ||
    lineRoute === 'project' && gameVoice.project?.vc;
  const singleVoiceLabel = $('#game-line-voice-label');
  const voiceLabelText = conversion ? '普通话表演音色' : '音色';
  singleVoiceLabel.dataset.i18n = voiceLabelText;
  singleVoiceLabel.textContent = tr(voiceLabelText);
  const drama = gameDramaRoute(lineRoute);
  $('#game-drama3-fields').hidden = !drama;
  if (drama) {
    const target = gameVoice.project?.role_voices?.[row?.role];
    let message = fields.emotion.value === 'source' ? 'Drama 3 直接参考本句原声，可能沿用原声语言。' : '表演指示优先于情绪预设。';
    if (lineRoute === 'drama3_conversion') {
      if (!gameVoice.tools?.vc) message = 'Seed-VC 未就绪，请先安装变声环境。';
      else if (row?.role && !gameVoice.voices.some(voice => voice.id === target)) message = '角色变声目标不可用，请在「角色音色」重新绑定。';
      else message = '单句音色选择普通话参考音，角色绑定音色用于变声。';
    }
    $('#game-drama3-status').textContent = tr(gameVoice.tools?.drama3_status || message);
  }
  $('#game-polish-one').disabled = busy || !gameVoice.row || !fields.text.value.trim() ||
    !(fields.ja.value.trim() || fields.en.value.trim());
  $('#game-dialogue-suggestion').hidden = gameVoice.dirty || !gameVoice.row?.suggested_text;
  $('#game-use-suggestion').disabled = busy;
  const keepOriginal = row?.decision === 'original';
  $('#game-adopt').hidden = !gameVoice.candidate || !keepOriginal && gameVoice.candidate === row?.selected;
  $('#game-adopt').disabled = busy || gameVoice.dirty;
  $('#game-tested').disabled = busy || gameVoice.dirty || keepOriginal || !row?.selected || !row?.replaceable;
  $('#game-tested').textContent = tr(gameVoice.row?.tested ? '撤销实机通过' : '标记实机通过');
  $('#game-import').disabled = busy;
  $('#game-save-role-voices').disabled = busy || !gameVoice.roleDirty;
  $('#game-auto-role-voices').disabled = busy || !(gameVoice.project?.roles || []).length;
  $('#game-draft').hidden = !gameVoice.dirty;
  $('#game-spoken').hidden = gameVoice.dirty || gameVoice.row?.spoken_text === gameVoice.row?.text;
  const generating = $('#game-action').value === 'generate';
  $('#game-batch-route-wrap').hidden = !generating;
  $('#game-keep-original').disabled = busy || !row;
  $('#game-defer-line').hidden = !row || row.decision === 'pending';
  $('#game-defer-line').disabled = busy;
  $('#game-keep-original').textContent = tr(keepOriginal ? '取消保留原版' : '保留原版');
  $('#game-line-decision').textContent = tr(keepOriginal ? '已保留原版' : row?.selected ? '已采用中文' : '待决定');
  $('#game-line-decision').dataset.state = keepOriginal ? 'original' : row?.selected ? 'ready' : 'pending';
  $('#game-guide-upload').disabled = busy || !row;
  $('#game-open-guide-record').disabled = busy || !row;
  $('#game-guide-play').disabled = !row?.guide;
  $('#game-guide-remove').hidden = !row?.guide;
  $('#game-guide-remove').disabled = busy;
  const candidate = row?.candidates?.[gameVoice.candidate];
  $('#game-play-dry').hidden = !candidate?.dry_file || !candidate?.package_file;
  $('#game-play-mix').hidden = !candidate?.package_file;
  $('#game-play-background').hidden = !candidate?.background_file;
  $('#game-play-performance').hidden = !candidate?.performance_file;
  $('#game-track-options').hidden = !candidate?.package_file && !candidate?.performance_file && !candidate?.background_file;
  for (const button of $$('#game-candidate-list [data-delete-candidate]')) button.disabled = busy;
  const background = fields.background_backend;
  background.closest('label').hidden = !fields.preserve_background.checked || ![1, 2].includes(row?.channels);
  background.querySelector('[value="bandit"]').disabled = !gameVoice.tools?.bandit;
  renderGameSceneControls();
  $('#game-batch-voice-wrap').hidden = !generating;
  $('#game-batch-emotion-wrap').hidden = !generating;
  $('#game-force-batch-wrap').hidden = !generating;
  $('#game-force-batch-settings').disabled = busy;
  const voiceLabel = $('#game-batch-voice-label');
  const label = $('#game-force-batch-settings').checked ? '本批指定音色' : '本批默认音色';
  voiceLabel.dataset.i18n = label;
  voiceLabel.textContent = tr(label);
  renderVcBatchHint();
}

let gameFilterTimer;
function updateGameFilters() {
  gameVoice.page = 0;
  clearTimeout(gameFilterTimer);
  gameFilterTimer = setTimeout(() => guard(() => gameVoice.project && loadGameRows()), 160);
}
$('#game-query').oninput = updateGameFilters;
for (const id of ['game-group', 'game-scene', 'game-role', 'game-status']) $('#' + id).onchange = updateGameFilters;
$('#game-prev').onclick = () => guard(async () => { gameVoice.page--; await loadGameRows(); });
$('#game-next').onclick = () => guard(async () => { gameVoice.page++; await loadGameRows(); });
$('#game-clear').onclick = () => { gameVoice.checked.clear(); renderGameRows(); };
$('#game-check-page').onchange = event => {
  for (const row of gameVoice.rows) {
    if (event.target.checked) gameVoice.checked.add(row.id);
    else gameVoice.checked.delete(row.id);
  }
  renderGameRows();
};
$('#game-rows').onchange = event => {
  const id = event.target.dataset.checkLine;
  if (!id) return;
  if (event.target.checked) gameVoice.checked.add(id);
  else gameVoice.checked.delete(id);
  renderGameControls();
};
$('#game-rows').onclick = event => {
  const button = event.target.closest('[data-open-line]');
  if (button) guard(() => selectGameLine(button.dataset.openLine));
};

async function leaveGameEditor() {
  if (gameVoice.saving) return false;
  if (!gameVoice.dirty) return true;
  const choice = await confirmChoice(tr('当前台词有未保存的修改。'), {save: true, title: tr('保留当前修改')});
  if (choice === 'cancel') return false;
  if (choice === 'save') return saveGameLine();
  gameVoice.dirty = false;
  return true;
}

async function selectGameLine(id, refresh = false) {
  if (!refresh && !await leaveGameEditor()) return;
  const request = ++gameVoice.editorRequest;
  const row = await api(gameLinePath(id));
  if (request !== gameVoice.editorRequest || refresh && gameVoice.dirty) return;
  const same = gameVoice.row?.id === row.id;
  const previousLatest = Object.keys(gameVoice.row?.candidates || {}).at(-1);
  const latest = Object.keys(row.candidates).at(-1);
  gameVoice.row = row;
  if (!same) {
    clearGamePlayer();
    stopGameGuide();
    gameVoice.candidate = '';
  } else if (latest && latest !== previousLatest) {
    gameVoice.candidate = latest;
    if (gameVoice.playback?.kind === 'candidate') clearGamePlayer();
  }
  renderGameEditor();
  renderGameRows();
}

function renderGameEditor() {
  const row = gameVoice.row;
  $('#game-no-row').hidden = Boolean(row);
  $('#game-inspector-content').hidden = !row;
  if (!row) return;
  const path = row.assets[gameVoice.project.language]?.path;
  const layout = row.channels === 6 ? '5.1' : row.channels === 2 ? '2.0' : row.channels ? `${row.channels}ch` : '';
  $('#game-asset').textContent = [path, row.id, layout, ...row.events].filter(Boolean).join(' · ');
  $('#game-line-context').textContent = [row.role, row.scene, `#${row.id}`].filter(Boolean).join(' · ');
  $('#game-ja-play').hidden = !row.assets.ja;
  $('#game-en-play').hidden = !row.assets.en;
  for (const language of ['ja', 'en']) {
    $(`#game-${language}-source`).textContent = gameProvenance(row[`${language}_source`]);
  }
  $('#game-text-source').textContent = gameProvenance(row.text_source);
  const preserve = gameForm().elements.preserve_background;
  preserve.disabled = ![1, 2, 6].includes(row.channels);
  preserve.checked = preserve.disabled ? false : Boolean(row.preserve_background);
  gameOptions(gameForm().elements.voice, gameVoice.voices.map(voice => [voice.id, voice.name]), tr('跟随角色或本批音色'), row.voice);
  gameForm().elements.emotion.replaceChildren(...Object.entries(gameEmotions).map(([value, label]) => new Option(tr(label), value)));
  for (const field of gameFields) gameForm().elements[field].value = row[field] ?? (gameNumbers.includes(field) ? 0 : '');
  $('#game-guide-info').textContent = row.guide ? `${row.guide.name} · ${tr('{0} 秒', row.guide.duration.toFixed(2))}` :
    tr('录制或导入普通话台词，变声时可使用这段表演。');
  $('#game-spoken').textContent = tr('送入语音模型 · {0}', row.spoken_text);
  $('#game-suggested-text').textContent = row.suggested_text || '';
  if (!Object.hasOwn(row.candidates, gameVoice.candidate)) {
    gameVoice.candidate = row.selected || Object.keys(row.candidates).at(-1) || '';
  }
  renderGameControls();
}

gameForm().oninput = event => {
  if (!gameFields.includes(event.target.name) && event.target.name !== 'preserve_background') return;
  gameVoice.dirty = true;
  gameVoice.edition++;
  renderGameControls();
};
gameForm().onsubmit = event => { event.preventDefault(); guard(saveGameLine); };
async function saveGameLine() {
  if (!gameVoice.dirty) return true;
  if (gameVoice.saving || !gameForm().reportValidity()) return false;
  const edition = gameVoice.edition;
  const values = Object.fromEntries(new FormData(gameForm()));
  for (const field of gameNumbers) values[field] = Number(values[field]);
  const preserve = gameForm().elements.preserve_background;
  values.preserve_background = !preserve.disabled && preserve.checked;
  values.revision = gameVoice.row.revision;
  gameVoice.saving = true;
  renderGameControls();
  try {
    gameVoice.row = await api(gameLinePath(), json('PUT', values));
    gameVoice.dirty = edition !== gameVoice.edition;
    gameVoice.project.revision = gameVoice.row.revision;
    if (!gameVoice.dirty) renderGameEditor();
    await refreshGameProject(true);
    return !gameVoice.dirty;
  } finally {
    gameVoice.saving = false;
    renderGameControls();
  }
}

function clearGamePlayer() {
  gameVoice.playback = null;
  const audio = $('#game-audition-audio');
  audio.pause();
  audio.removeAttribute('src');
  audio.load();
  $('#game-audition-player').hidden = true;
  $('#game-audition-wave').hidden = true;
}

function renderGameAudioStatus() {
  const playback = gameVoice.playback;
  if (!playback) return;
  const {meta, describe, title} = playback;
  $('#game-audition-title-current').textContent = tr(title);
  if (meta) $('#game-audition-meta').textContent = describe ? describe(meta) : tr('原声 {0} 秒', meta.duration.toFixed(2));
}

async function gameWave(kind, path, description, autoplay = false, title = path.endsWith('/ja') ? '日语原声' : '英语原声') {
  const audio = $('#game-audition-audio');
  const status = $('#game-audition-meta');
  const canvas = $('#game-audition-wave');
  const playback = {kind, path, title, describe: description};
  gameVoice.playback = playback;
  audio.pause();
  audio.src = path;
  $('#game-audition-player').hidden = false;
  canvas.hidden = true;
  status.textContent = tr('正在读取音频…');
  renderGameAudioStatus();
  if (autoplay) audio.play().catch(() => {});
  let meta;
  try {
    const [endpoint, query] = path.split('?');
    meta = await api(`${endpoint}/waveform${query ? `?${query}` : ''}`);
  } catch (error) {
    if (gameVoice.playback === playback) status.textContent = error.message;
    return;
  }
  if (gameVoice.playback !== playback) return;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = getComputedStyle(canvas).getPropertyValue('--accent');
  const scale = canvas.width / meta.peaks.length;
  meta.peaks.forEach((peak, index) => {
    const height = Math.max(1, peak * canvas.height);
    ctx.fillRect(index * scale, (canvas.height - height) / 2, Math.max(1, scale - .3), height);
  });
  canvas.hidden = false;
  playback.meta = meta;
  updateGamePlayhead();
  renderGameAudioStatus();
}

const gameLineAudio = $('#game-audition-audio');
const gameLineWave = $('#game-audition-wave');
const gameLineDuration = () => Number.isFinite(gameLineAudio.duration) && gameLineAudio.duration > 0
  ? gameLineAudio.duration : gameVoice.playback?.meta?.duration || 0;

function updateGamePlayhead() {
  const duration = gameLineDuration();
  const time = Math.min(gameLineAudio.currentTime, duration);
  gameLineWave.style.setProperty('--game-wave-progress', `${duration ? 100 * time / duration : 0}%`);
  gameLineWave.setAttribute('aria-valuemax', duration.toFixed(2));
  gameLineWave.setAttribute('aria-valuenow', time.toFixed(2));
  gameLineWave.setAttribute('aria-valuetext', tr('{0} 秒', time.toFixed(2)));
}

function seekGameAudition(time) {
  const duration = gameLineDuration();
  if (!duration) return false;
  gameLineAudio.currentTime = Math.max(0, Math.min(duration, time));
  updateGamePlayhead();
  return true;
}

for (const event of ['timeupdate', 'loadedmetadata', 'seeked']) gameLineAudio.addEventListener(event, updateGamePlayhead);
gameLineWave.onclick = event => {
  const bounds = gameLineWave.getBoundingClientRect();
  if (!bounds.width) return;
  const duration = gameLineDuration();
  const position = duration * (event.clientX - bounds.left) / bounds.width;
  if (seekGameAudition(Math.min(position, duration - .01))) {
    gameLineAudio.play().catch(error => { if (error.name !== 'AbortError') toast(error.message); });
  }
};
gameLineWave.onkeydown = event => {
  const duration = gameLineDuration();
  const step = Math.max(.1, duration * .05);
  const movements = {Home: 0, End: duration, ArrowLeft: gameLineAudio.currentTime - step,
    ArrowDown: gameLineAudio.currentTime - step, ArrowRight: gameLineAudio.currentTime + step,
    ArrowUp: gameLineAudio.currentTime + step};
  if (!Object.hasOwn(movements, event.key)) return;
  event.preventDefault();
  seekGameAudition(movements[event.key]);
};
$('#game-voices-page').addEventListener('play', event => {
  gameAuditionAudio?.pause();
  for (const audio of $$('#game-voices-page audio')) if (audio !== event.target) audio.pause();
}, true);

for (const language of ['ja', 'en']) {
  $(`#game-${language}-play`).onclick = () => guard(() => gameWave('source', `${gameLinePath()}/source/${language}`, null, true));
}

async function loadGameCandidate(track = '', autoplay = false) {
  const value = gameVoice.row?.candidates[gameVoice.candidate];
  if (!value) return;
  const url = `${gameLinePath()}/candidates/${gameVoice.candidate}${track ? `?track=${track}` : ''}`;
  await gameWave('candidate', url, meta => {
    const timing = value.delta > .15 ? tr('超出原声 {0} 秒', value.delta.toFixed(2)) :
      tr('与原声相差 {0} 秒', value.delta.toFixed(2));
    const duration = value.voice_duration ?? meta.duration;
    const overrun = value.window_delta > .15 ? tr('超过目标时长 {0} 秒', value.window_delta.toFixed(2)) : '';
    return [value.voice, gameCandidateEmotion(value), tr('中文 {0} 秒', duration.toFixed(2)), value.package_file ? tr('含原声背景') : '', overrun,
      timing, meta.peak >= .999 ? tr('峰值可能削波') : ''].filter(Boolean).join(' · ');
  }, autoplay, {dry: '中文干声', mix: '回混成品', background: '分离背景', performance: '变声前表演'}[track] || '中文候选版本');
}

for (const track of ['dry', 'mix', 'background', 'performance']) {
  $(`#game-play-${track}`).onclick = () => guard(() => loadGameCandidate(track, true));
}

async function setGameOriginal(rows, original, protectAdopted = false) {
  if (!await saveGameLine()) return;
  const result = await api(`${gameProjectPath()}/original-selection`, json('PUT', {
    rows, original, protect_adopted: protectAdopted, revision: gameVoice.project.revision,
  }));
  gameVoice.project = result.project;
  if (gameVoice.row) await selectGameLine(gameVoice.row.id, true);
  renderGameProject();
  await loadGameRows();
  toast(tr('已更新 {0} 条配音决定', result.changed));
}

$('#game-keep-original').onclick = () => guard(() => setGameOriginal(
  [gameVoice.row.id], gameVoice.row.decision !== 'original'));
$('#game-defer-line').onclick = () => guard(async () => {
  if (!await saveGameLine()) return;
  gameVoice.row = await api(`${gameLinePath()}/defer`, json('POST', {revision: gameVoice.project.revision}));
  renderGameEditor();
  await refreshGameProject(true);
});

function stopGameGuide() {
  const audio = $('#game-guide-audio');
  audio.pause();
  audio.removeAttribute('src');
  audio.load();
  audio.hidden = true;
}

$('#game-guide-upload').onclick = () => $('#game-guide-file').click();
$('#game-guide-file').onchange = event => {
  const file = event.currentTarget.files[0];
  event.currentTarget.value = '';
  if (file) guard(() => uploadGameGuide(file));
};

async function uploadGameGuide(file) {
  const row = gameVoice.row?.id;
  const projectId = gameVoice.project?.id;
  if (!row || gameBusy() || gameVoice.saving || !await saveGameLine() ||
      gameVoice.project?.id !== projectId || gameVoice.row?.id !== row) return false;
  const form = new FormData();
  form.set('audio', file);
  form.set('revision', String(gameVoice.project.revision));
  gameVoice.saving = true;
  renderGameControls();
  try {
    const updated = await api(`${gameLinePath(row)}/guide`, {method: 'PUT', body: form});
    if (gameVoice.project?.id !== projectId || gameVoice.row?.id !== row) return false;
    gameVoice.row = updated;
    gameVoice.project.revision = updated.revision;
    stopGameGuide();
    $('#game-line-route').value = 'conversion';
    if (!gameVoice.dirty) renderGameEditor();
    await refreshGameProject(true);
    return true;
  } finally {
    gameVoice.saving = false;
    renderGameControls();
  }
}

const gameRecordDialog = $('#game-guide-record-dialog');
let gameRecording;

function renderGameRecording(recording, message) {
  const active = ['starting', 'recording', 'stopping'].includes(recording.phase);
  $('#game-record-status').textContent = message;
  $('#game-record-start').disabled = !recording.devices.length || active || recording.saving;
  $('#game-record-stop').hidden = !active;
  $('#game-record-stop').disabled = !recording.token || recording.phase === 'stopping';
  $('#game-record-device').disabled = active || recording.saving;
  $('#game-record-apply').disabled = !recording.file || recording.saving;
}

function cancelGameRecordingToken(recording) {
  if (recording.token) fetch(`/api/recording/${recording.token}/cancel`, {method: 'POST', keepalive: true}).catch(() => {});
  recording.token = '';
}

function clearGameRecordingPreview(recording) {
  const audio = $('#game-record-audio');
  audio.pause();
  audio.removeAttribute('src');
  audio.load();
  audio.hidden = true;
  if (recording.url) URL.revokeObjectURL(recording.url);
  recording.url = '';
  recording.file = null;
}

$('#game-open-guide-record').onclick = () => guard(async () => {
  const recording = {devices: [], phase: 'idle', row: gameVoice.row.id, project: gameVoice.project.id};
  gameRecording = recording;
  for (const audio of $$('#game-voices-page audio')) audio.pause();
  $('#game-record-script').textContent = gameForm().elements.text.value;
  $('#game-record-time').textContent = '';
  $('#game-record-device').replaceChildren();
  renderGameRecording(recording, tr('正在读取录音设备…'));
  gameRecordDialog.showModal();
  try {
    const devices = await api('/api/recording/devices');
    if (gameRecording !== recording) return;
    recording.devices = devices.microphone || [];
    const select = $('#game-record-device');
    select.replaceChildren(...recording.devices.map(device => new Option(device.name, device.id)));
    select.value = String((recording.devices.find(device => device.default) || recording.devices[0])?.id ?? '');
    renderGameRecording(recording, recording.devices.length ? tr('录音支持 0.2 至 29 秒。') : tr(devices.error || '没有可用的录音设备'));
  } catch (error) {
    if (gameRecording === recording) renderGameRecording(recording, error.message);
  }
});

async function pollGameRecording(recording) {
  if (gameRecording !== recording || !recording.token) return;
  const token = recording.token;
  try {
    const result = await api(`/api/recording/${token}`);
    if (gameRecording !== recording) return;
    recording.phase = result.status;
    $('#game-record-time').textContent = tr('{0} 秒', Number(result.duration).toFixed(1));
    if (result.status === 'ready') {
      const response = await fetch(`/api/recording/${token}/audio`);
      if (!response.ok) throw Error(tr('录音文件读取失败，请重新录制'));
      const blob = await response.blob();
      if (gameRecording !== recording) return;
      recording.file = new File([blob], 'guide-recording.wav', {type: 'audio/wav'});
      recording.url = URL.createObjectURL(blob);
      $('#game-record-audio').src = recording.url;
      $('#game-record-audio').hidden = false;
      cancelGameRecordingToken(recording);
      renderGameRecording(recording, tr('录音已就绪，可以试听后使用。'));
    } else if (['failed', 'cancelled'].includes(result.status)) {
      cancelGameRecordingToken(recording);
      renderGameRecording(recording, tr(result.error || '录音已取消'));
    } else {
      const text = {starting: '正在启动录音…', recording: '正在录音，停止后可试听', stopping: '正在整理录音…'};
      renderGameRecording(recording, tr(text[result.status] || '正在录音，停止后可试听'));
      recording.timer = setTimeout(() => pollGameRecording(recording), 250);
    }
  } catch (error) {
    if (gameRecording !== recording) return;
    cancelGameRecordingToken(recording);
    recording.phase = 'failed';
    renderGameRecording(recording, error.message);
  }
}

$('#game-record-start').onclick = () => guard(async () => {
  const recording = gameRecording;
  clearGameRecordingPreview(recording);
  recording.phase = 'starting';
  $('#game-record-time').textContent = '';
  renderGameRecording(recording, tr('正在启动录音…'));
  try {
    const result = await api('/api/recording/start', json('POST', {
      kind: 'microphone', guide: true, device: Number($('#game-record-device').value),
    }));
    recording.token = result.id;
    if (gameRecording !== recording) { cancelGameRecordingToken(recording); return; }
    await pollGameRecording(recording);
  } catch (error) {
    if (gameRecording !== recording) return;
    recording.phase = 'failed';
    renderGameRecording(recording, error.message);
  }
});

$('#game-record-stop').onclick = () => guard(async () => {
  const recording = gameRecording;
  if (!recording?.token) return;
  recording.phase = 'stopping';
  renderGameRecording(recording, tr('正在整理录音…'));
  await api(`/api/recording/${recording.token}/stop`, {method: 'POST'});
});

$('#game-record-apply').onclick = () => guard(async () => {
  const recording = gameRecording;
  if (!recording?.file || gameVoice.row?.id !== recording.row || gameVoice.project?.id !== recording.project) return;
  recording.saving = true;
  renderGameRecording(recording, tr('正在保存引导音…'));
  try {
    const saved = await uploadGameGuide(recording.file);
    if (gameRecording !== recording) return;
    if (saved) {
      $('#game-voice-details').open = $('#game-guide-panel').open = true;
      gameRecordDialog.close();
      toast(tr('中文引导音已保存'));
    } else {
      $('#game-record-status').textContent = tr('引导音尚未保存，请检查当前台词后重试。');
    }
  } catch (error) {
    if (gameRecording === recording) $('#game-record-status').textContent = error.message;
  } finally {
    if (gameRecording === recording) {
      recording.saving = false;
      renderGameRecording(recording, $('#game-record-status').textContent);
    }
  }
});

gameRecordDialog.onclose = () => {
  if (!gameRecording) return;
  clearTimeout(gameRecording.timer);
  cancelGameRecordingToken(gameRecording);
  clearGameRecordingPreview(gameRecording);
  gameRecording = null;
};
window.addEventListener('pagehide', () => { if (gameRecording) cancelGameRecordingToken(gameRecording); });
$('#game-guide-play').onclick = () => {
  const audio = $('#game-guide-audio');
  audio.src = `${gameLinePath()}/guide?v=${gameVoice.row.revision}`;
  audio.hidden = false;
  audio.play().catch(() => {});
};
$('#game-guide-remove').onclick = () => guard(async () => {
  if (!await saveGameLine()) return;
  gameVoice.row = await api(`${gameLinePath()}/guide`, json('DELETE', {revision: gameVoice.project.revision}));
  stopGameGuide();
  renderGameEditor();
  await refreshGameProject(true);
});

function renderGameSceneControls() {
  const scene = $('#game-scene').value;
  $('#game-scene-title').textContent = scene || tr('场景试听');
  $('#game-scene-hint').textContent = scene ? tr('按场景顺序试听。待决定的台词播放原版。') : tr('从场景筛选中选择一个场景，即可连续试听。');
  $('#game-scene-play').disabled = !scene || !gameVoice.project;
}

function stopGameScene() {
  ++gameScenePreview.request;
  gameScenePreview.rows = [];
  const audio = $('#game-scene-audio');
  audio.pause();
  audio.removeAttribute('src');
  audio.load();
  $('#game-scene-player').hidden = true;
}

async function startGameScene() {
  if (!await saveGameLine()) return;
  const scene = $('#game-scene').value;
  const project = gameVoice.project;
  stopGameScene();
  const request = gameScenePreview.request;
  const path = gameProjectPath();
  const rows = [];
  for (let offset = 0; ; offset = rows.length) {
    const result = await api(`${path}/rows?${new URLSearchParams({scene, offset, limit: 100})}`);
    if (request !== gameScenePreview.request || gameVoice.project?.id !== project.id || $('#game-scene').value !== scene) return;
    if (result.revision !== project.revision) throw Error(tr('项目已有新版本，请重新载入台词'));
    rows.push(...result.rows);
    if (!result.rows.length || rows.length >= result.total) break;
  }
  if (!rows.length) throw Error(tr('没有匹配的台词'));
  gameScenePreview.rows = rows;
  gameScenePreview.index = 0;
  $('#game-scene-player').hidden = false;
  playGameScene(0);
}

function playGameScene(index) {
  const row = gameScenePreview.rows[index];
  if (!row || !gameVoice.project) return;
  gameScenePreview.index = index;
  const language = gameVoice.project.language;
  const chinese = $('#game-scene-version').value === 'current' && row.decision === 'chinese';
  const text = chinese ? row.text : row[language];
  const mode = chinese ? '已采用中文' : row.decision === 'pending' ? '原版试听（待决定）' : '原版试听';
  $('#game-scene-now').textContent = `${index + 1}/${gameScenePreview.rows.length} · ${[tr(mode), row.role, text || `#${row.id}`].filter(Boolean).join(' · ')}`;
  const audio = $('#game-scene-audio');
  audio.src = `${gameLinePath(row.id)}${chinese ? `/candidates/${row.selected}?track=mix` : `/source/${language}?native=true`}`;
  audio.play().catch(() => {});
  $('#game-scene-previous').disabled = index === 0;
  $('#game-scene-next').disabled = index + 1 === gameScenePreview.rows.length;
}

$('#game-scene-play').onclick = () => guard(startGameScene);
$('#game-scene-stop').onclick = stopGameScene;
$('#game-scene-previous').onclick = () => playGameScene(gameScenePreview.index - 1);
$('#game-scene-next').onclick = () => playGameScene(gameScenePreview.index + 1);
$('#game-scene-version').onchange = () => playGameScene(gameScenePreview.index);
$('#game-scene-audio').onended = () => playGameScene(gameScenePreview.index + 1);
$('#game-scene-audio').onerror = () => {
  if (gameScenePreview.rows.length) $('#game-scene-now').textContent += ` · ${tr('音频读取失败')}`;
};

async function previewGamePackage(command, rows) {
  if (!await saveGameLine()) return;
  const {id, revision} = gameVoice.project;
  const release = command === 'package-release';
  const result = await api(`${gameProjectPath()}/package-preview`, json('POST', {rows, release, revision}));
  if (gameVoice.project?.id !== id || gameVoice.project.revision !== revision) return;
  const labels = {replace: '已采用中文', original: '已保留原版', pending: '待决定', unsupported: '资源当前不可替换',
    untested: '待实机验收', missing: '候选音频缺失', overrun: '超出配音窗口'};
  const replaced = result.rows.filter(row => row.status === 'replace').length;
  const original = result.rows.filter(row => row.status === 'original').length;
  const blocked = result.rows.length - original - replaced;
  $('#game-package-summary').textContent = tr('{0} 条采用中文 · {1} 条保留原版 · {2} 条待处理', replaced, original, blocked);
  $('#game-package-review').innerHTML = result.rows.map(row => `<li data-status="${row.status}"><div><strong>${esc(tr(labels[row.status]))}</strong><span>${esc([row.scene, row.role, `#${row.id}`].filter(Boolean).join(' · '))}</span></div><p>${esc(row.text)}</p>${row.events.length > 1 ? `<small>${esc(tr('关联事件 · {0}', row.events.join('、')))}</small>` : ''}</li>`).join('');
  gamePackageRequest = {id, revision, rows, release};
  $('#game-package-confirm').disabled = Boolean(blocked) || !replaced;
  $('#game-package-dialog').showModal();
}

$('#game-package-confirm').onclick = () => guard(async () => {
  const {id, revision, rows, release} = gamePackageRequest;
  $('#game-package-dialog').close();
  if (gameVoice.project?.id !== id || gameVoice.project.revision !== revision) throw Error(tr('项目已有新版本，请重新载入台词'));
  gameVoice.project = await api(`${gameProjectPath()}/jobs`, json('POST', {action: 'package', rows, release, revision}));
  renderGameProject();
});

$('#game-adopt').onclick = () => guard(async () => {
  gameVoice.row = await api(`${gameLinePath()}/adopt`, json('POST', {candidate: gameVoice.candidate, revision: gameVoice.row.revision}));
  renderGameEditor();
  await refreshGameProject(true);
});
$('#game-tested').onclick = () => guard(async () => {
  gameVoice.row = await api(`${gameLinePath()}/test-result`, json('POST', {tested: !gameVoice.row.tested, revision: gameVoice.row.revision}));
  renderGameEditor();
  await refreshGameProject(true);
});

$('#game-import').onclick = () => guard(async () => {
  if (!await saveGameLine()) return;
  let result;
  try {
    result = await api(`${gameProjectPath()}/import-text`, json('POST', {revision: gameVoice.project.revision}));
  } catch (error) {
    if (error.status === 404) throw Error(tr('当前服务尚未加载台词导入接口。请重启 BTranslateVideo 后刷新页面。'));
    throw error;
  }
  gameVoice.project.revision = result.revision;
  await refreshGameProject(true);
  if (gameVoice.row && !gameVoice.dirty) await selectGameLine(gameVoice.row.id, true);
  if (!result.matched) throw Error(tr('没有找到与当前音频匹配的游戏台词'));
  if (!result.filled) toast(tr('没有可导入的空台词（{0} 条已有内容）', result.matched));
  else toast(tr('已导入 {0} 条台词，其中 {1} 条含中文', result.filled, result.chinese));
});

$('#game-role-voice-query').oninput = filterGameRoleVoices;
$('#game-role-voice-filter').onchange = filterGameRoleVoices;
$('#game-role-voice-list').onchange = () => {
  gameVoice.roleDirty = true;
  filterGameRoleVoices();
  renderGameControls();
};
$('#game-role-voice-list').onclick = event => {
  const extract = event.target.closest('[data-extract-voice]');
  if (extract) { guard(() => runExtractRoleVoice(extract.dataset.extractVoice)); return; }
  const audition = event.target.closest('[data-audition-voice]');
  if (audition) auditionRoleVoice(audition.dataset.auditionVoice);
};

let gameAuditionAudio = null;
function auditionRoleVoice(role) {
  const select = [...document.querySelectorAll('[data-role-voice]')].find(item => item.dataset.roleVoice === role);
  const voiceId = select?.value;
  if (!voiceId) { toast(tr('请先为该角色选择音色')); return; }
  const voice = gameVoice.voices.find(item => item.id === voiceId);
  if (gameAuditionAudio) gameAuditionAudio.pause();
  for (const audio of $$('#game-voices-page audio')) audio.pause();
  gameAuditionAudio = new Audio(`/api/voices/${encodeURIComponent(voiceId)}/audio?v=${encodeURIComponent(voice?.updated || '')}`);
  gameAuditionAudio.play().catch(error => toast(error.message));
  toast(tr('正在试听参考音：{0}', voice?.name || role));
}

async function runExtractRoleVoice(role) {
  if (!await saveGameLine()) return;
  if (gameBusy()) throw Error(tr('请等待当前任务结束'));
  if (gameVoice.roleDirty) throw Error(tr('请先保存角色音色'));
  gameVoice.project = await api(`${gameProjectPath()}/roles/${encodeURIComponent(role)}/voice-from-source`,
    json('POST', {revision: gameVoice.project.revision, name: tr('{0}（游戏原声）', role)}));
  renderGameProject();
  toast(tr('正在从原声提取 {0} 的音色', role));
}
$('#game-auto-role-voices').onclick = () => {
  const normalize = value => String(value || '').toLowerCase().replace(/[\s_\-·:：()（）]/g, '');
  let matched = 0;
  for (const select of document.querySelectorAll('[data-role-voice]')) {
    if (select.value && !select.selectedOptions[0]?.disabled) continue;
    const role = normalize(select.dataset.roleVoice);
    const voice = gameVoice.voices.find(item => normalize(item.name) === role ||
      (role.length >= 2 && normalize(item.description).includes(role)));
    if (voice) { select.value = voice.id; matched++; }
  }
  if (matched) {
    gameVoice.roleDirty = true;
    filterGameRoleVoices();
    renderGameControls();
    toast(tr('已按名称匹配 {0} 个角色，可调整后保存', matched));
  } else {
    toast(tr('没有可自动匹配的角色，请手动选择'));
  }
};
$('#game-save-role-voices').onclick = () => guard(async () => {
  const mapping = {};
  for (const select of document.querySelectorAll('[data-role-voice]')) {
    if (select.value) mapping[select.dataset.roleVoice] = select.value;
  }
  gameVoice.project = await api(`${gameProjectPath()}/role-voices`,
    json('PUT', {revision: gameVoice.project.revision, role_voices: mapping}));
  gameVoice.roleDirty = false;
  renderGameProject();
  toast(tr('角色音色已保存'));
});
$('#game-vc').onchange = () => guard(async () => {
  if (!await saveGameLine()) { $('#game-vc').checked = Boolean(gameVoice.project.vc); return; }
  const enabled = $('#game-vc').checked;
  gameVoice.project = await api(`${gameProjectPath()}/voice-conversion`,
    json('PUT', {revision: gameVoice.project.revision, enabled}));
  renderGameProject();
  toast(tr(enabled ? '已开启变声：中文音色发音，角色音色变声' : '已关闭变声'));
});
$('#game-batch-voice').onchange = () => {
  try { localStorage.setItem(gameBatchVoiceKey(), $('#game-batch-voice').value || 'none'); } catch {}
  renderGameControls();
};
$('#game-action').onchange = renderGameControls;
$('#game-batch-emotion').onchange = renderGameControls;
$('#game-force-batch-settings').onchange = renderGameControls;
for (const id of ['game-line-route', 'game-batch-route']) {
  $('#' + id).onchange = event => {
    renderGameControls();
    if (gameDramaRoute(event.target.value) || event.target.value === 'warm') guard(refreshGameDramaTools);
  };
}

async function refreshGameDramaTools() {
  gameVoice.tools = await api(`${gameApi}/tools`);
  renderGameTools();
  renderGameControls();
}

setInterval(() => {
  if (state.section === 'game-voices' && gameVoice.project && !gameBusy() && gameVoice.tools?.warm_model) {
    refreshGameDramaTools().catch(() => {});
  }
}, 5000);
$('#game-run').onclick = () => guard(() => {
  const action = $('#game-action').value;
  const rows = [...gameVoice.checked];
  if (action.startsWith('original-')) return setGameOriginal(rows, action === 'original-keep', true);
  if (action.startsWith('package-')) return previewGamePackage(action, rows);
  return runGameJob(action, rows, action === 'generate' && $('#game-force-batch-settings').checked, $('#game-batch-route').value);
});
$('#game-show-skipped').onclick = () => {
  const skipped = gameVoice.project?.job?.skipped_rows || [];
  if (skipped.length) {
    // Leave exactly the skipped lines selected so the next translate/ASR step targets them.
    gameVoice.checked = new Set(skipped);
    renderGameControls();
  }
  $('#game-status').value = 'skipped';
  $('#game-status').dispatchEvent(new Event('change'));
  document.querySelector('.game-lines')?.scrollIntoView({block: 'start', behavior: 'smooth'});
};
$('#game-generate-one').onclick = () => guard(() => runGameJob(Object.keys(gameVoice.row.candidates).length ? 'alternate' : 'generate',
  [gameVoice.row.id], false, $('#game-line-route').value));
$('#game-polish-one').onclick = () => guard(() => runGameJob('polish', [gameVoice.row.id]));
$('#game-use-suggestion').onclick = () => {
  const field = gameForm().elements.text;
  field.value = gameVoice.row.suggested_text;
  field.dispatchEvent(new Event('input', {bubbles: true}));
  field.focus();
};
$('#game-retry').onclick = () => guard(async () => {
  if (!await saveGameLine()) return;
  gameVoice.project = await api(`${gameProjectPath()}/retry`, json('POST', {revision: gameVoice.project.revision}));
  renderGameProject();
});
$('#game-pronunciation').onsubmit = event => {
  event.preventDefault();
  const content = event.currentTarget.elements.rules.value;
  guard(async () => {
    const rules = content.split(/\r?\n/).filter(line => line.trim()).map(line => {
      const separator = line.indexOf('=');
      if (separator < 1 || !line.slice(separator + 1).trim()) throw Error(tr('发音规则格式为 关键词 = 读法'));
      return {term: line.slice(0, separator).trim(), spoken: line.slice(separator + 1).trim()};
    });
    if (!await saveGameLine()) return;
    gameVoice.project = await api(`${gameProjectPath()}/pronunciation`, json('PUT', {revision: gameVoice.project.revision, rules}));
    await refreshGameProject(true);
  });
};
$('#game-cancel').onclick = () => guard(async () => {
  gameVoice.project = await api(`${gameProjectPath()}/cancel`, {method: 'POST'});
  await refreshGameProject(true);
});

async function runGameJob(command, rows, forceBatchSettings = false, route = 'project') {
  if (!await saveGameLine()) return;
  // 'asr-translate' maps to the backend's combined recognition + translation job.
  const action = command === 'asr-translate' ? 'transcribe_translate' : command.split('-')[0];
  const option = command.split('-')[1];
  const projectLanguage = gameVoice.project?.language === 'en' ? 'en' : 'ja';
  const generating = action === 'generate' || action === 'alternate';
  if (generating && (gameDramaRoute(route) || route === 'warm')) await refreshGameDramaTools();
  if (generating && !gameDramaRoute(route) && !(route === 'warm' && gameVoice.tools?.drama3_warm) && rows.length === 1 &&
      !forceBatchSettings && !gameVoice.row?.voice && !$('#game-batch-voice').value) {
    // Voice priority on the server: line voice → role voice → batch default → TTS default.
    // An empty batch selector is a valid choice, so explain the fallback instead of
    // blocking the job when a character-specific voice may already be bound.
    const role = gameVoice.row?.role || '';
    const roleVoice = gameVoice.project?.role_voices?.[role];
    if (!roleVoice) {
      toast(role
        ? tr('角色「{0}」未绑定音色，单条生成将使用 TTS 服务默认音色；可在「角色音色」中绑定。', role)
        : tr('未指定音色，单条生成将使用 TTS 服务默认音色；可在「角色音色」中绑定角色音色。'));
    }
  }
  const emotion = action === 'generate' || action === 'alternate' ? $('#game-batch-emotion').value : '';
  const payload = {action, rows, revision: gameVoice.project.revision, voice: $('#game-batch-voice').value,
    language: action === 'asr' ? option : projectLanguage, release: option === 'release', emotion,
    force_batch_settings: forceBatchSettings, route};
  gameVoice.project = await api(`${gameProjectPath()}/jobs`, json('POST', payload));
  renderGameProject();
  const total = gameVoice.project.job?.total || 0;
  if ((action === 'generate' || action === 'alternate') && rows.length > total) {
    toast(tr('已跳过 {0} 条没有可朗读内容的台词', rows.length - total));
  }
}

setInterval(async () => {
  if (state.section !== 'game-voices' || !gameBusy() || gameVoice.polling || gameVoice.saving) return;
  gameVoice.polling = true;
  try { await refreshGameProject(); }
  catch (error) { $('#game-job-error').textContent = error.message; }
  finally { gameVoice.polling = false; }
}, 1200);
