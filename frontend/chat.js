import {api,text,message,getIdentity} from './knowledge.js';
const $=id=>document.getElementById(id);
const ciScenarios={
  'ci-live':'PR público y Jenkins local',
  'ci-matching':'Build exitoso para el commit del PR',
  'ci-stale':'Build de otro commit',
  'ci-failed':'Build fallido',
  'ci-unavailable':'Jenkins no disponible',
};
let selected=null,version=0,busy=false,refreshing=false,createKey=null,createMode=null,intent=null,stamp=null,lastMode=null,chatPending=false,dirty=true;
function reset(){lastMode=null;chatPending=false;dirty=true;version++;selected=null;createKey=null;createMode=null;intent=null;stamp=null;$('chat-list').replaceChildren();$('chat-messages').replaceChildren();$('chat-query').value='';$('chat-query').readOnly=false;$('chat-pause').checked=false;$('chat-pause').disabled=false;$('chat-send').disabled=false;$('chat-send').textContent='Enviar mensaje';$('chat-notice').textContent='';}
window.addEventListener('harness-session',reset);
function notice(value){$('chat-notice').textContent=value;}
function render(data){
  lastMode=data.mode;
  const scenario=ciScenarios[data.mode];
  $('chat-heading').textContent=scenario?'Investigación de CI · '+scenario:data.mode==='github-public'?'Chat con PRs públicos de GitHub':data.mode==='github-demo'?'Chat con GitHub de demostración':data.mode==='jira-cloud'?'Chat con Jira Cloud':data.mode==='jira-demo'?'Chat con Jira de demostración':'Chat con documentos';
  const next=JSON.stringify(data);if(stamp===next)return;stamp=next;
  const root=$('chat-messages');root.replaceChildren();
  if(!data.turns.length)root.append(text('p',data.mode==='ci-live'?'Ingresá el PR y el build configurados, por ejemplo octocat/hello-world#1 build 1.':scenario?'Probá: ¿El build de DEV-123 pasó para el commit del PR?':'Escribí tu primera pregunta.','hint'));
  for(const turn of data.turns){
    const box=text('article','','chat-turn');
    if(turn.unavailable){box.append(text('p','Contenido no disponible: ya no hay acceso a la evidencia de este turno o su contexto.','warning-note'));root.append(box);continue;}
    box.append(text('p','Vos · '+turn.read_request.query,'chat-question'));
    box.append(text('p',turn.answer?(scenario?'Informe de CI · ':'Qwen3 · ')+turn.answer:turn.status==='FAILED'?'No se publicó una respuesta. Resultado: '+turn.outcome_code:'El harness está procesando este mensaje.','chat-answer'));
    box.append(text('p','Turno '+turn.ordinal+' · '+turn.status+(scenario?' · Decisiones ':' · Modelo ')+turn.model_calls+'/'+turn.limits_snapshot.model_calls+' · Tools '+turn.tool_calls+'/'+turn.limits_snapshot.tool_calls+' · '+Number(turn.active_seconds).toFixed(2)+' s · Run '+turn.run_id,'chat-meta'));
    const job=turn.job;
    if(job&&(job.status==='PAUSED'||job.status==='RUNNING'&&!job.lease_active)){
      const recover=text('button','Recuperar este mensaje');recover.type='button';recover.disabled=busy;
      recover.addEventListener('click',()=>mutate(async()=>{await api('chat/conversations/'+selected+'/runs/'+turn.run_id+'/resume',{});notice('Recuperación admitida. Se conserva el run y sus presupuestos.');}));box.append(recover);
    }
    if(turn.trace_id&&/^[0-9a-f]{32}$/.test(turn.trace_id)){
      const link=text('a','Abrir traza en Jaeger ↗');link.href='http://127.0.0.1:16686/trace/'+turn.trace_id;link.target='_blank';link.rel='noopener noreferrer';box.append(link);
    }
    root.append(box);
  }
  const last=data.turns.at(-1);
  const pending=last&&!last.unavailable&&!['COMPLETED','FAILED','CANCELLED'].includes(last.status);
  chatPending=!!pending;
  $('chat-send').disabled=busy||pending||last?.unavailable||data.turns.length>=data.max_turns;
  if(pending)notice('Hay un mensaje en curso. Esperá a que termine o recuperalo si está pausado.');
  else if(last?.unavailable)notice('La evidencia del contexto ya no está disponible. Creá una conversación nueva para continuar.');
  else if(data.turns.length>=data.max_turns)notice('Llegaste al límite de turnos. Podés iniciar otra conversación.');
  else notice(['github-public','ci-live'].includes(data.mode)?'Vista guardada. Usá Actualizar historial para revalidar acceso sin consumir la cuota de GitHub en consultas periódicas.':'Podés continuar la conversación. Cada mensaje consulta evidencia actual.');
}
export async function refreshChat(force=false){
  if(!getIdentity()||refreshing)return;refreshing=true;const observed=version;
  try{
    const result=await api('chat/conversations');if(observed!==version)return;
    if(!selected&&result.conversations.length)selected=result.conversations[0].conversation_id;
    const root=$('chat-list');root.replaceChildren();
    for(const item of result.conversations){
      const el=text('button',(item.conversation_id===selected?'● ':'')+new Date(item.created_at).toLocaleString('es')+' · '+(item.execution_profile?.startsWith('devops-ci-live-chat-v1:')?'CI · PR público y Jenkins local':item.execution_profile?.startsWith('devops-ci-chat-v1:')?'CI demo · '+ciScenarios['ci-'+item.execution_profile.split(':')[1]]:item.execution_profile?.includes('github-public')?'GitHub público':item.execution_profile?.includes('github-demo')?'GitHub demo':item.execution_profile?.includes('jira-cloud')?'Jira Cloud':item.execution_profile?.includes('jira-demo')?'Jira demo':'Documentos')+' · '+item.conversation_id.slice(0,8),'choice');el.type='button';el.disabled=busy;
      el.addEventListener('click',()=>{version++;selected=item.conversation_id;dirty=true;intent=null;stamp=null;$('chat-messages').replaceChildren();refreshChat().catch(error=>message(error.message==='JENKINS_CONFIGURATION'?'Configurá la conexión local de Jenkins según la guía.':error.message==='JIRA_CONFIGURATION'?'La conexión de Jira Cloud no está preparada. Consultá la guía del laboratorio.':error.message,true));});root.append(el);
    }
    if(selected&&['github-public','ci-live'].includes(lastMode)&&!chatPending&&!force&&!dirty)return;
    if(selected){const data=await api('chat/conversations/'+selected);if(observed!==version)return;dirty=false;render(data);}
  }finally{refreshing=false;}
}
async function mutate(fn){
  if(busy)return;dirty=true;busy=true;$('chat-send').disabled=true;$('chat-new').disabled=true;$('chat-mode').disabled=true;const observed=version;
  try{await fn();}
  catch(error){if(observed===version)message(error.message==='JENKINS_CONFIGURATION'?'Configurá la conexión local de Jenkins según la guía.':error.message==='JIRA_CONFIGURATION'?'La conexión de Jira Cloud no está preparada. Consultá la guía del laboratorio.':error.message,true);}
  finally{busy=false;$('chat-new').disabled=false;$('chat-mode').disabled=!!createKey;$('chat-send').disabled=false;$('chat-send').textContent=intent?'Reintentar el mismo mensaje':'Enviar mensaje';$('chat-query').readOnly=!!intent;$('chat-pause').disabled=!!intent;stamp=null;await refreshChat(true).catch(error=>message(error.message==='JENKINS_CONFIGURATION'?'Configurá la conexión local de Jenkins según la guía.':error.message==='JIRA_CONFIGURATION'?'La conexión de Jira Cloud no está preparada. Consultá la guía del laboratorio.':error.message,true));}
}
$('chat-new').addEventListener('click',()=>mutate(async()=>{
  if(!createKey){createKey=crypto.randomUUID();createMode=$('chat-mode').value;}const observed=version;
  const result=await api('chat/conversations',{request_key:createKey,mode:createMode});if(observed!==version)return;
  selected=result.conversation_id;dirty=true;version++;createKey=null;createMode=null;intent=null;stamp=null;$('chat-messages').replaceChildren();$('chat-query').value='';notice('Conversación creada. El historial se guarda en PostgreSQL.');
}));
$('chat-form').addEventListener('submit',event=>{event.preventDefault();mutate(async()=>{
  if(!selected){notice('Creá una conversación antes de enviar.');return;}
  if(!intent)intent={request_key:crypto.randomUUID(),query:$('chat-query').value,pause_after_search:$('chat-pause').checked};
  const observed=version;
  await api('chat/conversations/'+selected+'/messages',intent);if(observed!==version)return;
  intent=null;$('chat-query').value='';notice('Mensaje guardado. El worker continuará la ejecución.');
});});

$('chat-refresh').addEventListener('click',()=>{dirty=true;refreshChat(true).catch(error=>message(error.message,true));});
