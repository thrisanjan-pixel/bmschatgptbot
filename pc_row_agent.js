/* Keep the seat reader on the user's PC while Railway stores rules and sends alerts. */
'use strict';
const crypto=require('crypto');
const http=require('http');
const path=require('path');
const {spawn}=require('child_process');

const ROOT=__dirname;
const URL='https://bmschatgptbot-production.up.railway.app';
const PORT=8125;
const csrf=crypto.randomBytes(24).toString('base64url');
let password='',lastRead=0,lastError='',connected=false,working=false;

function signed(method,route,body='',key=password){
  const stamp=String(Math.floor(Date.now()/1000)),nonce=crypto.randomBytes(18).toString('base64url');
  const digest=crypto.createHash('sha256').update(body).digest('hex');
  const payload=[method,route,stamp,nonce,digest].join('\n');
  const signature=crypto.createHmac('sha256',key).update(payload).digest('hex');
  return {'X-Row-Time':stamp,'X-Row-Nonce':nonce,'X-Row-Signature':signature};
}
async function call(method,route,data,key=password){
  const body=data===undefined?'':JSON.stringify(data);
  const response=await fetch(URL+route,{method,body:method==='GET'?undefined:body,
    headers:{...signed(method,route,body,key),...(method==='GET'?{}:{'Content-Type':'application/json'})},
    signal:AbortSignal.timeout(25000)});
  if(!response.ok)throw Error(response.status===401?'Website password was rejected':`Railway returned HTTP ${response.status}`);
  return response.json();
}
function readShow(show){return new Promise((resolve,reject)=>{
  const node=process.execPath,script=path.join(ROOT,'pc_row_reader.js');
  const child=spawn(node,[script],{cwd:ROOT,windowsHide:true,stdio:['pipe','pipe','pipe']});
  let output='',error='';
  const timer=setTimeout(()=>{child.kill();reject(Error('Seat read timed out'))},60000);
  child.stdout.on('data',c=>{output+=c;if(output.length>600000){child.kill();reject(Error('Seat read was too large'))}});
  child.stderr.on('data',c=>{error+=c;if(error.length>2000)error=error.slice(-2000)});
  child.on('error',e=>{clearTimeout(timer);reject(e)});
  child.on('close',()=>{clearTimeout(timer);try{const result=JSON.parse(output);if(result.error)throw Error(result.error);resolve(result.data)}catch(e){reject(Error(e.message||'Seat reader failed'))}});
  child.stdin.end(JSON.stringify(show));
})}
async function loop(){
  if(!password||working)return;
  working=true;
  try{
    const {jobs}=await call('GET','/api/row-agent/jobs');
    connected=true;lastError='';
    for(const show of jobs){
      try{
        const data=await readShow(show);
        await call('POST','/api/row-agent/observation',{show_id:show.id,data});
        lastRead=Date.now();
      }catch(error){
        lastError=String(error.message||error).slice(0,180);
        try{await call('POST','/api/row-agent/observation',{show_id:show.id,error:lastError})}catch{}
      }
    }
  }catch(error){connected=false;lastError=String(error.message||error).slice(0,180)}
  finally{working=false;setTimeout(loop,3000)}
}
function page(message=''){
  const safe=message.replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  return `<!doctype html><html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Screenwatch PC seat reader</title><style>body{background:#08090c;color:#f3f4f6;font:15px system-ui;margin:0;min-height:100vh;display:grid;place-items:center}main{width:min(420px,calc(100vw - 40px));background:#101217;border:1px solid #ffffff20;border-radius:14px;padding:28px}h1{font-size:22px}p{color:#a1a1aa;line-height:1.5}input,button{box-sizing:border-box;width:100%;padding:12px;border-radius:8px;font:inherit}input{background:#08090c;color:white;border:1px solid #ffffff38}button{border:0;background:white;color:#08090c;font-weight:700;margin-top:14px;cursor:pointer}.message{color:#34d399}</style><main><h1>Connect PC seat reader</h1><p>Enter your Screenwatch website password here on your PC. It stays in this reader's memory until the reader stops and signs requests to your Railway site. Never send it in chat.</p><p class="message">${safe}</p><form method="post" action="/pair"><input type="hidden" name="csrf" value="${csrf}"><label>Website password<input type="password" name="password" required minlength="16" autocomplete="current-password"></label><button>Connect seat reader</button></form><p>Keep this PC awake for 30-second row checks. If the reader or PC restarts, connect it again. The website remains usable from your phone.</p></main></html>`;
}
const server=http.createServer(async(req,res)=>{
  if(req.headers.host!==`127.0.0.1:${PORT}`&&req.headers.host!==`localhost:${PORT}`){res.writeHead(403);res.end();return}
  res.setHeader('Cache-Control','no-store');res.setHeader('X-Content-Type-Options','nosniff');
  if(req.method==='GET'&&req.url==='/status'){
    res.setHeader('Content-Type','application/json');res.end(JSON.stringify({paired:!!password,connected,lastRead,lastError,working}));return;
  }
  if(req.method==='GET'&&req.url==='/'){
    res.setHeader('Content-Type','text/html; charset=utf-8');res.end(page(password?(connected?'Seat reader connected.':'Seat reader paired; connecting…'):''));return;
  }
  if(req.method==='POST'&&req.url==='/pair'){
    if(req.headers.origin&&!['http://127.0.0.1:'+PORT,'http://localhost:'+PORT].includes(req.headers.origin)){res.writeHead(403);res.end();return}
    let body='';for await(const part of req){body+=part;if(body.length>4096){res.writeHead(413);res.end();return}}
    const form=new URLSearchParams(body),candidate=form.get('password')||'';
    if(form.get('csrf')!==csrf||candidate.length<16){res.writeHead(400);res.end(page('Check the password and try again.'));return}
    try{
      await call('GET','/api/row-agent/jobs',undefined,candidate);
      password=candidate;connected=true;setTimeout(loop,0);
      res.writeHead(303,{Location:'/'});res.end();
    }catch{res.writeHead(401,{'Content-Type':'text/html; charset=utf-8'});res.end(page('Could not connect. Check the website password and Railway connection.'))}
    return;
  }
  res.writeHead(404);res.end();
});
if(require.main===module)server.listen(PORT,'127.0.0.1',()=>{
  process.stdout.write(`Screenwatch PC seat reader: http://127.0.0.1:${PORT}/\n`);
});
module.exports={signed};
