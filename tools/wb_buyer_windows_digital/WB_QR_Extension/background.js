// поток: ret. Scheduled capture and narrow upload; Telegram credentials stay on the server.
importScripts('schedule.js','navigation.js');
const DAILY='wb-qr-09-msk',RETRY='wb-qr-retry',WATCH='wb-qr-watch';
let serial=Promise.resolve();
function enqueue(fn){const result=serial.then(fn,fn);serial=result.catch(()=>{});return result;}
async function state(){return chrome.storage.local.get(['config','lastSuccessDay','run','attemptDay','attempts','lastResult','uploadToken','history']);}
async function badge(text){await chrome.action.setBadgeText({text});await chrome.action.setBadgeBackgroundColor({color:text==='!'?'#b3261e':'#277544'});}
function allowedUrl(value){try {const u=new URL(value);return u.protocol==='https:'&&['www.wildberries.ru','wildberries.ru'].includes(u.hostname)&&/^\/lk(?:\/|$)/.test(u.pathname)&&!u.search&&!u.hash;}catch{return false;}}
async function plan(){const s=await state();if(s.config?.enabled)await chrome.alarms.create(DAILY,{when:WB_QR_SCHEDULE.next(Date.now())});else await chrome.alarms.clear(DAILY);}
async function sessionId(){
  const s=await chrome.storage.session.get('browserSessionId');
  if(s.browserSessionId)return s.browserSessionId;
  const id=crypto.randomUUID();await chrome.storage.session.set({browserSessionId:id});return id;
}
async function cleanup(run){
  if(!run)return;
  await chrome.storage.session.remove('input:'+run.id);
  // IDs can be reused after browser restart; never close tabs from an older session.
  if(!run.sessionId || run.sessionId!==await sessionId())return;
  for(const tabId of [run.runnerTab,run.wbTab])if(tabId)try{
    const t=await chrome.tabs.get(tabId);
    if(t.windowId!==run.windowId)continue;
    const runner=tabId===run.runnerTab && t.url===chrome.runtime.getURL('runner.html')+'?tab='+run.wbTab+'&scheduled='+run.id;
    const source=tabId===run.wbTab && WB_QR_NAV.allowed(t.url||t.pendingUrl);
    if(runner||source)await chrome.tabs.remove(tabId);
  }catch{}
}
async function setStage(run,stage){run.stage=stage;await chrome.storage.local.set({run});}
async function extractFromSource(run){
  let extracted;
  for(let route=0;route<2;route++){
    await setStage(run,'wait_source_navigation');
    await WB_QR_NAV.waitReady(run.wbTab);
    await setStage(run,'extract_qr');
    const deadline=Date.now()+12000;
    do{
      const result=await chrome.scripting.executeScript({target:{tabId:run.wbTab},files:['extract.js']});
      extracted=result[0]?.result;
      if(extracted?.reason!=='qr_control_not_found')break;
      await new Promise(r=>setTimeout(r,500));
    }while(Date.now()<deadline);
    if(!extracted)throw new Error('extraction_failed');
    if(extracted.reason!=='navigate_orders')break;
    const before=await chrome.tabs.get(run.wbTab),u=new URL(extracted.navigate);
    if(route || !WB_QR_NAV.allowed(u.href) || u.origin!==new URL(before.url).origin || !/^\/lk\//.test(u.pathname) || u.search)throw new Error('wrong_host');
    await chrome.tabs.update(run.wbTab,{url:u.href});
  }
  return extracted;
}
async function record(result){
  const s=await state();await chrome.storage.local.set({lastResult:result,history:[...(s.history||[]),result].slice(-20)});
}
async function failRun(reason) {
  const s=await state();if(!s.run)return;
  await chrome.storage.local.set({run:null});
  await record({ok:false,reason,stage:s.run.stage||null,at:new Date().toISOString()});
  await cleanup(s.run);
  await chrome.alarms.clear(WATCH);await badge('!');
  const d=WB_QR_SCHEDULE.day(Date.now());
  if(s.config?.enabled && d.enabled && d.key===s.run.day && (s.attempts||0)<3)
    await chrome.alarms.create(RETRY,{when:Date.now()+60*1000});
}
async function startIfDue(force=false) {
  const s=await state(),now=Date.now(),today=WB_QR_SCHEDULE.day(now);
  if(s.run){if(now-s.run.startedAt>2*60*1000){await failRun('run_timeout');return startIfDue(force);}return;}
  if(!s.config?.enabled || !today.enabled)return;
  if(!force && !WB_QR_SCHEDULE.eligible(now,s.config,s.lastSuccessDay))return;
  if(!force && s.attemptDay===today.key && (s.attempts||0)>=3)return;
  if(!allowedUrl(s.config.targetUrl)){await badge('!');return;}
  const run={id:crypto.randomUUID(),day:today.key,startedAt:now,sessionId:await sessionId(),wbTab:null,runnerTab:null,windowId:null,stage:'create_window'};
  await chrome.storage.local.set({run,attemptDay:today.key,attempts:s.attemptDay===today.key?(s.attempts||0)+1:1});
  await chrome.alarms.create(WATCH,{when:now+2*60*1000});
  try{
    // Use a selected, visible source page. Background tabs may be deferred by Chrome.
    const window=await chrome.windows.create({url:s.config.targetUrl,type:'popup',focused:true,width:1200,height:900});
    run.windowId=window.id;run.wbTab=window.tabs?.[0]?.id;
    if(!run.wbTab)throw new Error('source_tab_missing');
    await chrome.storage.local.set({run});await badge('…');
    const extracted=await extractFromSource(run);
    await chrome.storage.session.set({['input:'+run.id]:extracted});
    await setStage(run,'render_qr');
    // Create the processing page only after WB has loaded and extraction has finished.
    const runner=await chrome.tabs.create({windowId:run.windowId,url:chrome.runtime.getURL('runner.html')+'?tab='+run.wbTab+'&scheduled='+run.id,active:true});
    run.runnerTab=runner.id;await chrome.storage.local.set({run});
  }catch(error){
    const known=['wrong_host','navigation_timeout','source_tab_missing','extraction_failed'];
    await failRun(known.includes(error.message)?error.message:'browser_error');
  }
}
async function recover(){
  const version=await chrome.storage.local.get('schemaVersion');
  if(version.schemaVersion!==6){
    const old=await state();
    await chrome.storage.local.set({schemaVersion:6,run:null,attemptDay:null,attempts:0});
    for(const name of [RETRY,WATCH])await chrome.alarms.clear(name);
    await cleanup(old.run);
  }
  const pending=await state();
  if(pending.run){
    if(pending.run.sessionId!==await sessionId())await failRun('browser_restarted');
    else if(!await chrome.alarms.get(WATCH))await chrome.alarms.create(WATCH,{when:Math.max(Date.now()+1000,pending.run.startedAt+2*60*1000)});
  }
  await plan();await startIfDue();
}
chrome.runtime.onInstalled.addListener(()=>enqueue(recover));
chrome.runtime.onStartup.addListener(()=>enqueue(recover));
chrome.alarms.onAlarm.addListener(alarm=>enqueue(async()=>{
  if(alarm.name===DAILY){await plan();await startIfDue();}
  if(alarm.name===RETRY)await startIfDue();
  if(alarm.name===WATCH)await failRun('run_timeout');
}));
chrome.runtime.onMessage.addListener((message,sender,reply)=>{
  if(sender.id!==chrome.runtime.id)return;
  enqueue(async()=>{
    if(message.type==='schedule-diagnostic'){
      const s=await state();return {version:6,at:new Date().toISOString(),enabled:Boolean(s.config?.enabled),next:s.config?.enabled?new Date(WB_QR_SCHEDULE.next(Date.now())).toISOString():null,lastSuccessDay:s.lastSuccessDay||null,run:s.run?{day:s.run.day,stage:s.run.stage}:null,lastResult:s.lastResult||null,history:s.history||[]};
    }
    if(message.type==='schedule-state'){
      const s=await state();return {ok:true,config:s.config||null,lastResult:s.lastResult||null,run:s.run?{day:s.run.day,stage:s.run.stage}:null,next:s.config?.enabled?WB_QR_SCHEDULE.next(Date.now()):null};
    }
    if(message.type==='schedule-enable'){
      const tab=await chrome.tabs.get(message.tabId),u=new URL(tab.url);
      u.search='';u.hash='';
      if(!allowedUrl(u.href))return {ok:false,error:'Откройте раздел заказов Цифрового в кабинете WB.'};
      const s=await state();if(s.run)return {ok:false,error:'Дождитесь завершения текущего запуска.'};
      if(typeof message.uploadToken!=='string'||!/^[-_A-Za-z0-9]{32,128}$/.test(message.uploadToken))return {ok:false,error:'Вставьте код подключения со страницы Пульта.'};
      await chrome.storage.local.set({uploadToken:message.uploadToken,config:{enabled:true,targetUrl:u.href,enabledAt:Date.now()}});
      await plan();await badge('');return {ok:true,next:WB_QR_SCHEDULE.next(Date.now())};
    }
    if(message.type==='schedule-test'){
      const s=await state();if(!s.config?.enabled)return {ok:false,error:'Сначала включите сбор.'};
      if(s.run)return {ok:false,error:'Запуск уже выполняется.'};
      if(!WB_QR_SCHEDULE.day(Date.now()).enabled)return {ok:false,error:'По воскресеньям сбор отключён.'};
      await startIfDue(true);return {ok:true};
    }
    if(message.type==='schedule-disable'){
      await chrome.storage.local.set({config:{enabled:false}});
      for(const name of [DAILY,RETRY])await chrome.alarms.clear(name);
      await badge('');return {ok:true};
    }
    if(message.type==='scheduled-input'){
      const s=await state(),run=s.run;
      if(!run||run.id!==message.runId||sender.tab?.id!==run.runnerTab)return {ok:false};
      const input=await chrome.storage.session.get('input:'+run.id);
      return {ok:true,extracted:input['input:'+run.id]};
    }
    if(message.type==='scheduled-result'){
      const s=await state(),run=s.run;
      if(!run || run.id!==message.runId || sender.tab?.id!==run.runnerTab)return {ok:false};
      if(!message.ok){await failRun(message.reason||'capture_failed');return {ok:false};}
      if(!s.config?.enabled || !WB_QR_SCHEDULE.day(Date.now()).enabled || run.day!==WB_QR_SCHEDULE.day(Date.now()).key){await failRun('schedule_cancelled');return {ok:false};}
      if(typeof message.png!=='string'||!message.png.startsWith('data:image/png;base64,')||message.png.length>8000000){await failRun('invalid_png');return {ok:false};}
      if(!message.validToday){await failRun('validity_not_today');return {ok:false};}
      try {
        await setStage(run,'upload_qr');
        const controller=new AbortController();
        const timeout=setTimeout(()=>controller.abort(),12000);
        let response;
        try {response=await fetch('https://bi.metaverseworld.ru/api/returns/wb-qr/tsifrovoy',{
          method:'POST',headers:{'Authorization':'Bearer '+s.uploadToken,'Content-Type':'application/json'},
          credentials:'omit',cache:'no-store',redirect:'error',signal:controller.signal,
          body:JSON.stringify({png_b64:message.png.split(',')[1],day:run.day,validity:message.validity})
        });}finally{clearTimeout(timeout);}
        if(!response.ok)throw new Error('upload_failed');
        const ack=await response.json();if(!ack.ok||ack.day!==run.day)throw new Error('upload_failed');
        await completeUpload(run.id);
      }catch{await failRun('upload_failed');return {ok:false};}
      return {ok:true};
    }
    return {ok:false};
  }).then(reply,()=>reply({ok:false,error:'Ошибка браузера; откройте результат теста.'}));
  return true;
});
async function completeUpload(runId){
  const s=await state(),run=s.run;if(!run||run.id!==runId)return;
  await chrome.storage.local.set({lastSuccessDay:run.day,run:null});
  await record({ok:true,day:run.day,at:new Date().toISOString(),delivery:'returns_report'});
  await chrome.alarms.clear(WATCH);await chrome.alarms.clear(RETRY);await badge('OK');
  await cleanup(run);
}
// Alarms may be cleared by a browser update; restore whenever the worker starts.
chrome.storage.local.setAccessLevel({accessLevel:'TRUSTED_CONTEXTS'}).catch(()=>{});
enqueue(recover);
