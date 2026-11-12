import test from 'node:test';
import assert from 'node:assert/strict';
globalThis.document={getElementById(id){return {value:'',hidden:false,replaceChildren(){},addEventListener(){},classList:{toggle(){}}};}};
globalThis.window=new EventTarget();
globalThis.setInterval=()=>0;
const {challenge,randomString,base64url}=await import('../frontend/oidc.js');

test('PKCE S256 coincide con el vector de la RFC 7636',async()=>{
  assert.equal(await challenge('dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk'),'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM');
});
test('El verifier es aleatorio y usa solo el alfabeto base64url',()=>{
  const a=randomString(48),b=randomString(48);
  assert.notEqual(a,b);assert.match(a,/^[A-Za-z0-9_-]{43,128}$/);assert.equal(base64url(new Uint8Array([251,255])),'-_8');
});
