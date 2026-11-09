'use strict';
const $ = id => document.getElementById(id);
const element = (tag, className, text) => { const el = document.createElement(tag); if (className) el.className = className; if (text !== undefined) el.textContent = text; return el; };
let scenarios = [], runs = [], selected = null, refreshing = false, lastRenderKey = null, lastRecentKey = null;
let writesInWorker = false, m4Enabled = false, m4Items = [], m4Run = null, m4Proposal = null, m4Busy = false, m4Fetching = false;
const assessments = {VERIFIED: ['Revisión verificada','good'], STALE_BUILD: ['Build de otra revisión','warn'], BUILD_FAILED: ['Build fallido','bad'], INCOMPLETE: ['Evidencia incompleta','warn']};
const errorMessages = {APPROVAL_EXPIRED:'La aprobación venció; necesitás una nueva propuesta.', CONFLICT:'El estado cambió. Actualizá para revisar la versión guardada.', STALE_PRECONDITION:'La revisión cambió; esta autorización no permite el envío.', OPERATION_BUSY:'Hay un envío activo o incierto. Esperá el lease y consultá el estado.', UNSAFE_RECOVERY:'No se permite otro envío. Consultá el efecto con el mismo identificador.', INCOMPLETE_EVIDENCE:'La lectura todavía no tiene evidencia válida para proponer.', DISPATCH_EXPIRED:'El proveedor rechazó el envío por vencimiento.', FORBIDDEN: 'La política bloqueó el acceso al recurso solicitado.', UNKNOWN_TOOL: 'El modelo propuso una herramienta que no está disponible.', BUDGET_EXCEEDED: 'Se agotó el presupuesto de llamadas.', RUN_DEADLINE_EXCEEDED: 'Se agotó el tiempo activo permitido.', UPSTREAM_TIMEOUT: 'Una llamada excedió su tiempo permitido.', MCP_UNAVAILABLE: 'No pudimos conectar una herramienta. Comprobá que el servidor Jenkins esté preparado.', RUN_BUSY: 'Esta ejecución ya está en curso.', INVALID_PAUSE: 'La pausa tras un fallo requiere el escenario «Jenkins no disponible».', INVALID_TRANSITION: 'Este estado no permite recuperar la ejecución.', TIME_BUDGET_UNKNOWN: 'Esta ejecución antigua no tiene medición de tiempo recuperable.', QUEUE_FULL: 'Hay demasiadas ejecuciones pendientes. Esperá a que terminen.', REQUEST_REJECTED: 'La solicitud no pertenece a esta consola local.'};
async function api(path, body) {
  let response;
  try { response = await fetch('/api' + path, body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json','X-Harness-Client':'local-ui'}, body:JSON.stringify(body)}); }
  catch { throw new Error('La app local no está disponible. Volvé a iniciar el servidor.'); }
  const data = await response.json();
  if (!response.ok) throw new Error(errorMessages[data.error] || 'No pudimos completar la solicitud (' + (data.error || response.status) + ').');
  return data;
}
function message(text) { $('message').textContent = text || ''; $('message').hidden = !text; }
function statusOf(run) {
  if (run.job?.status === 'PENDING') return ['Esperando worker','running'];
  if (run.job?.status === 'RUNNING') return run.job.lease_active ? ['Worker en curso','running'] : ['Esperando recuperación','warn'];
  if (run.is_active) return [run.status === 'QUEUED' ? 'En espera' : 'En curso','running'];
  if (run.can_resume) return ['Interrumpida','warn'];
  if (run.status === 'FAILED') return ['Bloqueada / error','bad'];
  if (run.status === 'CANCELLED') return ['Cancelada','neutral'];
  return assessments[run.assessment?.status] || ['Completada','good'];
}
function titleOf(run) { return scenarios.find(item => item.id === scenarioOf(run))?.title || 'Ejecución guardada'; }
function scenarioOf(run) {
  if (run.execution_profile.includes(':injection:job')) return 'injection-job';
  if (run.execution_profile.includes(':injection:shell')) return 'injection-shell';
  if (run.execution_profile.includes(':injection:claim')) return 'injection-claim';
  return run.execution_profile.split(':')[1];
}
function renderRecent() {
  const key = JSON.stringify(runs.map(run => [run.run_id,run.version,run.is_active,run.service_error,run.job?.status,run.job?.fencing_token])) + selected;
  if (key === lastRecentKey) return; lastRecentKey = key;
  $('total-count').textContent = runs.length;
  $('verified-count').textContent = runs.filter(run => run.assessment?.status === 'VERIFIED').length;
  $('review-count').textContent = runs.filter(run => run.status === 'FAILED' || ['STALE_BUILD','BUILD_FAILED','INCOMPLETE'].includes(run.assessment?.status)).length;
  const list = $('recent-runs'); list.replaceChildren();
  if (!runs.length) list.append(element('p','sidebar-empty','Todavía no hay ejecuciones.'));
  for (const run of runs) {
    const [label, tone] = statusOf(run), button = element('button','recent-run' + (selected === run.run_id ? ' active' : ''));
    button.type = 'button'; button.setAttribute('aria-label',titleOf(run) + ': ' + label); button.setAttribute('aria-pressed',String(selected === run.run_id));
    const text = element('span'); text.append(element('strong','',titleOf(run)),element('small','',label + ' · ' + run.run_id.slice(0,8)));
    button.append(element('span','status-dot ' + tone),text); button.addEventListener('click',() => { selected = run.run_id; renderRecent(); refreshDetail(); }); list.append(button);
  }
}
function renderRun(run) {
  const key = JSON.stringify([run.run_id,run.version,run.is_active,run.service_error,run.job?.status,run.job?.fencing_token]);
  if (key === lastRenderKey) return; lastRenderKey = key;
  $('empty-state').hidden = true; $('run-detail').hidden = false;
  const [label,tone] = statusOf(run); $('run-status').textContent = label; $('run-status').className = 'badge ' + tone;
  $('run-id').textContent = run.run_id; $('run-date').textContent = new Date(run.created_at).toLocaleString('es');
  $('active-time').textContent = run.timing_version === 1 ? run.active_seconds.toFixed(2) + ' s' : 'Sin medición';
  $('model-count').textContent = run.model_calls; $('tool-count').textContent = run.tool_calls;
  const journey = $('tool-journey'); journey.replaceChildren();
  ['jira_get_issue','github_get_pull_request','jenkins_get_build'].forEach((name,index) => {
    const done = run.results.some(result => result.tool_name === name), missing = run.tool_failures.some(failure => failure.call.name === name), pending = run.pending_call?.name === name;
    const step = element('div','tool-step ' + (done ? 'done' : missing ? 'missing' : pending ? 'pending' : ''));
    step.append(element('small','','0' + (index + 1)),element('strong','',['Jira','GitHub','Jenkins'][index]),element('span','',done ? '✓ Evidencia guardada' : missing ? '◉ No disponible' : pending ? (run.is_active ? 'Consultando…' : 'Intento pendiente') : 'Sin consultar'));
    journey.append(step);
  });
  $('assessment').hidden = !run.assessment;
  if (run.assessment) {
    const a = run.assessment, [title, type] = assessments[a.status] || [a.status,'neutral'];
    $('assessment').className = 'assessment ' + type; $('assessment-title').textContent = title;
    $('assessment-revisions').textContent = 'PR: ' + (a.pr_revision || 'Sin evidencia') + '  /  Build: ' + (a.build_revision || 'Sin evidencia') + (a.build_successful === null ? ' · Resultado de CI desconocido' : '');
  }
  const code = run.outcome_code || run.service_error; $('error-detail').hidden = !code;
  $('error-detail').textContent = code ? (errorMessages[code] || 'El run se detuvo sin completar el informe.') + ' (' + code + ')' : '';
  $('answer-section').hidden = !run.answer; $('answer').textContent = run.answer || '';
  const budget = $('budget'); budget.replaceChildren();
  for (const [title,value,limit] of [['Tiempo activo',run.active_seconds,run.limits_snapshot.active_timeout],['Intentos de herramienta',run.tool_calls,run.limits_snapshot.tool_calls]]) {
    const group = element('div'), header = element('div','budget-label'), track = element('div','budget-track'), fill = element('div','budget-fill');
    header.append(element('span','',title),element('span','',limit ? (title === 'Tiempo activo' ? value.toFixed(2) + ' / ' + limit + ' s' : value + ' / ' + limit) : 'Sin snapshot'));
    fill.style.width = Math.min(100,limit ? value / limit * 100 : 0) + '%'; track.append(fill); group.append(header,track); budget.append(group);
  }
  $('worker-info').hidden = !run.job;
  $('worker-info').textContent = run.job ? 'Ejecución independiente · Intentos del worker: ' + run.job.attempts + ' · Estado del trabajo: ' + run.job.status : '';
  $('resume-section').hidden = !run.can_resume; $('resume').disabled = run.is_active;
  const evidence = $('evidence-list'); evidence.replaceChildren();
  for (const result of run.results) {
    const detail = element('details','evidence-item'); detail.append(element('summary','',result.source_id),element('pre','',JSON.stringify(result.data,null,2))); evidence.append(detail);
  }
  for (const failure of run.tool_failures) {
    const detail = element('details','evidence-item'); detail.append(element('summary','','Evidencia ausente · ' + failure.code),element('pre','',JSON.stringify(failure,null,2))); evidence.append(detail);
  }
  if (!run.results.length && !run.tool_failures.length) evidence.append(element('p','evidence-empty','La evidencia aparecerá al confirmar la primera consulta.'));
  if (m4Enabled) refreshM4();
}
async function refreshDetail() {
  const id = selected; if (!id) return;
  try { const run = await api('/runs/' + encodeURIComponent(id)); if (selected === id) renderRun(run); } catch (error) { message(error.message); }
}
async function refreshRuns() {
  if (refreshing) return; refreshing = true;
  try { runs = (await api('/runs')).runs; if (!selected && runs.length) selected = runs[0].run_id; renderRecent(); const current = runs.find(run => run.run_id === selected); if (current) renderRun(current); else await refreshDetail(); } catch(error) { message(error.message); } finally { refreshing = false; }
}
function updateScenario() {
  const chosen = scenarios.find(item => item.id === $('scenario').value); $('scenario-description').textContent = chosen?.description || '';
  for (const option of $('pause').options) option.disabled = (option.value === 'failure' && $('scenario').value !== 'unavailable') || (['injection-job','injection-shell'].includes($('scenario').value) && ['github','jenkins'].includes(option.value));
  if ($('pause').selectedOptions[0]?.disabled) $('pause').value = 'none';
}
$('run-form').addEventListener('submit',async event => {
  event.preventDefault(); message(''); $('start').disabled = true;
  try { const run = await api('/runs',{scenario:$('scenario').value,pause_at:$('pause').value}); selected = run.run_id; renderRun(run); await refreshRuns(); } catch(error) { message(error.message); } finally { $('start').disabled = false; }
});
$('resume').addEventListener('click',async () => {
  $('resume').disabled = true; message('');
  try { const run = await api('/runs/' + encodeURIComponent(selected) + '/resume',{}); renderRun(run); await refreshRuns(); } catch(error) { message(error.message); } finally { $('resume').disabled = false; }
});
$('scenario').addEventListener('change',updateScenario); $('refresh').addEventListener('click',refreshRuns);
(async () => {
  try { const config = await api('/config'); m4Enabled = config.approvals_demo === true; writesInWorker = config.write_worker === true; $('approval-panel').hidden = !m4Enabled; $('approval-nav').hidden = !m4Enabled; $('approval-unavailable').hidden = m4Enabled; scenarios = config.scenarios; if (config.execution_mode === 'worker') { $('environment').textContent = 'Worker independiente'; document.querySelector('.version').textContent = 'M3c'; document.querySelector('.page-footer > span:last-child').textContent = 'Laboratorio educativo · M3c / worker'; } $('scenario').replaceChildren(...scenarios.map(item => { const option = element('option','',item.title); option.value = item.id; return option; })); if (m4Enabled) { document.querySelector('.version').textContent = writesInWorker ? 'M4f' : 'M4e'; $('environment').textContent = 'M4 local · worker independiente'; } updateScenario(); await refreshRuns(); setInterval(refreshRuns,1200); if (m4Enabled) setInterval(refreshM4,3000); } catch(error) { message('No pudimos conectar con la API local. ' + error.message); $('start').disabled = true; }
})();

const approvalLabels = {PENDING_APPROVAL:'Esperando aprobación',APPROVED:'Aprobada',REJECTED:'Rechazada',EXPIRED:'Vencida',INVALIDATED:'Revisión invalidada',CONSUMED:'Aprobación consumida',PREPARED:'Operación preparada',DISPATCHING:'Envío pendiente de confirmar',UNKNOWN:'Resultado incierto',SUCCEEDED:'Solicitud aceptada',NOT_APPLIED:'No aplicada'};
function currentProposal() { return m4Items.find(item => item.proposal.proposal_id === m4Proposal); }
function renderM4() {
  if (!m4Enabled) return;
  const currentRun = runs.find(run => run.run_id === selected);
  $('proposal-create').disabled = !selected || m4Busy || m4Run !== selected || m4Items.length > 0 || currentRun?.status !== 'COMPLETED' || !currentRun?.assessment;
  $('approval-help').textContent = currentRun?.status === 'COMPLETED' ? 'Proponé pruebas para la revisión del PR confirmada en esta lectura. Para otra intención de este ejercicio, creá una lectura nueva.' : 'La lectura debe completar y guardar evidencia antes de proponer.';
  const item = currentProposal(), proposal = item?.proposal, operation = item?.operation, job = item?.write_job;
  $('proposal-content').hidden = !proposal;
  $('approval-refresh').disabled = m4Busy;
  if (!proposal) return;
  const selector = $('proposal-select');
  const choices = JSON.stringify(m4Items.map(row => [row.proposal.proposal_id,row.proposal.effective_status]));
  if (selector.dataset.choices !== choices) {
    selector.replaceChildren(...m4Items.map(row => { const option = element('option','',row.proposal.proposal_id.slice(0,8) + ' · ' + (approvalLabels[row.proposal.effective_status] || row.proposal.effective_status)); option.value = row.proposal.proposal_id; return option; }));
    selector.dataset.choices = choices;
  }
  selector.value = m4Proposal; selector.disabled = m4Busy;
  $('proposal-status').textContent = approvalLabels[proposal.effective_status] || proposal.effective_status;
  const payload = proposal.payload, facts = $('proposal-facts'); facts.replaceChildren();
  for (const [label,value] of [['Operador',proposal.requested_by],['Job',payload.arguments.job],['Repositorio',payload.arguments.repository],['Revisión aprobada',payload.arguments.revision],['Vence',new Date(proposal.expires_at).toLocaleString('es')],['Huella del contenido',proposal.payload_hash],['Decisión registrada por',proposal.decided_by || 'Todavía sin decisión']]) facts.append(element('dt','',label),element('dd','',value));
  $('proposal-payload').textContent = JSON.stringify(payload,null,2);
  const pending = proposal.effective_status === 'PENDING_APPROVAL';
  $('proposal-approve').disabled = m4Busy || !pending; $('proposal-reject').disabled = m4Busy || !pending;
  $('operation-prepare').disabled = m4Busy || proposal.effective_status !== 'APPROVED' || !!operation;
  $('operation-content').hidden = !operation;
  if (!operation) return;
  $('operation-status').textContent = approvalLabels[operation.status] || operation.status;
  $('operation-id').textContent = operation.operation_id;
  $('operation-dispatch').disabled = m4Busy || operation.status !== 'PREPARED' || !!job;
  $('operation-dispatch').textContent = writesInWorker ? 'Encolar solicitud simulada' : 'Solicitar pruebas simuladas';
  $('write-job').hidden = !job;
  $('write-job').textContent = job ? 'Trabajo de escritura: ' + ({PENDING:'Esperando worker',RUNNING:'Worker en curso',DONE:'Finalizado',NEEDS_REVIEW:'Requiere revisión'}[job.status] || job.status) + ' · Intentos de reclamo: ' + job.attempts + (job.last_error ? ' · ' + job.last_error : '') : '';
  $('operation-interrupt').disabled = m4Busy || operation.status !== 'PREPARED' || !!job;
  $('operation-reconcile').disabled = m4Busy || !['UNKNOWN','DISPATCHING'].includes(operation.status);
  $('operation-note').textContent = operation.status === 'DISPATCHING' ? 'Esperá aproximadamente once segundos desde el envío antes de consultar. No vuelvas a enviar.' : operation.status === 'UNKNOWN' ? 'Consultá el efecto con el mismo identificador. Una búsqueda vacía no autoriza otro envío.' : operation.status === 'SUCCEEDED' ? 'El proveedor confirmó aceptación. La ejecución del build y su resultado no se comprueban aquí.' : operation.outcome_code ? 'Motivo: ' + operation.outcome_code : 'El envío requiere esta operación y su aprobación vigente.';
  const resolution = item.resolution;
  $('resolution-section').hidden = operation.status !== 'UNKNOWN' && !resolution;
  $('resolution-prepare').disabled = m4Busy || operation.status !== 'UNKNOWN' || !!resolution;
  $('resolution-detail').hidden = !resolution;
  if (resolution) {
    $('resolution-summary').textContent = (resolution.payload.conclusion === 'NOT_APPLIED' ? 'Conclusión del cierre: no hay solicitud y este ID no aceptará un envío posterior.' : 'Conclusión del cierre: la solicitud ya fue aceptada.') + ' Estado de revisión: ' + resolution.effective_status + (resolution.reviewed_by ? ' · ' + resolution.reviewed_by : '');
    $('resolution-evidence').textContent = JSON.stringify(resolution.payload.evidence,null,2);
    $('resolution-hash').textContent = 'Huella de esta revisión: ' + resolution.payload_hash;
    $('resolution-confirm').disabled = m4Busy || operation.status !== 'UNKNOWN' || resolution.effective_status !== 'PENDING_REVIEW';
  }
  $('operation-events').replaceChildren(...operation.events.map(event => element('li','',(approvalLabels[event.status] || event.status) + ' · ' + new Date(event.occurred_at).toLocaleTimeString('es') + ' · ' + event.actor_id)));
}
async function refreshM4() {
  if (!m4Enabled || m4Busy || m4Fetching || !selected) return;
  m4Fetching = true; const id = selected;
  if (m4Run !== id) { m4Run = null; m4Items = []; m4Proposal = null; renderM4(); }
  try {
    const data = await api('/runs/' + encodeURIComponent(id) + '/proposals');
    if (selected !== id || m4Busy) return;
    m4Run = id; m4Items = data.items;
    if (!m4Items.some(item => item.proposal.proposal_id === m4Proposal)) m4Proposal = m4Items[0]?.proposal.proposal_id || null;
    renderM4();
  } catch (error) { $('approval-message').textContent = error.message; }
  finally { m4Fetching = false; }
}
function proposalKey(runId) {
  try {
    const name = 'harness-proposal-key:' + runId;
    let key = localStorage.getItem(name);
    if (!key) { key = crypto.randomUUID(); localStorage.setItem(name,key); }
    return key;
  } catch { throw new Error('El navegador necesita guardar la intención local antes de crear una propuesta.'); }
}
async function mutateM4(action) {
  if (!selected || m4Busy || m4Run !== selected) return;
  const item = currentProposal(), id = selected; m4Busy = true; $('approval-message').textContent = ''; renderM4();
  try {
    if (action === 'create') {
      const data = await api('/runs/' + encodeURIComponent(id) + '/proposals',{request_key:proposalKey(id)});
      if (selected === id) m4Proposal = data.proposal.proposal_id;
    } else if (action === 'approve' || action === 'reject') {
      const p = item.proposal;
      await api('/proposals/' + p.proposal_id + '/decision',{payload_hash:p.payload_hash,expected_version:p.version,decision:action === 'approve' ? 'APPROVED' : 'REJECTED'});
    } else if (action === 'prepare') await api('/proposals/' + item.proposal.proposal_id + '/operation',{});
    else if (action === 'dispatch') { await api('/operations/' + item.operation.operation_id + '/dispatch',{interrupt:$('operation-interrupt').value || null}); $('operation-interrupt').value = ''; }
    else if (action === 'resolution-prepare') await api('/operations/' + item.operation.operation_id + '/resolution',{});
    else if (action === 'resolution-confirm') await api('/operations/' + item.operation.operation_id + '/resolution/confirm',{payload_hash:item.resolution.payload_hash,expected_version:item.resolution.version});
    else await api('/operations/' + item.operation.operation_id + '/reconcile',{});
    if (selected === id) $('approval-message').textContent = 'Estado actualizado. Revisá la propuesta y el historial de la operación.';
  } catch (error) { $('approval-message').textContent = error.message + ' Actualizá para consultar lo que quedó guardado; no cambies de intención para repetir un efecto incierto.'; }
  finally { m4Busy = false; await refreshM4(); renderM4(); }
}
$('proposal-select').addEventListener('change',() => { m4Proposal = $('proposal-select').value; renderM4(); });
$('approval-refresh').addEventListener('click',refreshM4);
for (const [id,action] of [['proposal-create','create'],['proposal-approve','approve'],['proposal-reject','reject'],['operation-prepare','prepare'],['operation-dispatch','dispatch'],['operation-reconcile','reconcile'],['resolution-prepare','resolution-prepare'],['resolution-confirm','resolution-confirm']]) $(id).addEventListener('click',() => mutateM4(action));
