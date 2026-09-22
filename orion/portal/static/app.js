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
function money(value){if(value===null||value===undefined)return 'Unavailable';return new Intl.NumberFormat('en-IN',{style:'currency',currency:'INR'}).format(Number(value||0));}
for(const product of instruments){const label=document.createElement('label'),input=document.createElement('input');input.type='checkbox';input.value=product;input.name='product';label.append(input,document.createTextNode(product));$('#products').append(label);}
function fillSettings(settings){const form=$('#settings-form');for(const key of numericFields)form.elements[key].value=settings[key];form.elements.index_channel.value='';form.elements.commodity_channel.value='';const products=new Set();for(const [id,c] of Object.entries(settings.channels)){form.elements[c.name==='index-options'?'index_channel':'commodity_channel'].value=id;c.products.forEach(p=>products.add(p));}document.querySelectorAll('[name=product]').forEach(i=>i.checked=products.has(i.value));}
async function refresh(fill=false){
 const data=await api('/api/me');current=data;csrf=data.csrf;$('#login-view').hidden=true;$('#dashboard').hidden=false;$('#logout').hidden=false;
 const health=data.worker_health||{};
 $('#worker-health').textContent=data.worker_online
 ? `Telegram: ${health.telegram||'legacy worker'} · Kotak: ${health.broker||'unknown'} · Catalogue: ${health.catalogue||'unknown'} · Last message: ${health.last_message_at||'none this run'} · Last quote: ${health.last_quote_at||'none this run'}`
 : 'Worker offline — no active monitoring confirmed.';
 $('#greeting').textContent=`WELCOME, ${data.username}`;$('#cash').textContent=money(data.state.cash);$('#entry-state').textContent=data.enabled?'Enabled':'Paused';$('#worker-state').textContent=data.worker_online?'Online · paper':'Offline';
 $('#enable').disabled=data.enabled;$('#pause').disabled=!data.enabled;
 await refreshConnections();
 if(fill)fillSettings(data.settings);
 renderPnl(data.pnl);
 $('#history').replaceChildren();for(const event of data.history){const el=document.createElement('p');el.className='activity';el.textContent=`${event.at} · ${event.event}${event.signal_id?' · '+event.signal_id:''}`;$('#history').append(el);}
 if(!data.history.length)$('#history').textContent='Your account has no activity yet.';
}
function bind(selector,fn){$(selector).addEventListener('click',async e=>{e.preventDefault();const button=e.currentTarget;button.disabled=true;try{await fn();}catch(err){notice(err.message,true);}finally{button.disabled=false;}});}
$('#login-form').addEventListener('submit',async e=>{e.preventDefault();const form=e.currentTarget;try{const d=await api('/api/login','POST',{username:form.username.value,password:form.password.value});csrf=d.csrf;form.password.value='';await refresh(true);notice('Signed in.');}catch(err){form.password.value='';notice(err.message,true);}});
bind('#logout',async()=>{await api('/api/logout','POST',{});csrf='';current=null;$('#dashboard').hidden=true;$('#login-view').hidden=false;$('#logout').hidden=true;resetConnections();$('#demo-result').textContent='';notice('Signed out.');});
$('#settings-form').addEventListener('submit',async e=>{e.preventDefault();const form=e.currentTarget,body={};for(const key of numericFields)body[key]=Number(form.elements[key].value);body.index_channel=form.index_channel.value.trim();body.commodity_channel=form.commodity_channel.value.trim();body.products=[...document.querySelectorAll('[name=product]:checked')].map(i=>i.value);try{await api('/api/settings','PUT',body);await refresh(true);notice('Settings saved.');}catch(err){notice(err.message,true);}});
for(const [id,enabled] of [['#enable',true],['#pause',false]])bind(id,async()=>{await api('/api/control','POST',{enabled});await refresh();notice(enabled?'Paper entries enabled. An authenticated worker must be online to receive calls.':'New entries paused. Open paper positions still need a running worker.');});
bind('#demo',async()=>{const d=await api('/api/demo','POST',{});$('#demo-result').hidden=false;$('#demo-result').textContent=d.note+'\n\n'+d.events.map(x=>JSON.stringify(x)).join('\n')+'\n\nFinal demo cash: '+money(d.state.cash);});
refresh(true).catch(()=>{});
setInterval(()=>{if(current)refresh().catch(err=>notice(err.message,true));},15000);

function colored(cell,value){if(value===null||value===undefined)return;cell.classList.toggle('gain',Number(value)>0);cell.classList.toggle('loss',Number(value)<0);}
function renderPnl(pnl){
 const filter=$('#instrument-filter');const chosen=filter.value;filter.replaceChildren();
 for(const [value,label] of [['','All instruments'],...pnl.instruments.map(r=>[r.product,r.product])]){const o=document.createElement('option');o.value=value;o.textContent=label;filter.append(o);}filter.value=chosen;
 const selected=pnl.instruments.find(r=>r.product===filter.value);const totals=selected||pnl.totals;
 for(const [id,key] of [['#realized-pnl','realized'],['#open-pnl','unrealized'],['#total-pnl','total']]){const el=$(id);el.textContent=money(totals[key]);el.classList.remove('gain','loss');colored(el,totals[key]);}
 const stale=totals.stale_positions,missing=totals.missing_positions;
 $('#pnl-freshness').textContent=missing?`${missing} open position(s) have no accepted price. Open and total P&L are unavailable.`:stale?`${stale} open position(s) use stale prices. Open and total P&L are last-known estimates.`:totals.open_lots?'Prices were fresh when this report was generated.':'No open positions.';
 $('#pnl-note').textContent=pnl.note;
 const rows=pnl.instruments.filter(r=>!filter.value||r.product===filter.value);$('#instrument-pnl').replaceChildren();$('#pnl-empty').hidden=rows.length>0;
 for(const r of rows){const tr=document.createElement('tr');for(const [value,key] of [[r.product,null],[r.trades,null],[r.open_lots,null],[money(r.realized),'realized'],[money(r.unrealized),'unrealized'],[money(r.total),'total'],[r.open_lots?r.mark_status:'closed',null]]){const td=document.createElement('td');td.textContent=value;if(key)colored(td,r[key]);tr.append(td);}$('#instrument-pnl').append(tr);}
 const trades=pnl.positions.filter(r=>!filter.value||r.product===filter.value);$('#positions').replaceChildren();$('#empty').hidden=trades.length>0;
 for(const p of trades){const tr=document.createElement('tr');for(const value of [`${p.symbol} · ${p.strike} ${p.option_type} · ${p.expiry}`,p.status,p.remaining,p.entry,p.stop,money(p.realized),money(p.unrealized),money(p.total),p.quote_time?`${p.bid} · ${p.quote_time} (${p.mark_status})`:'—']){const td=document.createElement('td');td.textContent=value;tr.append(td);}$('#positions').append(tr);}
}
$('#instrument-filter').addEventListener('change',()=>{if(current)renderPnl(current.pnl);});

function resetConnections(){
 for(const id of ['telegram-credentials','kotak-credentials','telegram-start','telegram-code','telegram-password','kotak-verify'])$('#'+id).reset();
 $('#channel-picker').hidden=true;
 for(const id of ['index-picker','commodity-picker'])$('#'+id).replaceChildren();
}
async function refreshConnections(){
 const d=await api('/api/connections');
 const fields=current.credentials.saved_fields;
 $('#telegram-status').textContent=`${fields.includes('telegram_api_id')&&fields.includes('telegram_api_hash')?'API details saved.':'Save API ID and hash first.'} ${d.telegram.linked?'Authorized session saved.':'Not linked.'}${d.telegram.checked_at?' Last checked: '+new Date(d.telegram.checked_at*1000).toLocaleString():''}`;
 $('#kotak-status').textContent=d.kotak.checked_at?'Authentication verified at '+new Date(d.kotak.checked_at*1000).toLocaleString()+'. This is a past check, not an active worker connection.':(fields.some(k=>k.startsWith('kotak_'))?'Credentials saved; not validated.':'Save Kotak credentials first.');
 $('#telegram-code').hidden=d.telegram.step!=='code';
 $('#telegram-password').hidden=d.telegram.step!=='password';
 $('#telegram-cancel').hidden=!['code','password'].includes(d.telegram.step);
 $('#telegram-start').hidden=d.telegram.linked||['code','password'].includes(d.telegram.step);
 $('#telegram-channels').disabled=!d.telegram.linked;
}
function connectionForm(id,fn){
 $('#'+id).addEventListener('submit',async e=>{
  e.preventDefault();const form=e.currentTarget,button=form.querySelector('button');button.disabled=true;
  const feedback=$('#'+id.split('-')[0]+'-feedback');feedback.textContent='Working…';
  try{await fn(form);feedback.textContent=$('#notice').textContent;feedback.className='ok';}catch(err){notice(err.message,true);feedback.textContent=err.message;feedback.className='error';}finally{button.disabled=false;if(current)refreshConnections().catch(()=>{});}
 });
}
for(const provider of ['telegram','kotak']){
 connectionForm(provider+'-credentials',async form=>{
  const body={};for(const key of credentialFields.filter(k=>k.startsWith(provider+'_')))if(form.elements[key].value)body[key]=form.elements[key].value.trim();
  if(!Object.keys(body).length)throw new Error('Enter credentials to save. Blank fields preserve existing values.');
  await api('/api/credentials','PUT',body);form.reset();if(provider==='telegram')$('#channel-picker').hidden=true;
  await refresh();notice(provider==='telegram'?'Telegram details saved. Connect Telegram below.':'Kotak details saved. Enter a fresh TOTP to validate.');
 });
 bind('#'+provider+'-remove',async()=>{
  if(!confirm('Remove saved '+provider+' credentials?'))return;
  await api('/api/credentials','PUT',Object.fromEntries(credentialFields.filter(k=>k.startsWith(provider+'_')).map(k=>[k,null])));
  $('#'+provider+'-credentials').reset();if(provider==='telegram')resetConnections();await refresh();notice('Saved '+provider+' credentials removed.');
 });
}
connectionForm('telegram-start',async form=>{
 await api('/api/connections/telegram/start','POST',{phone:form.phone.value.trim()});form.reset();await refreshConnections();notice('Login code requested. Check Telegram.');
});
for(const step of ['code','password'])connectionForm('telegram-'+step,async form=>{
 const value=form.elements[step].value;form.elements[step].value='';
 const d=await api('/api/connections/telegram/'+step,'POST',{[step]:value});await refresh();
 notice(d.step==='password'?'Enter your Telegram two-step password.':'Telegram linked. Click Check connection & load channels.');
});
bind('#telegram-cancel',async()=>{await api('/api/connections/telegram/cancel','POST',{});$('#telegram-code').reset();$('#telegram-password').reset();await refreshConnections();notice('Login cancelled.');});
bind('#telegram-channels',async()=>{
 const d=await api('/api/connections/telegram/channels','POST',{});
 for(const id of ['index-picker','commodity-picker']){
  const select=$('#'+id);select.replaceChildren();const empty=document.createElement('option');empty.value='';empty.textContent='Choose a channel';select.append(empty);
  for(const c of d.channels){const option=document.createElement('option');option.value=c.id;option.textContent=c.title+' ('+c.id+')';select.append(option);}
 }
 $('#channel-note').textContent=d.channels.length?d.note:'No broadcast channels found. Join your providers’ channels in Telegram, then reload.';
 $('#channel-picker').hidden=false;await refresh();notice('Telegram connection checked. Choose your channels.');
});
bind('#use-channels',async()=>{
 const index=$('#index-picker').value,commodity=$('#commodity-picker').value;
 if(!index&&!commodity)throw new Error('Choose at least one channel.');
 if(index&&index===commodity)throw new Error('Choose different channels for index and commodity formats.');
 const form=$('#settings-form');if(index)form.index_channel.value=index;if(commodity)form.commodity_channel.value=commodity;
 notice('Channel IDs filled in. Select instruments, then click Save settings.');form.scrollIntoView({behavior:'smooth',block:'start'});
});
connectionForm('kotak-verify',async form=>{
 const totp=form.totp.value;form.totp.value='';notice('Checking Kotak authentication…');
 const d=await api('/api/connections/kotak/verify','POST',{totp});await refresh();notice(d.message);
});
