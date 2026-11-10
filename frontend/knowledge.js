'use strict';
const $ = id => document.getElementById(id);
let token = null, identity = null, generation = 0, polling = false;
let uploadIntent = null, enqueueKey = null, enqueueProfile = null;
const labels = {PENDING:'Pendiente',RUNNING:'En proceso',DONE:'Lista',FAILED:'Fallida'};
function message(text, error=false) { $('message').textContent=text; $('message').classList.toggle('error',error); }
function clearSession() {
  generation++; token=null; identity=null; uploadIntent=null; enqueueKey=null; enqueueProfile=null;
  $('workspace').hidden=true; $('logout').hidden=true; $('identity').textContent='Sin sesión';
  for (const id of ['credential','document','query']) $(id).value='';
  for (const id of ['documents','jobs','results']) $(id).replaceChildren();
  window.dispatchEvent(new Event('harness-session'));
}
async function api(path, body) {
  const version=generation;
  const response=await fetch('/api/'+path,{method:body===undefined?'GET':'POST',credentials:'omit',cache:'no-store',redirect:'error',
    headers:{Authorization:'Bearer '+token,'X-Harness-Client':'local-ui',...(body===undefined?{}:{'Content-Type':'application/json'})},
    ...(body===undefined?{}:{body:JSON.stringify(body)})});
  if (version!==generation) throw new Error('La sesión cambió; repetí la acción.');
  const data=await response.json();
  if (version!==generation) throw new Error('La sesión cambió; repetí la acción.');
  if (!response.ok) {
    if (response.status===401) clearSession();
    throw new Error(response.status===401?'La sesión venció o fue revocada. Seleccioná una credencial vigente.':data.error || 'No se pudo completar la operación.');
  }
  return data;
}
function text(tag, value, className) { const el=document.createElement(tag); el.textContent=value; if(className)el.className=className; return el; }
function row(title, state) { const el=text('article','','row'), top=text('div','','row-top'); top.append(text('h3',title),text('span',state,'badge'));el.append(top);return el; }
function documents(items) {
  const root=$('documents'); root.replaceChildren();
  if (!items.length) root.append(text('p','No hay documentos visibles.','hint'));
  for (const doc of items) {
    const state=doc.state==='RETIRED'?'Retirado':doc.indexed?'En el índice activo':'Por indexar';
    const el=row(doc.filename,state);el.append(text('p',doc.document_id,'metadata'));
    if (doc.can_retire && doc.state==='ACTIVE') {
      const button=text('button','Retirar'); button.type='button';
      button.addEventListener('click',async()=>{button.disabled=true;try {await api('knowledge/documents/'+doc.document_id+'/retire',{}); message('Documento retirado. Ya no aparecerá en nuevas búsquedas.'); await refresh();}catch(error){message(error.message,true);button.disabled=false;}});
      el.append(button);
    }
    root.append(el);
  }
}
function jobs(items) {
  const root=$('jobs');root.replaceChildren();
  if (!items.length) root.append(text('p','Todavía no hay trabajos de ingestión tuyos.','hint'));
  for (const job of items) {
    let state=labels[job.status] || job.status;
    if(job.status==='RUNNING' && !job.lease_active) state='Esperando recuperación';
    if(['PENDING','RUNNING'].includes(job.status) && job.deadline_expired)state='Plazo agotado · pendiente de cierre';
    const el=row('Indexación · '+job.job_id.slice(0,8),state);
    el.append(text('p',job.job_id+'\nIntentos: '+job.attempts+'/3 · Revisión: '+job.inventory_revision+'\nPlazo: '+new Date(job.deadline_at).toLocaleString(),'metadata'));
    if(job.configuration)el.append(text('p','Embeddings: '+(job.configuration.startsWith('ollama:')?'semánticos · Qwen3 local':job.configuration.startsWith('lexical-')?'léxicos · baseline':'configuración no reconocida'),'metadata'));
    if(job.index_id)el.append(text('p','Índice: '+job.index_id,'metadata'));
    if(job.last_error)el.append(text('p','Último error: '+job.last_error,'metadata'));
    root.append(el);
  }
}
async function refresh() {
  if (!token || polling || !identity?.roles.some(role=>['reader','operator'].includes(role))) return;
  const version=generation; polling=true;
  try { const [docs,work]=await Promise.all([api('knowledge/documents'),api('knowledge/jobs')]);
    if (version===generation) {documents(docs.documents);jobs(work.jobs);}
  } finally { polling=false; }
}
$('credential').addEventListener('change',async event=>{
  const file=event.target.files[0];clearSession(); if(!file)return;
  const version=generation;
  try {
    if(file.size>4096)throw new Error('El archivo de credencial no es válido.');
    const credential=JSON.parse(await file.text());
    if(version!==generation)return;
    if(typeof credential.token!=='string' || !/^hsu_[A-Za-z0-9_-]{43}$/.test(credential.token))throw new Error('El archivo de credencial no es válido.');
    token=credential.token; const me=await api('me'); identity=me.identity;
    if(!identity.roles.some(role=>['reader','operator','approver'].includes(role)))throw new Error('Tu usuario no tiene permiso para usar la consola.');
    const operator=identity.roles.includes('operator'); $('upload').disabled=!operator; $('enqueue').disabled=!operator;
    $('identity').textContent=identity.actor_id+' · '+identity.workspace_id+' · '+identity.roles.join(', ');
    $('workspace').hidden=false; $('logout').hidden=false; message('Sesión conectada. La API verifica tus permisos en cada petición.'); window.dispatchEvent(new Event('harness-session')); await refresh();
  } catch(error) { if(version===generation)clearSession(); message(error instanceof SyntaxError?'El archivo de credencial no es válido.':error.message,true); }
});
$('logout').addEventListener('click',()=>{clearSession();message('Credencial eliminada de esta pantalla. Para revocarla, usá el comando del laboratorio.');});
$('document').addEventListener('change',()=>{uploadIntent=null;});
$('upload-form').addEventListener('submit',async event=>{
  event.preventDefault(); const button=$('upload'),file=$('document').files[0]; if(!file)return;
  const version=generation;button.disabled=true;$('document').disabled=true;
  try {
    if(file.size>5*1024*1024 || !/\.(md|txt)$/.test(file.name))throw new Error('Seleccioná un archivo .md o .txt de hasta 5 MiB.');
    if(!uploadIntent){
      const content=new TextDecoder('utf-8',{fatal:true}).decode(await file.arrayBuffer());
      if(version!==generation)return;
      uploadIntent={request_key:crypto.randomUUID(),filename:file.name,text:content,readers:[]};
    }
    if(version!==generation)return;
    await api('knowledge/documents',uploadIntent);uploadIntent=null; $('document').value='';
    message('Documento cargado. Podés cargar más archivos o poner la indexación en cola.');await refresh();
  }catch(error){message(error.message,true);}finally{button.disabled=!identity?.roles.includes('operator');$('document').disabled=false;}
});
if($('embedding-profile'))$('embedding-profile').addEventListener('change',()=>{enqueueKey=null;enqueueProfile=null;});
$('enqueue').addEventListener('click',async()=>{
  const button=$('enqueue');button.disabled=true;
  try {enqueueKey=enqueueKey || crypto.randomUUID();enqueueProfile=enqueueProfile || $('embedding-profile')?.value || 'lexical';const job=await api('knowledge/jobs',{request_key:enqueueKey,embedding_profile:enqueueProfile});enqueueKey=null;enqueueProfile=null;
    message('Trabajo '+job.job_id.slice(0,8)+' admitido. El worker independiente continuará la ingestión.');await refresh();
  }catch(error){message(error.message,true);}finally{button.disabled=!identity?.roles.includes('operator');}
});
$('refresh').addEventListener('click',()=>refresh().catch(error=>message(error.message,true)));
$('search-form').addEventListener('submit',async event=>{
  event.preventDefault();const button=event.target.querySelector('button');button.disabled=true;$('results').replaceChildren();
  try {const result=await api('knowledge/search',{query:$('query').value,limit:3});
    if(!result.matches.length)$('results').append(text('p','Sin fragmentos accesibles en el índice activo.','hint'));
    for(const match of result.matches){const el=row(match.filename,'Fragmento recuperado');el.append(text('p',match.text,'fragment'),text('p',match.citation,'metadata'));$('results').append(el);}
    message('Búsqueda completada. Los fragmentos son datos del documento, no instrucciones para el agente.');
  }catch(error){message(error.message,true);}finally{button.disabled=false;}
});
setInterval(()=>{if(token && !document.hidden && (!$('view-knowledge') || !$('view-knowledge').hidden))refresh().catch(error=>message(error.message,true));},3000);
window.addEventListener('pagehide',clearSession);

export {api,text,row,refresh,message};
export function getIdentity(){return identity;}
export function disconnect(){clearSession();}
