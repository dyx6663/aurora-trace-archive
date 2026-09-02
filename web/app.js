const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
let activeRun = null;
let renderedEvents = 0;
let projects = new Map();
let lastDiff = '';
let lastVerificationEvent = null;

function eventHtml(event) {
  const tool = event.tool ? `<code>${esc(event.tool)}</code>` : '';
  const titles = {
    'Run initialized': '已建立隔离副本',
    'Task understood': '任务已登记',
    'Acceptance contract locked': '验收条件已锁定',
    'Execution started': '开始执行',
    'Verification gate passed': '验收门通过',
    'Task completed': '任务完成',
    'Run stopped': '执行已停止',
  };
  let title = titles[event.title] || event.title;
  if (event.title.startsWith('Selected ')) {
    const action = {list_files:'读取项目结构', read_file:'读取文件', write_file:'写入文件', replace_text:'应用精确补丁', run_command:'运行验证命令'};
    title = action[event.tool] || '准备调用工具';
  } else if (event.title.endsWith(' returned')) {
    const action = {list_files:'项目结构已读取', read_file:'文件已读取', write_file:'文件已更新', replace_text:'精确补丁已应用', run_command:'验证命令已返回'};
    title = action[event.tool] || '工具已完成';
  } else if (event.title.endsWith(' blocked')) {
    const action = {list_files:'项目结构读取被阻止', read_file:'文件读取被阻止', write_file:'文件写入被阻止', replace_text:'补丁应用被阻止', run_command:'验证命令被阻止'};
    title = action[event.tool] || '工具调用被阻止';
  }
  return `<article class="event ${esc(event.kind)}"><div class="event-top"><b>${esc(title)} ${tool}</b><time>${esc(event.time)}</time></div><p>${esc(event.detail || '')}</p></article>`;
}

function appendEvents(events) {
  if (!events.length) return;
  if (renderedEvents === 0) $('#events').innerHTML = '';
  const fresh = events.slice(renderedEvents);
  if (!fresh.length) return;
  $('#events').insertAdjacentHTML('beforeend', fresh.map(eventHtml).join(''));
  renderedEvents = events.length;
  $('#events').scrollTop = $('#events').scrollHeight;
}

function renderDiff(diffs) {
  const raw = (diffs || []).join('\n');
  if (!raw || raw === lastDiff) return;
  lastDiff = raw;
  $('#diff').innerHTML = raw.split('\n').map(line => {
    const safe = esc(line);
    if (line.startsWith('+') && !line.startsWith('+++')) return `<span class="diff-add">${safe}</span>`;
    if (line.startsWith('-') && !line.startsWith('---')) return `<span class="diff-del">${safe}</span>`;
    return safe;
  }).join('\n');
}

function update(data) {
  const events = data.events || [];
  appendEvents(events);
  $('#eventCount').textContent = `${events.length} 条`;
  $('#exportBtn').disabled = !activeRun;
  const state = data.state || 'IDLE';
  const labels = {IDLE:'待命', QUEUED:'排队中', UNDERSTAND:'理解任务', PLAN:'制定计划', EXECUTE:'执行中', VERIFY:'验证中', COMPLETED:'已完成', FAILED:'失败'};
  const pill = $('#statePill');
  pill.textContent = labels[state] || state;
  pill.className = 'status-pill ' + (state === 'COMPLETED' ? 'done' : state === 'FAILED' ? 'failed' : state !== 'IDLE' ? 'running' : 'idle');
  const calls = events.filter(event => event.kind === 'decision').length;
  const score = data.trust_score || 0;
  $('#metricIter').textContent = String(calls).padStart(2, '0');
  $('#metricTools').textContent = String(calls).padStart(2, '0');
  $('#metricScore').textContent = String(score).padStart(2, '0');
  $('#progress').textContent = String(score).padStart(2, '0');
  $('#healthText').textContent = state === 'COMPLETED' ? '验收通过' : state === 'FAILED' ? '执行中断' : state === 'IDLE' ? '等待执行' : '正在执行';
  $('#healthSub').textContent = data.summary || (state === 'IDLE' ? '尚未开始运行' : '正在读取项目、应用改动并运行测试');
  $('#verified').textContent = state === 'COMPLETED' ? '● 已验证' : state === 'FAILED' ? '● 未通过' : '● 待验证';
  $('#verified').className = state === 'COMPLETED' ? 'verified ok' : 'verified';
  renderDiff(data.diffs);
  const evidence = data.evidence || {};
  const evidenceKeys = ['baseline_failure_captured', 'minimal_patch_recorded', 'regression_tests_passed', 'workspace_boundary_respected'];
  document.querySelectorAll('.contract div').forEach((element, index) => {
    const passed = Boolean(evidence[evidenceKeys[index]]);
    element.classList.toggle('checked', passed);
    element.firstElementChild.textContent = passed ? '✓' : '□';
  });
  const order = ['UNDERSTAND', 'PLAN', 'EXECUTE', 'VERIFY', 'COMPLETED'];
  const stateIndex = state === 'COMPLETED' ? 4 : order.indexOf(state);
  document.querySelectorAll('#states span').forEach((element, index) => element.classList.toggle('active', index <= stateIndex));
  const successfulCommand = [...events].reverse().find(event => event.kind === 'tool_result' && event.tool === 'run_command' && event.payload?.ok);
  if (successfulCommand && successfulCommand.id !== lastVerificationEvent) {
    lastVerificationEvent = successfulCommand.id;
    $('#verifyBody').innerHTML = `<div class="terminal-line"><span>›</span> <code>${esc(successfulCommand.payload.command)}</code></div><div class="terminal-line success"><span>✓</span> 回归测试通过 / regression passed</div><div class="terminal-line dim">${esc((successfulCommand.payload.output || '').slice(-280))}</div>`;
  }
}

function resetRunView() {
  renderedEvents = 0;
  lastDiff = '';
  lastVerificationEvent = null;
  $('#events').innerHTML = '<div class="empty"><div class="empty-icon">◌</div><b>正在建立隔离副本</b><span>系统即将读取项目并记录第一条事件。</span></div>';
  $('#diff').innerHTML = '<span class="dim">// 运行后将在这里显示实际改动</span>';
  $('#verifyBody').innerHTML = '<div class="terminal-line"><span>›</span> <code>等待测试命令</code></div><div class="terminal-line dim"><span>·</span> 尚未运行测试</div>';
  document.querySelectorAll('.contract div').forEach(element => { element.classList.remove('checked'); element.firstElementChild.textContent = '□'; });
}

async function poll() {
  if (!activeRun) return;
  try {
    const response = await fetch('/api/run/' + activeRun, {cache: 'no-store'});
    const data = await response.json();
    update(data);
    if (!data.finished) setTimeout(poll, 450);
    else {
      $('#runBtn').disabled = false;
      $('#runBtn').innerHTML = '<span>↻</span> 再次运行 <small>CTRL ↵</small>';
    }
  } catch (error) {
    showError('无法读取运行状态：' + error.message);
  }
}

function showError(message) {
  $('#runBtn').disabled = false;
  $('#runBtn').innerHTML = '<span>↻</span> 重试 <small>CTRL ↵</small>';
  $('#healthText').textContent = '无法启动';
  $('#healthSub').textContent = message;
}

function projectDescription(project) {
  const profile = project.profile || {};
  const languages = (profile.languages || ['Unknown']).join(' / ');
  const tests = (profile.suggested_tests || []).join(' · ');
  return `${languages} · ${profile.files ?? project.file_count ?? 0} 个文件 · ${tests}`;
}

async function loadProjects(selectedId = null) {
  const response = await fetch('/api/projects', {cache: 'no-store'});
  const data = await response.json();
  projects = new Map(data.projects.map(project => [project.id, project]));
  $('#project').innerHTML = data.projects.map(project => `<option value="${esc(project.id)}">${esc(project.name)}${project.source === 'uploaded' ? ' · 已导入' : ''}</option>`).join('');
  if (selectedId) $('#project').value = selectedId;
  updateProjectMeta();
}

function updateProjectMeta() {
  const project = projects.get($('#project').value);
  if (!project) return;
  $('#projectMeta').textContent = project.source === 'uploaded' ? '已导入 · ' + projectDescription(project) : '内置示例 · ' + projectDescription(project);
  if (project.source === 'uploaded') {
    $('#mode').value = 'live';
    $('#task').value = '请分析这个项目，定位并修复问题，运行可用测试，并给出修改证据。';
  }
}

$('#project').addEventListener('change', updateProjectMeta);
$('#uploadBtn').addEventListener('click', () => $('#projectFile').click());
$('#projectFile').addEventListener('change', async event => {
  const file = event.target.files[0];
  if (!file) return;
  $('#uploadBtn').disabled = true;
  $('#uploadBtn').textContent = '导入中…';
  try {
    const form = new FormData(); form.append('project', file);
    const response = await fetch('/api/projects/import', {method: 'POST', body: form});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '项目导入失败');
    await loadProjects(data.project.id);
  } catch (error) {
    showError(error.message);
  } finally {
    $('#uploadBtn').disabled = false;
    $('#uploadBtn').textContent = '＋ 导入 ZIP';
    event.target.value = '';
  }
});

$('#runBtn').addEventListener('click', async () => {
  if ($('#runBtn').disabled) return;
  $('#runBtn').disabled = true;
  $('#runBtn').innerHTML = '<span>◌</span> 正在执行 <small>RUN</small>';
  resetRunView();
  try {
    const response = await fetch('/api/run', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({task:$('#task').value, mode:$('#mode').value, project_id:$('#project').value})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '任务启动失败');
    activeRun = data.run_id;
    poll();
  } catch (error) {
    showError(error.message);
  }
});

$('#exportBtn').addEventListener('click', () => { if (activeRun) window.open('/api/run/' + activeRun + '/export', '_blank'); });
document.addEventListener('keydown', event => { if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') $('#runBtn').click(); });
setInterval(() => $('#clock').textContent = new Date().toLocaleTimeString('zh-CN', {hour12:false}), 1000);
loadProjects().catch(error => showError(error.message));
