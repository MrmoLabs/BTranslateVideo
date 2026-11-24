'use strict';
/* Redesigned game audio localization workspace.
 *
 * game-voices.js keeps the project state, API calls and data transforms.
 * This layer keeps that contract (same ids, same request shapes) but replaces
 * the lines-table rendering, adds a real filter/pagination toolbar and turns
 * the single-line inspector into a compact audition + editing panel.
 */
(() => {
  if (typeof gameVoice === 'undefined' || typeof gameVoice.rows === 'undefined') {
    console.warn('game-voices-ui: game-voices.js is not loaded');
    return;
  }
  const PAGE_SIZES = [20, 50, 100];
  const SIZE_KEY = 'game-voice-page-size';
  const STATUS_CHIPS = [
    ['', '全部'], ['replaceable', '可替换'], ['unvoiced', '待配音'],
    ['no-script', '缺原文'], ['no-translation', '缺译文'], ['no-text', '缺中文稿'], ['has-suggestion', '有口语建议'], ['timing', '配音超时'],
    ['risky', '建议保留原声'], ['original', '已保留原版'], ['pending', '待决定'], ['ready', '已采用'], ['tested', '实机通过'],
  ];
  const FIT_LABELS = {short: '极短', shout: '喊叫', mixed: '可能混音'};
  const ui = {
    pageSize: PAGE_SIZES.includes(Number(localStorage.getItem(SIZE_KEY))) ? Number(localStorage.getItem(SIZE_KEY)) : 50,
    enhanced: false,
    filterTimer: 0,
    editorRow: '',
  };
  gameVoice.page = Number.isInteger(gameVoice.page) ? gameVoice.page : 0;

  const pages = () => Math.max(1, Math.ceil(gameVoice.total / ui.pageSize));
  const rowsPath = () => `${gameProjectPath()}/rows`;
  const currentPageSize = () => {
    const value = Number($('#game-page-size')?.value);
    return PAGE_SIZES.includes(value) ? value : ui.pageSize;
  };

  function lineState(row) {
    if (row.decision === 'original') return {key: 'original', label: '已保留原版'};
    if (row.tested) return {key: 'tested', label: '实机通过'};
    if (row.selected) return {key: 'ready', label: '已采用'};
    if (row.generated) return {key: 'generated', label: '待确认'};
    if (!row.ja && !row.en) return {key: 'nosource', label: '待识别'};
    if (!row.text) return {key: 'untranslated', label: '待翻译'};
    if (!row.replaceable) return {key: 'preview', label: '仅试听'};
    return {key: 'replaceable', label: '可替换'};
  }

  function candidateSummary(row) {
    if (row.duration == null || row.decision === 'original') return '';
    const parts = [tr('{0} 秒', row.duration.toFixed(2))];
    if (row.delta > .15) parts.push(tr('超出原声 {0} 秒', row.delta.toFixed(2)));
    else if (row.delta != null && row.delta > 0) parts.push(tr('与原声相差 {0} 秒', row.delta.toFixed(2)));
    return parts.join(' · ');
  }

  // ---------------------------------------------------------------- rows
  function loadRows() {
    const request = ++gameVoice.listRequest;
    const values = new URLSearchParams({
      q: $('#game-query').value, group: $('#game-group').value,
      role: $('#game-role').value, scene: $('#game-scene').value,
      status: $('#game-status').value,
      offset: String(gameVoice.page * ui.pageSize), limit: String(ui.pageSize),
    });
    return api(`${rowsPath()}?${values}`).then(data => {
      if (request !== gameVoice.listRequest) return;
      gameVoice.rows = data.rows;
      gameVoice.total = data.total;
      if (!data.rows.length && gameVoice.page > 0) {
        gameVoice.page = Math.max(0, pages() - 1);
        return loadRows();
      }
      renderGameRows();
    });
  }

  function renderRows() {
    const current = gameVoice.row?.id;
    $('#game-rows').innerHTML = gameVoice.rows.map(row => {
      const state = lineState(row);
      const primary = row.text || row.ja || row.en;
      const secondary = row.text && (row.ja || row.en) ? (row.ja || row.en) : '';
      const meta = [row.scene_order ? tr('场景第 {0} 句', row.scene_order) : '', `#${row.id}`, row.role, row.scene || row.group].filter(Boolean).join(' · ');
      const candidate = candidateSummary(row);
      const fit = (row.tts_fit || '').split(',').filter(value => value in FIT_LABELS);
      const fitText = fit.map(value => tr(FIT_LABELS[value])).join(' · ');
      return `<tr class="${row.id === current ? 'game-row-current' : ''}">
        <td class="game-col-check"><input type="checkbox" data-check-line="${row.id}" aria-label="${esc(tr('选择语音 {0}', row.id))}" ${gameVoice.checked.has(row.id) ? 'checked' : ''}></td>
        <td class="game-col-line"><button type="button" class="game-open-row" data-open-line="${row.id}" aria-current="${row.id === current ? 'true' : 'false'}">
          <span class="game-line-text${primary ? '' : ' game-line-empty'}" title="${esc(primary || tr('待识别'))}">${esc(primary || tr('待识别'))}</span>
          ${secondary ? `<span class="game-line-source">${esc(secondary)}</span>` : ''}
          <span class="game-line-meta">${esc(meta)}</span></button></td>
        <td class="game-col-state"><span class="game-state" data-state="${state.key}">${esc(tr(state.label))}</span>${fit.length ? `<span class="game-state" data-state="risky" title="${esc(tr('建议保留原声'))}">${esc(fitText)}</span>` : ''}${candidate ? `<small>${esc(candidate)}</small>` : ''}</td>
        <td class="game-col-actions"><div class="game-row-actions">
          <button type="button" data-preview-line="${row.id}">${esc(tr('试听'))}</button>
          <button type="button" data-asr-line="${row.id}">${esc(tr('识别'))}</button>
          <button type="button" data-translate-line="${row.id}">${esc(tr('翻译'))}</button>
        </div></td>
      </tr>`;
    }).join('') || `<tr class="game-empty-row"><td colspan="4">${esc(tr('没有匹配的台词'))}</td></tr>`;
    renderGameControls();
  }

  // ------------------------------------------------------------- controls
  function renderChrome() {
    for (const el of [$('#game-total'), $('#game-list-total')]) {
      if (el) el.textContent = tr('共 {0} 条', gameVoice.total || 0);
    }
    const chipLabels = {all: '全部'};
    for (const chip of $$('.game-chip')) {
      const label = STATUS_CHIPS.find(([value]) => value === chip.dataset.status)?.[1];
      if (label) chip.textContent = tr(label);
      chip.setAttribute('aria-pressed', String(chip.dataset.status === $('#game-status').value));
    }
    if ($('#game-filter-clear')) $('#game-filter-clear').textContent = tr('清空筛选');
    if ($('#game-analyze-fit')) {
      $('#game-analyze-fit').textContent = tr('分析当前筛选');
      $('#game-analyze-fit').title = tr('按原声时长与频谱标出极短、喊叫、可能混音的句子');
    }
    if ($('#game-show-skipped')) $('#game-show-skipped').textContent = tr('查看并处理');
    if ($('#game-page-jump')) $('#game-page-jump').textContent = tr('跳转');
    if ($('#game-page-size-label')) $('#game-page-size-label').textContent = tr('每页条数');
    if ($('#game-no-row')) $('#game-no-row').textContent = tr('从台词表选择一句，试听原声或候选，再编辑和生成。');
    const linePrev = $('#game-line-prev'), lineNext = $('#game-line-next');
    if (linePrev) { linePrev.textContent = tr('上一条'); linePrev.title = tr('上一条'); }
    if (lineNext) { lineNext.textContent = tr('下一条'); lineNext.title = tr('下一条'); }
  }

  const baseControls = renderGameControls;
  renderGameControls = function () {
    baseControls();
    const count = pages();
    const pageInput = $('#game-page-input');
    if (pageInput) {
      if (pageInput.max !== String(count)) pageInput.max = String(count);
      if (document.activeElement !== pageInput) pageInput.value = String(gameVoice.page + 1);
    }
    if ($('#game-page-count')) $('#game-page-count').textContent = String(count);
    if ($('#game-prev')) $('#game-prev').disabled = gameVoice.page <= 0;
    if ($('#game-next')) $('#game-next').disabled = gameVoice.page >= count - 1;
    for (const el of [$('#game-total'), $('#game-list-total')]) {
      if (el) el.textContent = tr('共 {0} 条', gameVoice.total || 0);
    }
    const pageSize = $('#game-page-size');
    if (pageSize && Number(pageSize.value) !== ui.pageSize) pageSize.value = String(ui.pageSize);
    updateLineNav();
  };

  function updateLineNav() {
    const prev = $('#game-line-prev'), next = $('#game-line-next');
    if (!prev || !next) return;
    const index = gameVoice.rows.findIndex(row => row.id === gameVoice.row?.id);
    prev.disabled = index <= 0;
    next.disabled = index < 0 || index >= gameVoice.rows.length - 1;
  }

  // -------------------------------------------------------------- filters
  function resetFilters() {
    $('#game-query').value = '';
    for (const id of ['game-group', 'game-scene', 'game-role']) $('#' + id).value = '';
    $('#game-status').value = '';
  }

  function applyFilters() {
    renderGameSceneControls();
    gameVoice.page = 0;
    renderChrome();
    clearTimeout(ui.filterTimer);
    ui.filterTimer = setTimeout(() => guard(() => gameVoice.project && loadRows()), 160);
  }

  async function runAnalyzeFit() {
    if (!await saveGameLine()) return;
    if (gameBusy()) throw Error(tr('请等待当前任务结束'));
    const body = {
      revision: gameVoice.project.revision,
      q: $('#game-query').value, group: $('#game-group').value,
      role: $('#game-role').value, scene: $('#game-scene').value,
      status: $('#game-status').value,
    };
    gameVoice.project = await api(`${gameProjectPath()}/analyze-fit`, json('POST', body));
    renderGameProject();
    toast(tr('正在分析原声，完成后可用「建议保留原声」筛选'));
  }

  // ------------------------------------------------------------ inspector
  function renderCandidates(row) {
    const list = $('#game-candidate-list');
    const entries = Object.entries(row.candidates || {}).reverse();
    if (!entries.length) {
      list.innerHTML = `<span class="game-candidate-empty">${esc(tr('暂无候选'))}</span>`;
      return;
    }
    list.innerHTML = entries.map(([id, item]) => {
      const active = id === gameVoice.candidate;
      const adopted = id === row.selected && row.decision !== 'original';
      const duration = item.voice_duration ?? item.duration;
      const method = gameMethod(item.synthesis?.method);
      const voice = item.synthesis?.method === 'drama3' && !item.synthesis.voice_id ? tr(item.voice) : item.voice;
      const detail = [voice || tr('服务音色'), method && tr(method), gameCandidateEmotion(item)].filter(Boolean).join(' · ');
      return `<div class="game-candidate-card"><button type="button" class="game-candidate-item" data-candidate-id="${esc(id)}" aria-pressed="${active}" title="${esc(detail)}">
        <span class="game-candidate-icon" aria-hidden="true">▶</span><span><strong>${esc(tr('{0} 秒', duration.toFixed(2)))}${adopted ? ' · ' + esc(tr('已采用')) : ''}</strong><small>${esc(detail)}</small></span></button>
        <button type="button" class="game-candidate-delete" data-delete-candidate="${esc(id)}" aria-label="${esc(tr('删除候选版本'))}" title="${esc(tr('删除候选版本'))}">${esc(tr('删除'))}</button></div>`;
    }).join('');
  }

  async function deleteCandidate(candidate) {
    const row = gameVoice.row;
    const projectId = gameVoice.project?.id;
    if (!row?.candidates[candidate] || gameBusy() || gameVoice.saving) return;
    const adopted = row.selected === candidate && row.decision !== 'original';
    const decision = await confirmChoice(tr(adopted ? '删除已采用的候选后，本句将回到待决定。' : '删除这条候选配音？'),
      {title: tr('删除候选版本'), proceed: tr('删除')});
    if (decision === 'cancel' || gameVoice.project?.id !== projectId || gameVoice.row?.id !== row.id || gameBusy() || gameVoice.saving) return;
    gameVoice.saving = true;
    renderGameControls();
    try {
      const path = `${gameLinePath(row.id)}/candidates/${candidate}`;
      const updated = await api(path, json('DELETE', {revision: gameVoice.row.revision}));
      if (gameVoice.project?.id !== projectId || gameVoice.row?.id !== row.id) return;
      gameVoice.row = updated;
      gameVoice.project.revision = updated.revision;
      if (gameVoice.playback?.path.split('?')[0] === path) clearGamePlayer();
      if (gameVoice.candidate === candidate) {
        gameVoice.candidate = updated.selected || Object.keys(updated.candidates).at(-1) || '';
      }
      renderCandidates(updated);
      await refreshGameProject(true);
      ($('#game-candidate-list [aria-pressed="true"]') || $('#game-generate-one')).focus();
      toast(tr('候选版本已删除'));
    } finally {
      gameVoice.saving = false;
      renderGameControls();
    }
  }

  const baseEditor = renderGameEditor;
  renderGameEditor = function () {
    baseEditor();
    const row = gameVoice.row;
    renderChrome();
    if (!row) {
      ui.editorRow = '';
      $('#game-candidate-list').replaceChildren();
      return;
    }
    renderCandidates(row);
    renderGameControls();
    // Untranslated lines need their Japanese/English reference in sight.
    const sourceDetails = gameForm().querySelector('.game-source-details');
    if (sourceDetails && ui.editorRow !== row.id) {
      ui.editorRow = row.id;
      sourceDetails.open = !row.text && Boolean(row.ja || row.en);
      $('#game-voice-details').open = true;
      $('.game-inspector').scrollTop = 0;
    }
    updateLineNav();
  };

  // ---------------------------------------------------------------- wiring
  function enhance() {
    if (ui.enhanced) return;
    const inspector = $('.game-inspector');
    const lines = $('.game-lines');
    const filters = $('.game-filters');
    const statusSelect = $('#game-status');
    const pager = $('.board-pagination');
    if (!inspector || !lines || !filters || !statusSelect || !pager) return;
    ui.enhanced = true;

    // lines heading: result count next to the title
    const heading = lines.querySelector('.card-heading');
    if (heading && !$('#game-total')) {
      const total = document.createElement('span');
      total.id = 'game-total';
      total.className = 'game-list-total';
      heading.insertBefore(total, heading.querySelector('#game-selection'));
    }
    // table header: add the actions column
    const headRow = lines.querySelector('.game-table thead tr');
    if (headRow && headRow.children.length === 3) {
      headRow.children[1].classList.add('game-col-line-head');
      headRow.children[2].classList.add('game-col-state-head');
      const actions = document.createElement('th');
      actions.className = 'game-col-action-head';
      actions.textContent = tr('操作');
      headRow.append(actions);
    }

    // batch bar: move the page-level checkboxes close to the run controls
    const batch = lines.querySelector('.game-batch');
    if (batch) {
      const clear = $('#game-clear');
      if (clear) clear.textContent = tr('清空选择');
    }

    // status chips + clear filters
    const statusLabel = statusSelect.closest('label');
    if (statusLabel && !statusLabel.classList.contains('game-status-field')) {
      statusLabel.classList.add('game-status-field');
      statusSelect.classList.add('game-status-select');
      for (const [value, label] of STATUS_CHIPS) {
        if (![...statusSelect.options].some(option => option.value === value)) {
          statusSelect.append(new Option(label, value));
        }
      }
      // Internal filter used by the skipped-lines notice; not shown as a chip.
      if (![...statusSelect.options].some(option => option.value === 'skipped')) {
        statusSelect.append(new Option(tr('上次跳过'), 'skipped'));
      }
      const chips = document.createElement('div');
      chips.className = 'game-chips';
      chips.setAttribute('role', 'group');
      chips.setAttribute('aria-label', tr('状态'));
      chips.innerHTML = STATUS_CHIPS.map(([value, label]) =>
        `<button type="button" class="game-chip" data-status="${value}" aria-pressed="${value === statusSelect.value}">${esc(tr(label))}</button>`).join('');
      chips.addEventListener('click', event => {
        const chip = event.target.closest('.game-chip');
        if (!chip) return;
        statusSelect.value = chip.dataset.status;
        statusSelect.dispatchEvent(new Event('change'));
      });
      const clear = document.createElement('button');
      clear.type = 'button';
      clear.id = 'game-filter-clear';
      clear.className = 'text-button';
      clear.textContent = tr('清空筛选');
      clear.addEventListener('click', () => {
        resetFilters();
        statusSelect.value = '';
        statusSelect.dispatchEvent(new Event('change'));
      });
      const analyze = document.createElement('button');
      analyze.type = 'button';
      analyze.id = 'game-analyze-fit';
      analyze.className = 'text-button';
      analyze.textContent = tr('分析当前筛选');
      analyze.title = tr('按原声时长与频谱标出极短、喊叫、可能混音的句子');
      analyze.addEventListener('click', () => guard(runAnalyzeFit));
      statusLabel.append(chips, analyze, clear);
    }

    // pagination: total, page jump and page size
    pager.classList.add('game-pager');
    if (!pager.dataset.enhanced) {
      pager.dataset.enhanced = '1';
      const total = document.createElement('span');
      total.id = 'game-list-total';
      total.className = 'game-list-total';
      pager.prepend(total);
      const jump = document.createElement('label');
      jump.className = 'game-page-jump';
      jump.innerHTML = `<input id="game-page-input" type="number" min="1" step="1" inputmode="numeric" aria-label="${esc(tr('页码'))}"><span class="game-page-of">/ <b id="game-page-count">1</b></span><button type="button" id="game-page-jump" class="text-button">${esc(tr('跳转'))}</button>`;
      const size = document.createElement('label');
      size.className = 'game-page-size';
      size.innerHTML = `<span id="game-page-size-label">${esc(tr('每页条数'))}</span><select id="game-page-size" aria-label="${esc(tr('每页条数'))}">${PAGE_SIZES.map(value => `<option value="${value}">${value}</option>`).join('')}</select>`;
      const next = pager.querySelector('#game-next');
      if (next) pager.insertBefore(jump, next); else pager.append(jump);
      pager.append(size);
      const clear = pager.querySelector('#game-clear');
      if (clear) pager.append(clear);
    }

    // Line navigation stays in the inspector header.
    const cardHeading = inspector.querySelector('.card-heading');
    if (cardHeading && !inspector.querySelector('.game-inspector-nav')) {
      cardHeading.classList.add('game-inspector-head');
      const nav = document.createElement('div');
      nav.className = 'game-inspector-nav';
      nav.innerHTML = `<button type="button" id="game-line-prev" class="text-button" title="${esc(tr('上一条'))}">${esc(tr('上一条'))}</button><button type="button" id="game-line-next" class="text-button" title="${esc(tr('下一条'))}">${esc(tr('下一条'))}</button>`;
      cardHeading.append(nav);
      nav.querySelector('#game-line-prev').addEventListener('click', () => {
        const index = gameVoice.rows.findIndex(row => row.id === gameVoice.row?.id);
        if (index > 0) guard(() => selectGameLine(gameVoice.rows[index - 1].id));
      });
      nav.querySelector('#game-line-next').addEventListener('click', () => {
        const index = gameVoice.rows.findIndex(row => row.id === gameVoice.row?.id);
        if (index >= 0 && index < gameVoice.rows.length - 1) guard(() => selectGameLine(gameVoice.rows[index + 1].id));
      });
    }

    const candidates = $('#game-candidate-list');
    candidates.addEventListener('click', event => {
      const remove = event.target.closest('[data-delete-candidate]');
      if (remove) { guard(() => deleteCandidate(remove.dataset.deleteCandidate)); return; }
      const item = event.target.closest('[data-candidate-id]');
      if (!item) return;
      gameVoice.candidate = item.dataset.candidateId;
      for (const button of candidates.querySelectorAll('[data-candidate-id]')) {
        button.setAttribute('aria-pressed', String(button === item));
      }
      renderGameControls();
      guard(() => loadGameCandidate('', true));
    });

    // row preview + editing controls
    $('#game-rows').addEventListener('click', event => {
      const play = event.target.closest('[data-preview-line]');
      if (play) {
        event.preventDefault();
        event.stopPropagation();
        guard(async () => {
          const id = play.dataset.previewLine;
          if (gameVoice.row?.id !== id) {
            await selectGameLine(id);
            if (gameVoice.row?.id !== id) return;
          }
          const language = gameVoice.project?.language === 'en' ? 'en' : 'ja';
          await gameWave('source', `${gameLinePath(id)}/source/${language}`, null, true);
        });
        return;
      }
      // Single-line speech recognition and translation reuse the project's job API.
      const asr = event.target.closest('[data-asr-line]');
      if (asr) {
        event.preventDefault();
        event.stopPropagation();
        const language = gameVoice.project?.language === 'en' ? 'en' : 'ja';
        guard(() => runGameJob(`asr-${language}`, [asr.dataset.asrLine]));
        return;
      }
      const translate = event.target.closest('[data-translate-line]');
      if (translate) {
        event.preventDefault();
        event.stopPropagation();
        guard(() => runGameJob('translate', [translate.dataset.translateLine]));
      }
    });

    // filters + pagination controls
    $('#game-query').oninput = applyFilters;
    for (const id of ['game-group', 'game-role', 'game-status']) $('#' + id).onchange = applyFilters;
    $('#game-scene').onchange = () => { stopGameScene(); applyFilters(); };
    $('#game-prev').onclick = () => guard(async () => {
      if (gameVoice.page <= 0) return;
      gameVoice.page--;
      await loadRows();
    });
    $('#game-next').onclick = () => guard(async () => {
      if (gameVoice.page >= pages() - 1) return;
      gameVoice.page++;
      await loadRows();
    });
    const jumpTo = () => {
      const value = Number($('#game-page-input').value);
      if (!Number.isInteger(value) || value < 1 || value > pages()) return;
      gameVoice.page = value - 1;
      guard(() => loadRows());
    };
    $('#game-page-jump').addEventListener('click', jumpTo);
    $('#game-page-input').addEventListener('keydown', event => {
      if (event.key === 'Enter') { event.preventDefault(); jumpTo(); }
    });
    $('#game-page-size').addEventListener('change', event => {
      const value = Number(event.target.value);
      if (!PAGE_SIZES.includes(value)) return;
      ui.pageSize = value;
      try { localStorage.setItem(SIZE_KEY, String(value)); } catch {}
      gameVoice.page = 0;
      guard(() => loadRows());
    });
    $('#game-page-size').value = String(ui.pageSize);

    renderChrome();
  }

  const baseProject = renderGameProject;
  renderGameProject = function () {
    baseProject();
    enhance();
    $('#game-voices-page').classList.toggle('game-project-open', Boolean(gameVoice.project));
    renderChrome();
    renderGameControls();
  };
  const baseIndex = renderGameIndex;
  renderGameIndex = function () {
    baseIndex();
    $('#game-voices-page').classList.toggle('game-project-open', Boolean(gameVoice.project));
  };

  enhance();
  renderGameRows = renderRows;
  loadGameRows = loadRows;
})();
