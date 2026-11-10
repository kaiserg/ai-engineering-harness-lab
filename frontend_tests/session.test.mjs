import test from 'node:test';
import assert from 'node:assert/strict';
// Puerto DOM mínimo: probamos la frontera asíncrona de sesión sin un navegador ni tokens reales.
const elements=new Map();
globalThis.document={getElementById(id){
  if(!elements.has(id))elements.set(id,{value:'',hidden:false,replaceChildren(){},addEventListener(){},classList:{toggle(){}}});
  return elements.get(id);
}};
globalThis.window=new EventTarget();
globalThis.setInterval=()=>0;
const {api,disconnect}=await import('../frontend/knowledge.js');

for(const status of [200,401])test(`Una respuesta ${status} de una sesión anterior no atraviesa el cambio de sesión`,async()=>{
  let finishBody;let parsing;
  const parsingStarted=new Promise(resolve=>{parsing=resolve;});
  globalThis.fetch=async()=>({ok:status===200,status,json(){parsing();return new Promise(resolve=>{finishBody=resolve;});}});
  let resets=0;const listener=()=>{resets++;};window.addEventListener('harness-session',listener);
  const pending=api('protected');await parsingStarted;
  disconnect();finishBody({private:'OLD_SESSION_CONTENT',error:'UNAUTHENTICATED'});
  await assert.rejects(pending,/La sesión cambió/);
  assert.equal(resets,1,'Una respuesta vieja no debe limpiar una sesión posterior');
  window.removeEventListener('harness-session',listener);
});
