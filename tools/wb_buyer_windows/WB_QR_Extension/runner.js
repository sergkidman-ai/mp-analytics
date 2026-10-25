// поток: ret. Manual DOM test or rendering of a completed scheduled extraction.
const state=document.getElementById('status');
const scheduledRun=new URLSearchParams(location.search).get("scheduled");
const report={version:6,at:new Date().toISOString(),mode:scheduledRun?'scheduled':'manual',stage:'start',openedAutomatically:false,success:false,steps:[]};
const reasons={wrong_host:'Открыта другая страница.',login_or_protection:'WB требует вход или проверку. Пройдите её вручную в кабинете.',
  ambiguous_qr_controls:'Найдено несколько кнопок QR. Откройте раздел заказов нужного аккаунта.',qr_control_not_found:'Кнопка QR не найдена. Нужен отчёт для настройки под страницу WB.',
  qr_window_not_found:'После нажатия кнопки окно QR не появилось.',qr_link_requires_navigation:'Кнопка QR ведёт на другую страницу; маршрут требует проверки.',
  qr_image_unavailable:'Не удалось извлечь картинку из окна QR. Нужен отчёт теста.',changed_tab:'Страница WB изменилась; результат отменён.'};
function wb(url) {try {const u=new URL(url);return u.protocol==='https:'&&['wildberries.ru','www.wildberries.ru'].includes(u.hostname);}catch{return false;}}
async function decode(png) {
  const img=new Image();img.src=png;await img.decode();
  const c=document.createElement('canvas');c.width=img.width;c.height=img.height;
  const x=c.getContext('2d',{willReadFrequently:true});x.drawImage(img,0,0);
  const p=x.getImageData(0,0,c.width,c.height),qr=jsQR(p.data,p.width,p.height);
  return {img,qr};
}
async function formatCard(img,qr,code) {
  const points=['topLeftCorner','topRightCorner','bottomLeftCorner','bottomRightCorner'].map(k=>qr.location[k]);
  const left=Math.max(0,Math.floor(Math.min(...points.map(p=>p.x))));
  const top=Math.max(0,Math.floor(Math.min(...points.map(p=>p.y))));
  const right=Math.min(img.width,Math.ceil(Math.max(...points.map(p=>p.x))));
  const bottom=Math.min(img.height,Math.ceil(Math.max(...points.map(p=>p.y))));
  const w=right-left,h=bottom-top;
  const scale=Math.max(1,Math.ceil(520/Math.max(w,h))),margin=Math.ceil(Math.max(w,h)*.18)*scale;
  const c=document.createElement('canvas');c.width=w*scale+margin*2;c.height=h*scale+margin*2+(code?110:0);
  const x=c.getContext('2d');x.fillStyle='white';x.fillRect(0,0,c.width,c.height);
  x.imageSmoothingEnabled=false;x.drawImage(img,left,top,w,h,margin,margin,w*scale,h*scale);
  if(code){x.fillStyle='black';x.textAlign='center';x.font='bold 52px sans-serif';x.fillText(code.slice(0,3)+' '+code.slice(3),c.width/2,c.height-32);}
  const png=c.toDataURL('image/png'),check=await decode(png);
  if(!check.qr || check.qr.binaryData.join(',')!==qr.binaryData.join(','))throw new Error('crop_validation_failed');
  return png;
}
function publishReport() {
  const link=document.getElementById('report');
  link.href=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:'application/json'}));link.hidden=false;
}
(async()=>{
  try {
    const tabId=Number(new URLSearchParams(location.search).get('tab'));
    if(!Number.isInteger(tabId)||tabId<=0)throw new Error('wrong_host');
    let extracted;
    if(scheduledRun){
      report.stage='receive_extraction';
      const input=await chrome.runtime.sendMessage({type:'scheduled-input',runId:scheduledRun});
      if(!input?.ok)throw new Error('schedule_run_inactive');
      extracted=input.extracted;
      if(!extracted)throw new Error('extraction_failed');
      report.steps.push(extracted.diagnostics);
      report.openedAutomatically=Boolean(extracted.diagnostics?.clickedQr);
      if(!extracted.ok)throw new Error(extracted.reason);
    }else{
      report.stage='wait_source_navigation';
      await WB_QR_NAV.waitReady(tabId);
    let before=await chrome.tabs.get(tabId);
    if(!wb(before.url))throw new Error('wrong_host');
    // At most one safe same-origin navigation to a discovered orders link.

    for(let attempt=0;attempt<2;attempt++) {
      const result=await chrome.scripting.executeScript({target:{tabId},files:['extract.js']});
      extracted=result[0]?.result;
      if(!extracted)throw new Error('extraction_failed');
      report.steps.push(extracted.diagnostics);
      report.openedAutomatically ||= Boolean(extracted.diagnostics?.clickedQr);
      if(extracted.reason!=='navigate_orders')break;
      const next=new URL(extracted.navigate),current=new URL(before.url);
      if(attempt || next.origin!==current.origin || !wb(next.href) || !/^\/lk\//.test(next.pathname) || next.search)throw new Error('wrong_host');
      await chrome.tabs.update(tabId,{url:next.href});
      let loaded=false;
      for(let n=0;n<80;n++) {
        await new Promise(r=>setTimeout(r,250));
        const t=await chrome.tabs.get(tabId);
        if(!wb(t.url))throw new Error('changed_tab');
        if(t.status==='complete' && new URL(t.url).pathname===next.pathname){loaded=true;before=t;break;}
      }
      if(!loaded)throw new Error('navigation_timeout');
    }
    if(!extracted.ok)throw new Error(extracted.reason);
    const after=await chrome.tabs.get(tabId);
    if(!wb(after.url) || new URL(after.url).origin!==new URL(before.url).origin || new URL(after.url).pathname!==new URL(before.url).pathname)throw new Error('changed_tab');
    }
    report.stage='decode_qr';
    const decoded=[];
    for(const candidate of extracted.candidates){
      const d=await decode(candidate.png);if(d.qr)decoded.push({...candidate,...d});
    }
    if(!decoded.length)throw new Error('qr_not_decoded');
    const payloads=new Set(decoded.map(c=>c.qr.binaryData.join(',')));
    if(payloads.size!==1)throw new Error('ambiguous_qr_images');
    const codes=[...new Set(decoded.map(c=>c.code).filter(Boolean))];
    if(codes.length>1)throw new Error('ambiguous_pickup_codes');
    const chosen=decoded.find(c=>c.code)||decoded[0];
    if(scheduledRun && (!codes.length || !WB_QR_SCHEDULE.currentValidity(chosen.validity,Date.now())))throw new Error('validity_not_today');
    const png=await formatCard(chosen.img,chosen.qr,codes[0]||null);
    document.getElementById('result').src=png;document.getElementById('result').hidden=false;
    document.getElementById('save').href=png;document.getElementById('save').hidden=false;
    report.success=true;report.numericCodeFound=Boolean(codes.length);report.validityFound=Boolean(chosen.validity);
    report.validity=chosen.validity;
    state.textContent=(report.openedAutomatically?'Окно QR открыто автоматически. ':'Окно QR уже было открыто: автоматическое открытие ещё не проверено. ')+
      'Изображение извлечено, QR обрезан и проверен. '+(codes.length?'Цифровой код добавлен со страницы. ':'Цифровой код не определён — проверьте его в WB. ')+
      (chosen.validity?' Срок из WB: '+chosen.validity:' Срок действия автоматически не определён.');
    if(scheduledRun){
      report.stage='upload_qr';
      const ack=await chrome.runtime.sendMessage({type:'scheduled-result',runId:scheduledRun,ok:true,png,validToday:true,validity:chosen.validity});
      if(!ack?.ok){report.success=false;report.error='upload_failed';state.textContent='Не удалось передать QR в отчёт. Проверьте подключение к серверу.';}else{report.stage='complete';}
    }
  }catch(error){
    const known=[...Object.keys(reasons),'qr_not_decoded','ambiguous_qr_images','ambiguous_pickup_codes','crop_validation_failed','extraction_failed','navigation_timeout','validity_not_today','schedule_run_inactive'];
    report.error=known.includes(error.message)?error.message:'browser_error';
    state.textContent='Тест остановлен. '+(reasons[report.error]||'Не удалось надёжно получить QR. Сохраните отчёт теста и передайте в дропбокс.');
    if(scheduledRun)await chrome.runtime.sendMessage({type:'scheduled-result',runId:scheduledRun,ok:false,reason:report.error}).catch(()=>{});
  }finally{publishReport();}
})();
