'use strict';
const $ = id => document.getElementById(id);
const element = (tag, className, text) => { const el = document.createElement(tag); if (className) el.className = className; if (text !== undefined) el.textContent = text; return el; };
let scenarios = [], runs = [], selected = null, refreshing = false, lastRenderKey = null, lastRecentKey = null;
const assessments = {VERIFIED: ['Revisión verificada','good'], STALE_BUILD: ['Build de otra revisión','warn'], BUILD_FAILED: ['Build fallido','bad'], INCOMPLETE: ['Evidencia incompleta','warn']};
const errorMessages = {FORBIDDEN: 'La política bloqueó el acceso al recurso solicitado.', UNKNOWN_TOOL: 'El modelo propuso una herramienta que no está disponible.', BUDGET_EXCEEDED: 'Se agotó el presupuesto de llamadas.', RUN_DEADLINE_EXCEEDED: 'Se agotó el tiempo activo permitido.', UPSTREAM_TIMEOUT: 'Una llamada excedió su tiempo permitido.', MCP_UNAVAILABLE: 'No pudimos conectar una herramienta. Comprobá que el servidor Jenkins esté preparado.', RUN_BUSY: 'Esta ejecución ya está en curso.', INVALID_PAUSE: 'La pausa tras un fallo requiere el escenario «Jenkins no disponible».', INVALID_TRANSITION: 'Este estado no permite recuperar la ejecución.', TIME_BUDGET_UNKNOWN: 'Esta ejecución antigua no tiene medición de tiempo recuperable.', QUEUE_FULL: 'Hay demasiadas ejecuciones pendientes. Esperá a que terminen.', REQUEST_REJECTED: 'La solicitud no pertenece a esta consola local.'};
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
  try { const config = await api('/config'); scenarios = config.scenarios; if (config.execution_mode === 'worker') { $('environment').textContent = 'Worker independiente'; document.querySelector('.version').textContent = 'M3c'; document.querySelector('.page-footer > span:last-child').textContent = 'Laboratorio educativo · M3c / worker'; } $('scenario').replaceChildren(...scenarios.map(item => { const option = element('option','',item.title); option.value = item.id; return option; })); updateScenario(); await refreshRuns(); setInterval(refreshRuns,1200); } catch(error) { message('No pudimos conectar con la API local. ' + error.message); $('start').disabled = true; }
})();
