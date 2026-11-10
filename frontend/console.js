import {api,text,row,refresh as refreshKnowledge,message,getIdentity,disconnect} from './knowledge.js';
const $=id=>document.getElementById(id);
const names={runs:'Ejecuciones',knowledge:'Conocimiento',approvals:'Aprobaciones',observability:'Observabilidad',config:'Configuración'};
let active='runs', selectedRun=null, selectedProposal=null, runs=[], config=null;
let busy=false, refreshing=false, sessionVersion=0, runStamp=null, proposalStamp=null, runListStamp=null, approvalListStamp=null;
const proposalKeys=new Map();
const has=role=>getIdentity()?.roles.includes(role);
const canRead=()=>has('reader')||has('operator');
const canReview=()=>has('operator')||has('approver');
function permitted(view){return ['runs','knowledge'].includes(view)?canRead():view==='approvals'?canReview():!!getIdentity();}
function clearDetails(){for(const id of ['run-list','run-detail','approval-list','approval-detail','config-values'])$(id).replaceChildren();runStamp=null;proposalStamp=null;runListStamp=null;approvalListStamp=null;}
function show(view){
  if(!names[view] || (getIdentity()&&!permitted(view)))view=canRead()?'runs':canReview()?'approvals':'observability';
  active=view;$('section-title').textContent=names[view];
  if(getIdentity())message(names[view]+'. Sesión compartida; permisos verificados por la API.');
  for(const section of document.querySelectorAll('[data-page]'))section.hidden=section.dataset.page!==view;
  for(const button of document.querySelectorAll('[data-view]')){
    button.hidden=!!getIdentity()&&!permitted(button.dataset.view);
    if(button.dataset.view===view)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');
  }
  history.replaceState(null,'','#'+view);
  refresh().catch(error=>message(error.message,true));
}
for(const button of document.querySelectorAll('[data-view]'))button.addEventListener('click',()=>show(button.dataset.view));
function button(label,handler){const el=text('button',label);el.type='button';el.disabled=busy;el.addEventListener('click',handler);return el;}
async function action(fn){
  if(busy)return;busy=true;$('launch').disabled=true;document.querySelectorAll('[data-action]').forEach(el=>el.disabled=true);
  try{await fn();runStamp=null;proposalStamp=null;await refresh();}
  catch(error){message(error.message,true);}
  finally{busy=false;$('launch').disabled=!canRead();runStamp=null;proposalStamp=null;await refresh().catch(error=>message(error.message,true));}
}
function mutation(label,handler){const el=button(label,()=>action(handler));el.dataset.action='true';return el;}
function timestamp(value){return value?new Date(value).toLocaleString('es'):'—';}
function runPath(run){return run.kind==='rag'?'knowledge/agent/runs/'+run.run_id:'runs/'+run.run_id;}
function renderRuns(items){
  const stamp=JSON.stringify([items,selectedRun?.run_id,busy]);if(stamp===runListStamp)return;runListStamp=stamp;
  const root=$('run-list');root.replaceChildren();
  if(!items.length)root.append(text('p','Todavía no hay ejecuciones tuyas.','hint'));
  for(const item of items){
    const el=row(item.kind==='rag'?'DevOps + RAG':'Lectura DevOps',item.status);
    el.append(text('p',timestamp(item.created_at)+' · '+item.run_id.slice(0,8),'metadata'));
    el.append(button(selectedRun?.run_id===item.run_id?'Seleccionada':'Ver evidencia',()=>{selectedRun=item;runStamp=null;$('run-detail').replaceChildren();refresh().catch(error=>message(error.message,true));}));
    root.append(el);
  }
}
function renderRun(run,summary){
  const stamp=JSON.stringify([run,summary.trace_id,busy]);if(stamp===runStamp)return;runStamp=stamp;
  const root=$('run-detail');root.replaceChildren();
  const el=row(summary.kind==='rag'?'DevOps + conocimiento':'Lectura DevOps',run.status);
  el.append(text('p',run.run_id,'metadata'));
  const facts=text('div','','facts');
  for(const [label,value] of [['Decisiones',run.model_calls+' / '+run.limits_snapshot.model_calls],['Herramientas',run.tool_calls+' / '+run.limits_snapshot.tool_calls],['Tiempo activo',Number(run.active_seconds).toFixed(2)+' s']]){
    const fact=text('div',label);fact.append(text('strong',value));facts.append(fact);
  }
  el.append(facts);
  if(run.job)el.append(text('p','Worker: '+run.job.status+' · Intentos: '+run.job.attempts,'metadata'));
  if(run.outcome_code)el.append(text('p','Resultado: '+run.outcome_code,'warning-note'));
  if(run.answer)el.append(text('p',run.answer,'fragment'));
  const actions=text('div','','action-row');
  const recover=run.job&&(run.job.status==='PAUSED'||run.job.status==='RUNNING'&&!run.job.lease_active);
  if(recover)actions.append(mutation('Recuperar ejecución',async()=>{await api(runPath(summary)+'/resume',{});message('Recuperación admitida sin reiniciar los presupuestos.');}));
  if(summary.kind==='ci'&&run.status==='COMPLETED'&&has('operator'))actions.append(mutation('Crear o recuperar propuesta',async()=>{
    if(!proposalKeys.has(run.run_id))proposalKeys.set(run.run_id,crypto.randomUUID());
    const item=await api('runs/'+run.run_id+'/proposals',{request_key:proposalKeys.get(run.run_id)});
    selectedProposal=item.proposal.proposal_id;show('approvals');message('Propuesta guardada. Debe decidir otro actor autorizado.');
  }));
  if(summary.trace_id&&/^[0-9a-f]{32}$/.test(summary.trace_id)){
    const link=text('a','Abrir traza en Jaeger ↗');link.href='http://127.0.0.1:16686/trace/'+summary.trace_id;link.target='_blank';link.rel='noopener noreferrer';actions.append(link);
  }
  el.append(actions);
  if(summary.kind==='rag')el.append(text('p','El modelo sigue siendo guionado. El contenido documental no concede permiso para ejecutar acciones.','hint'));
  for(const result of run.results){
    const evidence=document.createElement('details');evidence.append(text('summary',result.tool_name+' · '+result.source_id),text('pre',JSON.stringify(result.data,null,2)));el.append(evidence);
  }
  root.append(el);
}
async function refreshRuns(){
  const result=await api('console/runs');runs=result.runs;renderRuns(runs);
  if(!selectedRun&&runs.length)selectedRun=runs[0];
  if(!selectedRun)return;
  const current=runs.find(item=>item.run_id===selectedRun.run_id)||selectedRun;selectedRun=current;
  try{renderRun(await api(runPath(current)),current);}
  catch(error){$('run-detail').replaceChildren(text('p',error.message,'warning-note'));runStamp=null;}
}
function renderApprovals(items){
  const stamp=JSON.stringify([items,selectedProposal,busy]);if(stamp===approvalListStamp)return;approvalListStamp=stamp;
  const root=$('approval-list');root.replaceChildren();
  if(!items.length)root.append(text('p','No hay propuestas accesibles. Un operador puede crearlas desde una lectura CI completada.','hint'));
  for(const item of items){const p=item.proposal;const el=row('Pruebas · '+p.payload.arguments.job,p.effective_status);
    el.append(text('p',p.requested_by+' · '+p.payload.arguments.revision+' · '+p.proposal_id.slice(0,8),'metadata'));
    el.append(button(selectedProposal===p.proposal_id?'Seleccionada':'Revisar intención',()=>{selectedProposal=p.proposal_id;proposalStamp=null;$('approval-detail').replaceChildren();refresh().catch(error=>message(error.message,true));}));root.append(el);
  }
}
async function renderProposal(item){
  const p=item.proposal, own=p.requested_by===getIdentity().actor_id;
  let operation=item.operation;
  if(operation&&own&&has('operator'))operation=await api('operations/'+operation.operation_id);
  const stamp=JSON.stringify([item,operation,busy]);if(stamp===proposalStamp)return;proposalStamp=stamp;
  const root=$('approval-detail');root.replaceChildren();const el=row('Intención de '+p.requested_by,p.effective_status);
  el.append(text('p',p.proposal_id+'\nVence: '+timestamp(p.expires_at)+'\nHash: '+p.payload_hash,'metadata'));
  el.append(text('pre',JSON.stringify(p.payload,null,2)));
  const actions=text('div','','action-row');
  if(p.effective_status==='PENDING_APPROVAL'){
    if(own)el.append(text('p','La decisión debe tomarla otro actor con permiso de aprobación.','hint'));
    else if(has('approver'))for(const [label,decision] of [['Aprobar intención','APPROVED'],['Rechazar intención','REJECTED']])actions.append(mutation(label,async()=>{
      await api('proposals/'+p.proposal_id+'/decision',{payload_hash:p.payload_hash,expected_version:p.version,decision});message('Decisión persistida para esta intención y versión.');
    }));
  }
  if(own&&has('operator')&&p.effective_status==='APPROVED'&&!operation)actions.append(mutation('Preparar operación única',async()=>{
    await api('proposals/'+p.proposal_id+'/operation',{});message('Operación preparada; todavía no se envió al proveedor.');
  }));
  el.append(actions);
  if(operation){
    const op=row('Operación simulada',operation.status);op.append(text('p',operation.operation_id,'metadata'));
    const job=operation.write_job||item.write_job;
    if(job)op.append(text('p','Worker de escrituras: '+job.status+' · Intentos: '+job.attempts+(job.last_error?' · '+job.last_error:''),'metadata'));
    if(own&&has('operator')&&operation.status==='PREPARED'&&!job)op.append(mutation('Encolar pruebas simuladas',async()=>{
      await api('operations/'+operation.operation_id+'/dispatch',{});message('Operación admitida. El worker de escrituras confirmará el resultado mediante el ledger.');
    }));
    const resolution=item.resolution;
    if(own&&has('operator')&&['UNKNOWN','DISPATCHING'].includes(operation.status)&&job?.status==='NEEDS_REVIEW'&&!resolution)op.append(mutation('Preparar cierre para revisión',async()=>{
      await api('operations/'+operation.operation_id+'/resolution',{});message('Cierre del proveedor preparado; requiere revisión por otro actor.');
    }));
    if(resolution){
      op.append(text('h3','Cierre del proveedor · '+resolution.effective_status));
      op.append(text('pre',JSON.stringify(resolution.payload,null,2)),text('p','Hash: '+resolution.payload_hash,'metadata'));
      if(!own&&has('approver')&&resolution.effective_status==='PENDING_REVIEW'&&operation.status==='UNKNOWN')op.append(mutation('Confirmar cierre revisado',async()=>{
        await api('operations/'+operation.operation_id+'/resolution/confirm',{payload_hash:resolution.payload_hash,expected_version:resolution.version});message('Cierre revisado confirmado.');
      }));
    }
    if(operation.events)for(const event of operation.events)op.append(text('p',event.status+' · '+event.actor_id+' · '+timestamp(event.occurred_at),'metadata'));
    op.append(text('p','SUCCEEDED confirma aceptación del proveedor simulado; el resultado del build es una evidencia aparte.','hint'));el.append(op);
  }
  root.append(el);
}
async function refreshApprovals(){
  const response=await api('proposals');renderApprovals(response.items);
  if(!selectedProposal&&response.items.length)selectedProposal=response.items[0].proposal.proposal_id;
  if(selectedProposal){try{await renderProposal(await api('proposals/'+selectedProposal));}catch(error){$('approval-detail').replaceChildren(text('p',error.message,'warning-note'));proposalStamp=null;}}
}
function renderConfig(data){
  const root=$('config-values');root.replaceChildren();
  for(const [title,value] of [['Identidad',{workspace:data.workspace,mode:data.identity_mode}],['Modelo y recuperación',{model:data.model,embeddings:data.embeddings,active_index:data.active_index,available:data.embedding_profiles}],['Límites por perfil',data.limits],['Ingestión',data.ingestion]]){
    const el=row(title,'Solo lectura');el.append(text('pre',JSON.stringify(value,null,2)));root.append(el);
  }
}
async function refresh(){
  if(!getIdentity()||refreshing)return;const version=sessionVersion;refreshing=true;
  try{
    const current=await api('me');if(version!==sessionVersion)return;
    if(JSON.stringify(current.identity)!==JSON.stringify(getIdentity())){disconnect();message('Los permisos cambiaron. Seleccioná de nuevo la credencial para actualizar la consola.');return;}
    if(active==='runs'&&canRead())await refreshRuns();
    if(active==='knowledge'&&canRead())await refreshKnowledge();
    if(active==='approvals'&&canReview())await refreshApprovals();
    if(active==='config')renderConfig(await api('console/config'));
  }finally{refreshing=false;}
}
window.addEventListener('harness-session',()=>{
  sessionVersion++;selectedRun=null;selectedProposal=null;runs=[];config=null;proposalKeys.clear();clearDetails();
  if(!getIdentity())return;
  const version=sessionVersion;
  api('console/config').then(data=>{
    if(version!==sessionVersion)return;config=data;
    $('ci-scenario').replaceChildren(...data.scenarios.map(item=>{const option=text('option',item.title);option.value=item.id;return option;}));
    const requested=location.hash.slice(1)||(location.pathname==='/knowledge'?'knowledge':'runs');show(requested);
  }).catch(error=>message(error.message,true));
});
$('run-kind').addEventListener('change',()=>{
  const rag=$('run-kind').value==='rag';$('ci-scenario').disabled=rag;$('ci-pause').disabled=rag;
  $('run-help').textContent=rag?'Jira → GitHub → Jenkins → RAG. Consulta fija de demostración; cinco decisiones y cuatro herramientas.':'Modelo y proveedores DevOps simulados. Cuatro decisiones; presupuesto persistido y recuperación.';
});
$('launch-form').addEventListener('submit',event=>{event.preventDefault();action(async()=>{
  const rag=$('run-kind').value==='rag';
  const started=await api(rag?'knowledge/agent/runs':'runs',rag?{}:{scenario:$('ci-scenario').value,pause_at:$('ci-pause').value});
  selectedRun={run_id:started.run_id,kind:rag?'rag':'ci'};message('Lectura admitida. El worker de ejecuciones continuará el recorrido.');
});});
setInterval(()=>{if(!document.hidden)refresh().catch(error=>message(error.message,true));},5000);
const initial=location.hash.slice(1)||(location.pathname==='/knowledge'?'knowledge':'runs');
if(names[initial])show(initial);
