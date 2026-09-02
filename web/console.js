const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const stateLabels = {IDLE:'等待', QUEUED:'排队中', UNDERSTAND:'理解任务', PLAN:'制定计划', EXECUTE:'执行操作', VERIFY:'验证结果', WAITING_APPROVAL:'等待授权', COMPLETED:'已完成', FAILED:'已中断', CANCELLED:'已取消'};
const evidenceKeys = ['baseline_failure_captured','minimal_patch_recorded','regression_tests_passed','workspace_boundary_respected'];
const phaseLabels = {understand:'理解任务', plan:'制定计划', execute:'执行操作', baseline:'修改前检查', patch:'应用补丁', regression:'回归验证', verify:'验证结果', gate:'验收门禁', lifecycle:'运行控制', complete:'完成', context:'上下文'};
const evidenceLabels = {baseline_failure:'基线失败', baseline_status:'基线状态', minimal_patch:'最小补丁', regression_test:'回归测试', acceptance_gate:'验收门禁', permission_request:'等待授权', permission_decision:'授权决定', cancellation:'取消运行', task_input:'任务输入', run_initialized:'运行初始化', state_transition:'状态变化', failure:'运行失败', completion:'完成记录', context_compaction:'上下文整理', baseline_hypothesis:'基线假设'};
const taskTypeLabels = {repair:'Bug 修复', feature:'功能新增', refactor:'结构重构', change:'一般变更'};
const modeLabels = {mock:'稳定演示', live:'实时模型'};
const eventKindLabels = {task:'任务', system:'系统', state:'状态', hypothesis:'基线假设', decision:'决策', approval:'授权', tool_result:'工具结果', error:'错误', cancel:'取消', guard:'验收拦截', finish:'完成', context:'上下文'};
let activeRun = null; let activeData = null; let projects = new Map(); let renderedEvents = 0; let lastDiff = ''; let replayTimer = null;

function setState(state) {
  const pill = $('#statePill'); const label = stateLabels[state] || state || 'IDLE';
  pill.textContent = label; pill.className = 'state-pill ' + (state === 'COMPLETED' ? 'done' : ['FAILED', 'CANCELLED'].includes(state) ? 'failed' : state === 'IDLE' ? 'idle' : 'running');
  const order = ['UNDERSTAND','PLAN','EXECUTE','VERIFY']; const index = order.indexOf(state);
  document.querySelectorAll('#stateMap span').forEach((node, i) => node.classList.toggle('active', i <= index || state === 'COMPLETED'));
  $('#phaseLabel').textContent = label;
}

function eventCard(event) {
  const payload = event.payload || {}; const tags = [];
  if (event.phase) tags.push(`<span class="tag">${esc(phaseLabels[event.phase] || event.phase)}</span>`);
  if (event.tool) tags.push(`<span class="tag">${esc(event.tool)}</span>`);
  if (event.evidence_type) tags.push(`<span class="tag evidence">${esc(evidenceLabels[event.evidence_type] || event.evidence_type)}</span>`);
  if (event.verification_status && event.verification_status !== 'pending') tags.push(`<span class="tag ${event.verification_status === 'passed' ? 'ok' : 'bad'}">${event.verification_status === 'passed' ? '已通过' : '未通过'}</span>`);
  if (event.parent_event_id) tags.push(`<span class="tag">关联事件 #${event.parent_event_id}</span>`);
  const detail = event.detail || (payload.output ? payload.output : '');
  return `<article class="event-card ${esc(event.kind)}"><div class="event-top"><strong>#${event.id} · ${esc(event.title)}</strong><time>${esc(event.time || event.timestamp || '')}</time></div><p class="event-detail">${esc(detail)}</p><div class="event-meta">${tags.join('')}</div></article>`;
}

function renderEvents(events, reset = false) {
  if (reset) { renderedEvents = 0; $('#events').innerHTML = ''; }
  const fresh = events.slice(renderedEvents); if (!fresh.length) return;
  if (renderedEvents === 0) $('#events').innerHTML = '';
  $('#events').insertAdjacentHTML('beforeend', fresh.map(eventCard).join('')); renderedEvents = events.length;
  $('#events').scrollTop = $('#events').scrollHeight;
}

function renderDiff(diffs = []) {
  const raw = diffs.join('\n'); if (raw === lastDiff) return; lastDiff = raw;
  $('#diffMeta').textContent = raw ? `${diffs.length} 处变更` : '暂无变更';
  if (!raw) { $('#diff').innerHTML = '<span class="muted">// 运行后显示真实文件变更</span>'; return; }
  $('#diff').innerHTML = raw.split('\n').map((line) => { const safe = esc(line); if (line.startsWith('+') && !line.startsWith('+++')) return `<span class="diff-add">${safe}</span>`; if (line.startsWith('-') && !line.startsWith('---')) return `<span class="diff-del">${safe}</span>`; return safe; }).join('');
}

function renderVerification(events = []) {
  const commandEvents = events.filter((event) => event.tool === 'run_command' && event.payload);
  const last = commandEvents[commandEvents.length - 1]; if (!last) return;
  const payload = last.payload; const ok = payload.ok === true;
  $('#verified').textContent = `${ok ? '✓ 通过' : '× 未通过'} · ${phaseLabels[payload.phase || last.phase] || '命令'}`;
  $('#verified').className = 'verify-badge ' + (ok ? 'ok' : 'bad');
  $('#verifyBody').innerHTML = `<p class="${ok ? 'success' : 'failure'}">${ok ? '✓' : '×'} ${esc(payload.command || '验证命令')}</p><p>${esc(payload.output || payload.error || '暂无输出')}</p>`;
}

function renderContract(contract = {}, evidence = {}) {
  const checks = contract.checks || evidenceKeys;
  const definitions = contract.gate_definitions || {};
  $('#contract').innerHTML = checks.map((key) => {
    const gate = definitions[key] || {};
    const passed = Boolean(evidence[key]);
    const code = key === 'baseline_failure_captured' ? (gate.policy === 'establish_green_baseline' ? '基线已确认' : '修改前检查') : key === 'minimal_patch_recorded' ? '补丁记录' : key === 'regression_tests_passed' ? '回归测试' : '边界检查';
    return `<div class="${passed ? 'checked' : ''}"><i>${passed ? '✓' : '○'}</i><span>${esc(gate.label || key)}</span><small>${esc(code)}</small></div>`;
  }).join('');
}

function previewTaskType(task, requested) {
  if (['repair', 'feature', 'refactor', 'change'].includes(requested)) return requested;
  const text = String(task || '').toLowerCase();
  if (['bug', 'fix', 'debug', '故障', '错误', '缺陷', '修复'].some((token) => text.includes(token))) return 'repair';
  if (['refactor', '重构', '重写', '整理结构', '迁移'].some((token) => text.includes(token))) return 'refactor';
  if (['feature', 'implement', 'add ', '新增', '增加', '添加', '实现', '支持'].some((token) => text.includes(token))) return 'feature';
  return 'change';
}

function previewContract() {
  if (activeData?.finished === false) return;
  const type = previewTaskType($('#task').value, $('#taskType').value);
  const repair = type === 'repair';
  renderContract({
    checks: evidenceKeys,
    gate_definitions: {
      baseline_failure_captured: {label: repair ? '复现基线故障' : '记录基线状态', policy: repair ? 'observe_failure' : 'establish_green_baseline'},
      minimal_patch_recorded: {label: '记录最小补丁'},
      regression_tests_passed: {label: '回归测试通过'},
      workspace_boundary_respected: {label: '未越过工作区边界'},
    },
  });
}

function renderApproval(data) {
  const panel = $('#approvalPanel'); const pending = data.pending_approval;
  if (!pending || data.finished || data.state !== 'WAITING_APPROVAL') {
    panel.classList.add('hidden');
    return;
  }
  panel.classList.remove('hidden');
  $('#approvalSummary').innerHTML = `<b>${esc(pending.tool || '工具')}</b><span>${esc(phaseLabels[pending.phase] || pending.phase || '策略检查')} · 等待执行授权</span><pre>${esc(JSON.stringify(pending.arguments || {}, null, 2))}</pre>`;
}

async function resolveApproval(decision) {
  if (!activeRun) return;
  const response = await fetch('/api/run/' + encodeURIComponent(activeRun) + '/approve', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({decision}),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '审批操作失败');
  update(data);
}

async function cancelRun() {
  if (!activeRun) return;
  const response = await fetch('/api/run/' + encodeURIComponent(activeRun) + '/cancel', {method: 'POST'});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '取消失败');
  update(data);
}

function update(data) {
  activeData = data; const events = data.events || []; setState(data.state);
  renderEvents(events); renderDiff(data.diffs || []); renderVerification(events); renderContract(data.contract || {}, data.evidence || {}); renderApproval(data);
  const decisions = events.filter((event) => event.kind === 'decision').length; const files = new Set(events.flatMap((event) => event.affected_files || [])); const score = data.trust_score || 0;
  const taskText = data.task || activeData?.task || '当前运行';
  $('#eventCount').textContent = `${events.length} 条事件`; $('#runCaption').textContent = data.project?.name ? `${data.project.name} · ${taskText}` : taskText; $('#runIdCaption').textContent = `运行编号 ${data.id || activeRun || '————'}`;
  $('#progress').textContent = String(score).padStart(2,'0'); $('#contractScore').textContent = `${String(score).padStart(2,'0')} / 100`; $('#metricIter').textContent = String(decisions).padStart(2,'0'); $('#metricTools').textContent = String(data.tool_call_count ?? decisions).padStart(2,'0'); $('#metricFiles').textContent = String(files.size).padStart(2,'0');
  if (data.contract?.task_type) $('#taskType').value = data.contract.task_type;
  $('#healthText').textContent = data.state === 'COMPLETED' ? '验收通过' : data.state === 'CANCELLED' ? '已取消' : data.state === 'FAILED' ? '运行中断' : data.state === 'WAITING_APPROVAL' ? '等待人工授权' : data.state === 'VERIFY' ? '等待证据闭合' : '正在执行';
  $('#healthSub').textContent = data.summary || (data.state === 'COMPLETED' ? '所有验收门禁均由真实事件支持' : data.state === 'WAITING_APPROVAL' ? '高风险工具在执行前需要人工确认' : '决策、工具和验证结果正在写入账本');
  $('#exportBtn').disabled = !activeRun; $('#replayBtn').disabled = !events.length; $('#cancelBtn').disabled = Boolean(data.finished);
  $('#approveBtn').disabled = !data.pending_approval || Boolean(data.finished); $('#rejectBtn').disabled = !data.pending_approval || Boolean(data.finished);
  if (data.finished) { $('#runBtn').disabled = false; $('#runBtn').innerHTML = '<span>↻</span> 再次运行 <kbd>Ctrl ↵</kbd>'; loadHistory(); }
}

async function loadRun(runId) { const response = await fetch('/api/run/' + encodeURIComponent(runId), {cache:'no-store'}); if (!response.ok) throw new Error('无法读取运行记录'); activeRun = runId; renderedEvents = 0; lastDiff = ''; update(await response.json()); }
async function poll() { if (!activeRun) return; try { const response = await fetch('/api/run/' + encodeURIComponent(activeRun), {cache:'no-store'}); const data = await response.json(); if (!response.ok) throw new Error(data.error || '运行状态读取失败'); update(data); if (!data.finished) window.setTimeout(poll, 420); } catch (error) { showError(error.message); } }
function showError(message) { $('#healthText').textContent = '请求失败'; $('#healthSub').textContent = message; $('#runBtn').disabled = false; $('#cancelBtn').disabled = !activeRun || Boolean(activeData?.finished); $('#runBtn').innerHTML = '<span>↻</span> 重试 <kbd>Ctrl ↵</kbd>'; }

function formatRunTime(value) {
  if (!value) return '时间未知';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString('zh-CN', {year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'});
}
function displayHistoryTask(task) {
  const value = String(task || '未命名任务');
  // 早期记录曾在错误编码环境中保存为问号；仅修复展示，不改动原始账本。
  if (value.includes('?') && /todo/i.test(value) && /bug/i.test(value)) return 'Todo 删除边界 Bug 修复任务（历史记录）';
  return value;
}
function projectDescription(project) { const profile = project.profile || {}; return `${(profile.languages || ['未知语言']).join(' / ')} · ${profile.files ?? project.file_count ?? 0} 个文件 · ${(profile.suggested_tests || []).join(' / ')}`; }
function projectDisplayName(project) { return project.id === 'demo' ? '内置 Todo 边界示例' : project.name; }
async function loadProjects(selected = null) { const response = await fetch('/api/projects', {cache:'no-store'}); const data = await response.json(); projects = new Map(data.projects.map((project) => [project.id, project])); $('#project').innerHTML = data.projects.map((project) => `<option value="${esc(project.id)}">${esc(projectDisplayName(project))}${project.source === 'uploaded' ? ' · 已导入' : ''}</option>`).join(''); if (selected) $('#project').value = selected; updateProjectMeta(); }
function updateProjectMeta() { const project = projects.get($('#project').value); if (!project) return; $('#projectMeta').textContent = project.source === 'uploaded' ? `已导入 · ${projectDescription(project)}` : `内置示例 · ${projectDescription(project)}`; if (project.source === 'uploaded') { $('#mode').value = 'live'; $('#task').value = '请分析这个项目，定位并修复问题，运行可用测试，并给出可审计的修改证据。'; } }
async function loadHistory() { try { const response = await fetch('/api/runs', {cache:'no-store'}); const data = await response.json(); const list = data.runs || []; $('#history').innerHTML = list.length ? list.map((run) => `<div class="history-item ${run.run_id === activeRun ? 'active' : ''}" data-run="${esc(run.run_id)}"><div class="history-item-top"><b>${esc(displayHistoryTask(run.task))}</b><span class="history-score">${String(run.trust_score).padStart(2,'0')}</span></div><div class="history-item-meta"><small>${esc(run.run_id)} · ${esc(stateLabels[run.state] || run.state)} · ${esc(taskTypeLabels[run.task_type] || run.task_type || '一般变更')} · ${esc(modeLabels[run.mode] || run.mode)}</small><time datetime="${esc(run.created_at || '')}">${esc(formatRunTime(run.created_at))}</time></div></div>`).join('') : '<p class="empty-small">尚无已保存的运行记录</p>'; document.querySelectorAll('[data-run]').forEach((node) => node.addEventListener('click', () => loadRun(node.dataset.run).catch((error) => showError(error.message)))); } catch (error) { $('#history').innerHTML = `<p class="empty-small">历史读取失败：${esc(error.message)}</p>`; } }

function openReplay() { if (!activeData?.events?.length) return; $('#replayPanel').classList.remove('hidden'); $('#replayTrack').innerHTML = activeData.events.map((event) => `<div class="replay-step"><b>#${event.id} · ${esc(phaseLabels[event.phase] || eventKindLabels[event.kind] || event.kind || '')}</b><span>${esc(event.title)}</span><small>${esc(event.tool || '系统')}</small></div>`).join(''); }
function playReplay() { const nodes = [...document.querySelectorAll('.replay-step')]; if (!nodes.length) return; window.clearInterval(replayTimer); let index = 0; $('#replayStatus').textContent = '正在按持久化事件顺序回放……'; replayTimer = window.setInterval(() => { nodes.forEach((node, i) => node.classList.toggle('active', i === index)); index += 1; if (index >= nodes.length) { window.clearInterval(replayTimer); $('#replayStatus').textContent = '回放完成 · 只读模式'; } }, 380); }

$('#project').addEventListener('change', updateProjectMeta); $('#task').addEventListener('input', previewContract); $('#taskType').addEventListener('change', previewContract); $('#refreshHistory').addEventListener('click', loadHistory); $('#historyBtn').addEventListener('click', () => $('#history').scrollIntoView({behavior:'smooth'})); $('#closeReplay').addEventListener('click', () => $('#replayPanel').classList.add('hidden')); $('#replayBtn').addEventListener('click', openReplay); $('#replayPlay').addEventListener('click', playReplay); $('#exportBtn').addEventListener('click', () => activeRun && window.open('/api/run/' + encodeURIComponent(activeRun) + '/export', '_blank')); $('#uploadBtn').addEventListener('click', () => $('#projectFile').click()); $('#approveBtn').addEventListener('click', () => resolveApproval('approve').catch((error) => showError(error.message))); $('#rejectBtn').addEventListener('click', () => resolveApproval('reject').catch((error) => showError(error.message))); $('#cancelBtn').addEventListener('click', () => cancelRun().catch((error) => showError(error.message)));
$('#contractToggle').addEventListener('click', () => { const panel = $('.contract-panel'); const collapsed = panel.classList.toggle('collapsed'); $('#contractToggle').textContent = collapsed ? '展开' : '收起'; $('#contractToggle').setAttribute('aria-expanded', String(!collapsed)); panel.querySelector('.contract-hint').textContent = collapsed ? '点击展开，查看完成条件' : '完成前必须满足的条件'; });
$('#projectFile').addEventListener('change', async (event) => { const file = event.target.files[0]; if (!file) return; $('#uploadBtn').disabled = true; $('#uploadBtn').textContent = '上传中'; try { const form = new FormData(); form.append('project', file); const response = await fetch('/api/projects/import', {method:'POST', body:form}); const data = await response.json(); if (!response.ok) throw new Error(data.error || '项目导入失败'); await loadProjects(data.project.id); } catch (error) { showError(error.message); } finally { $('#uploadBtn').disabled = false; $('#uploadBtn').textContent = '＋ ZIP'; event.target.value = ''; } });
$('#runBtn').addEventListener('click', async () => { if ($('#runBtn').disabled) return; $('#runBtn').disabled = true; $('#cancelBtn').disabled = false; $('#runBtn').innerHTML = '<span>◌</span> 正在执行 <kbd>RUN</kbd>'; $('#events').innerHTML = '<div class="empty-state"><span>◌</span><b>正在建立隔离副本</b><p>事件流即将开始。</p></div>'; renderedEvents = 0; lastDiff = ''; try { const response = await fetch('/api/run', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({task:$('#task').value, task_type:$('#taskType').value, mode:$('#mode').value, project_id:$('#project').value, approval_mode:$('#approvalMode').value})}); const data = await response.json(); if (!response.ok) throw new Error(data.error || '任务启动失败'); activeRun = data.run_id; await poll(); } catch (error) { showError(error.message); } });
document.addEventListener('keydown', (event) => { if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') $('#runBtn').click(); }); window.setInterval(() => { $('#clock').textContent = new Date().toLocaleTimeString('zh-CN', {hour12:false}); }, 1000); previewContract(); loadProjects().then(loadHistory).catch((error) => showError(error.message));
