'use strict';
// Login con OIDC (código de autorización + PKCE). El access token vive solo en memoria;
// sessionStorage guarda únicamente el verifier y el state durante la redirección.
import {connectToken,message} from './knowledge.js';
const $=id=>document.getElementById(id);
const KEY='harness-oidc-flow';

export function base64url(bytes){let binary='';for(const byte of bytes)binary+=String.fromCharCode(byte);return btoa(binary).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');}
export function randomString(length=32){return base64url(crypto.getRandomValues(new Uint8Array(length)));}
export async function challenge(verifier){return base64url(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(verifier))));}
function remember(value){try{sessionStorage.setItem(KEY,JSON.stringify(value));}catch{}}
function recall(){try{const raw=sessionStorage.getItem(KEY);sessionStorage.removeItem(KEY);return raw?JSON.parse(raw):null;}catch{return null;}}

export async function start(config){
  const verifier=randomString(48),state=randomString(24);
  remember({verifier,state});
  const url=new URL(config.authorization_endpoint);
  for(const [key,value] of Object.entries({response_type:'code',client_id:config.client_id,redirect_uri:location.origin+'/console',
    scope:'openid',state,code_challenge:await challenge(verifier),code_challenge_method:'S256'}))url.searchParams.set(key,value);
  location.assign(url);
}

export async function finish(config,params){
  const saved=recall();
  history.replaceState(null,'',location.pathname+location.hash);
  if(!saved || !params.get('state') || params.get('state')!==saved.state)throw new Error('La respuesta de inicio de sesión no coincide con la solicitud.');
  if(params.get('error'))throw new Error('El proveedor de identidad rechazó el inicio de sesión.');
  const response=await fetch(config.token_endpoint,{method:'POST',credentials:'omit',cache:'no-store',redirect:'error',
    headers:{'Content-Type':'application/x-www-form-urlencoded'},
    body:new URLSearchParams({grant_type:'authorization_code',client_id:config.client_id,code:params.get('code'),
      redirect_uri:location.origin+'/console',code_verifier:saved.verifier})});
  const data=await response.json();
  if(!response.ok || typeof data.access_token!=='string')throw new Error('No se pudo completar el inicio de sesión.');
  await connectToken(data.access_token,config.workspace);
}

async function init(){
  let config;
  try{const response=await fetch('/oidc-config',{credentials:'omit',cache:'no-store'});config=await response.json();}catch{return;}
  if(!config.enabled)return;
  $('oidc-login').hidden=false;
  $('oidc-login').addEventListener('click',()=>start(config).catch(error=>message(error.message,true)));
  const params=new URLSearchParams(location.search);
  if(params.has('code')||params.has('error'))finish(config,params).catch(error=>message(error.message,true));
}
if(typeof document!=='undefined' && document.getElementById('oidc-login'))init();
