import test from 'node:test';
import assert from 'node:assert/strict';
class Element{
  constructor(){this.value='';this.hidden=true;this.children=[];this.handlers={};this.disabled=false;this.classList={toggle(){}};}
  addEventListener(name,fn){this.handlers[name]=fn;}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=children;}
}
const elements=new Map();const el=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
globalThis.document={getElementById:el,createElement:()=>new Element(),hidden:false};
globalThis.window=new EventTarget();globalThis.setInterval=()=>0;
const id='11111111-1111-4111-8111-111111111111';
let detailReads=0,pauseList=null,turnStatus='COMPLETED';
const listing={conversations:[{conversation_id:id,created_at:'2026-01-01',execution_profile:'ollama-qwen3-4b-github-public-chat-v1'}]};
globalThis.fetch=async(path,options={})=>{
  let data;
  if(path==='/api/me')data={identity:{actor_id:'reader-demo',workspace_id:'workspace-alpha',roles:['reader']}};
  else if(path==='/api/chat/conversations'){
    if(pauseList){const wait=pauseList;pauseList=null;await wait;}
    data=listing;
  }else if(path.endsWith('/messages')){turnStatus='RUNNING';data={run_id:id};}
  else if(path==='/api/chat/conversations/'+id){
    detailReads++;data={mode:'github-public',max_turns:20,turns:[{ordinal:1,run_id:id,status:turnStatus,
      read_request:{query:'demo/atlas#42'},answer:turnStatus==='COMPLETED'?'OPEN':null,
      model_calls:2,tool_calls:1,active_seconds:1,limits_snapshot:{model_calls:3,tool_calls:2},job:{status:turnStatus==='COMPLETED'?'DONE':'RUNNING',lease_active:true}}]};
  }else data={documents:[],jobs:[]};
  return {ok:true,status:200,json:async()=>data};
};
const {refreshChat}=await import('../frontend/chat.js');
const {getIdentity}=await import('../frontend/knowledge.js');
const tick=()=>new Promise(resolve=>setImmediate(resolve));

test('El PR público no se sondea al cerrar y un envío concurrente vuelve a activar seguimiento',async()=>{
  await el('credential').handlers.change({target:{files:[{size:100,text:async()=>JSON.stringify({token:'hsu_'+'a'.repeat(43)})}]}});
  assert.ok(getIdentity());
  await refreshChat();assert.equal(detailReads,1);
  await refreshChat();assert.equal(detailReads,1,'Sin turno activo no vuelve a pedir evidencia a GitHub');
  await refreshChat(true);assert.equal(detailReads,2,'Actualizar historial sí revalida la vista');
  let release;pauseList=new Promise(resolve=>{release=resolve;});
  const inFlight=refreshChat();await tick();
  el('chat-query').value='¿Cuál es el head de ese PR?';
  el('chat-form').handlers.submit({preventDefault(){}});await tick();await tick();
  release();await inFlight;await tick();
  assert.equal(detailReads,3,'El envío no se pierde aunque coincida con una consulta de lista anterior');
  assert.equal(el('chat-send').disabled,true);
  turnStatus='COMPLETED';await refreshChat();assert.equal(el('chat-send').disabled,false);
  const completedReads=detailReads;await refreshChat();assert.equal(detailReads,completedReads);
});
