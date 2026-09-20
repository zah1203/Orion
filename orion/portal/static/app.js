'use strict';
let csrf=''; let current=null;
const $=s=>document.querySelector(s);
const instruments=['NIFTY','BANKNIFTY','GOLDM','GOLD','SILVERM','SILVER','CRUDEOIL','CRUDEOILM'];
const credentialFields=['kotak_consumer_key','kotak_mobile','kotak_ucc','kotak_mpin','telegram_api_id','telegram_api_hash'];
const numericFields=['paper_cash','risk_per_trade','daily_loss_limit','max_open_risk','max_lots','max_open_positions','max_entries_per_day'];
function notice(message,error=false){$('#notice').textContent=message;$('#notice').className=error?'error':'ok';}
async function api(path,method='GET',body){
 const headers={};if(method!=='GET'){headers['Content-Type']='application/json';if(csrf)headers['X-CSRF-Token']=csrf;}
 const r=await fetch(path,{method,headers,credentials:'same-origin',body:body===undefined?undefined:JSON.stringify(body)});
 const data=await r.json(); if(!r.ok){if(r.status===401){current=null;$('#dashboard').hidden=true;$('#login-view').hidden=false;$('#logout').hidden=true;}throw new Error(typeof data.detail==='string'?data.detail:'Request failed');}return data;
}
function money(value){return new Intl.NumberFormat('en-IN',{style:'currency',currency:'INR'}).format(Number(value||0));}
for(const product of instruments){const label=document.createElement('label'),input=document.createElement('input');input.type='checkbox';input.value=product;input.name='product';label.append(input,document.createTextNode(product));$('#products').append(label);}
function fillSettings(settings){const form=$('#settings-form');for(const key of numericFields)form.elements[key].value=settings[key];form.elements.index_channel.value='';form.elements.commodity_channel.value='';const products=new Set();for(const [id,c] of Object.entries(settings.channels)){form.elements[c.name==='index-options'?'index_channel':'commodity_channel'].value=id;c.products.forEach(p=>products.add(p));}document.querySelectorAll('[name=product]').forEach(i=>i.checked=products.has(i.value));}
async function refresh(fill=false){
 const data=await api('/api/me');current=data;csrf=data.csrf;$('#login-view').hidden=true;$('#dashboard').hidden=false;$('#logout').hidden=false;
 $('#greeting').textContent=`WELCOME, ${data.username}`;$('#cash').textContent=money(data.state.cash);$('#entry-state').textContent=data.enabled?'Enabled':'Paused';$('#worker-state').textContent=data.worker_online?'Online · paper':'Offline';
 $('#enable').disabled=data.enabled;$('#pause').disabled=!data.enabled;
 $('#connections').textContent=`Saved: ${data.credentials.saved_fields.join(', ')||'none'}. Telegram: ${data.credentials.telegram_linked?'authorized session saved':'not linked'}. Broker: ${data.worker_online?'paper worker online; inspect its authentication logs':'not authenticated by this dashboard'}.`;
 if(fill)fillSettings(data.settings);
 const positions=Object.values(data.state.positions);$('#empty').hidden=positions.length>0;$('#positions').replaceChildren();
 for(const p of positions){const row=document.createElement('tr');for(const value of [p.contract.symbol,p.status,p.remaining,p.entry_fill||'—',p.stop,money(p.pnl)]){const cell=document.createElement('td');cell.textContent=value;row.append(cell);}$('#positions').append(row);}
 $('#history').replaceChildren();for(const event of data.history){const el=document.createElement('p');el.className='activity';el.textContent=`${event.at} · ${event.event}${event.signal_id?' · '+event.signal_id:''}`;$('#history').append(el);}
 if(!data.history.length)$('#history').textContent='Your account has no activity yet.';
}
function bind(selector,fn){$(selector).addEventListener('click',async e=>{e.preventDefault();const button=e.currentTarget;button.disabled=true;try{await fn();}catch(err){notice(err.message,true);}finally{button.disabled=false;}});}
$('#login-form').addEventListener('submit',async e=>{e.preventDefault();const form=e.currentTarget;try{const d=await api('/api/login','POST',{username:form.username.value,password:form.password.value});csrf=d.csrf;form.password.value='';await refresh(true);notice('Signed in.');}catch(err){form.password.value='';notice(err.message,true);}});
bind('#logout',async()=>{await api('/api/logout','POST',{});csrf='';current=null;$('#dashboard').hidden=true;$('#login-view').hidden=false;$('#logout').hidden=true;$('#credentials-form').reset();$('#demo-result').textContent='';notice('Signed out.');});
$('#settings-form').addEventListener('submit',async e=>{e.preventDefault();const form=e.currentTarget,body={};for(const key of numericFields)body[key]=Number(form.elements[key].value);body.index_channel=form.index_channel.value.trim();body.commodity_channel=form.commodity_channel.value.trim();body.products=[...document.querySelectorAll('[name=product]:checked')].map(i=>i.value);try{await api('/api/settings','PUT',body);await refresh(true);notice('Settings saved.');}catch(err){notice(err.message,true);}});
$('#credentials-form').addEventListener('submit',async e=>{e.preventDefault();const form=e.currentTarget,body={};for(const key of credentialFields)if(form.elements[key].value)body[key]=form.elements[key].value;try{await api('/api/credentials','PUT',body);form.reset();await refresh();notice('Credentials saved. Authentication is a separate step.');}catch(err){notice(err.message,true);}});
bind('#clear-credentials',async()=>{if(!confirm('Remove your saved API credentials and Telegram authorization?'))return;await api('/api/credentials','PUT',Object.fromEntries(credentialFields.map(k=>[k,null])));$('#credentials-form').reset();await refresh();notice('Saved credentials removed.');});
for(const [id,enabled] of [['#enable',true],['#pause',false]])bind(id,async()=>{await api('/api/control','POST',{enabled});await refresh();notice(enabled?'Paper entries enabled. An authenticated worker must be online to receive calls.':'New entries paused. Open paper positions still need a running worker.');});
bind('#demo',async()=>{const d=await api('/api/demo','POST',{});$('#demo-result').hidden=false;$('#demo-result').textContent=d.note+'\n\n'+d.events.map(x=>JSON.stringify(x)).join('\n')+'\n\nFinal demo cash: '+money(d.state.cash);});
refresh(true).catch(()=>{});
setInterval(()=>{if(current)refresh().catch(err=>notice(err.message,true));},15000);
