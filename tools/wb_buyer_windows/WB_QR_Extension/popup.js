// поток: ret. User-click screenshot only. No cookie access, network or background job.
const captureButton = document.getElementById('capture');
const status = document.getElementById('status');
const preview = document.getElementById('preview');
const download = document.getElementById('download');

function isBuyerPage(tab) {
  try {
    const url = new URL(tab.url);
    return url.protocol === 'https:' && ['wildberries.ru', 'www.wildberries.ru'].includes(url.hostname);
  } catch (_) {
    return false;
  }
}

function currentTab() {
  return new Promise((resolve, reject) => {
    chrome.tabs.query({active: true, currentWindow: true}, tabs => {
      if (chrome.runtime.lastError || tabs.length !== 1) {
        reject(new Error('Не удалось определить вкладку. Откройте WB и нажмите значок расширения ещё раз.'));
      } else {
        resolve(tabs[0]);
      }
    });
  });
}

function takeScreen(windowId) {
  return new Promise((resolve, reject) => {
    chrome.tabs.captureVisibleTab(windowId, {format: 'png'}, data => {
      if (chrome.runtime.lastError || !data || !data.startsWith('data:image/png;base64,iVBORw0KGgo')) {
        reject(new Error('Браузер не дал сохранить экран. Откройте WB и нажмите значок расширения заново.'));
      } else {
        resolve(data);
      }
    });
  });
}

// Crop original pixels, keeping QR quiet zones and the short code below the WB QR.
async function cropPickupCard(data) {
  const img = new Image();
  img.src = data;
  await img.decode();
  const source = document.createElement('canvas');
  source.width = img.naturalWidth;
  source.height = img.naturalHeight;
  const ctx = source.getContext('2d', {willReadFrequently:true});
  ctx.drawImage(img, 0, 0);
  const pixels = ctx.getImageData(0, 0, source.width, source.height);
  const qr = jsQR(pixels.data, pixels.width, pixels.height);
  if (!qr) throw new Error('QR не найден. Откройте его крупно и повторите снимок.');
  const corners = ['topLeftCorner','topRightCorner','bottomLeftCorner','bottomRightCorner'].map(k=>qr.location[k]);
  const left = Math.min(...corners.map(p=>p.x));
  const right = Math.max(...corners.map(p=>p.x));
  const top = Math.min(...corners.map(p=>p.y));
  const bottom = Math.max(...corners.map(p=>p.y));
  const size = Math.max(right-left, bottom-top);
  const margin = Math.ceil(size * .18);
  // Never invent the numeric code: keep the original strip beneath the QR.
  const x = Math.max(0, Math.floor(left-margin));
  const y = Math.max(0, Math.floor(top-margin));
  const w = Math.min(source.width, Math.ceil(right+margin))-x;
  const h = Math.min(source.height, Math.ceil(bottom+size*.50))-y;
  const output = document.createElement('canvas');
  const scale = Math.max(1, Math.ceil(600/w));
  output.width = w*scale;
  output.height = h*scale;
  const out = output.getContext('2d', {willReadFrequently:true});
  out.imageSmoothingEnabled = false;
  out.drawImage(source,x,y,w,h,0,0,output.width,output.height);
  const checkPixels = out.getImageData(0,0,output.width,output.height);
  const check = jsQR(checkPixels.data,checkPixels.width,checkPixels.height);
  if (!check || check.binaryData.join(',') !== qr.binaryData.join(',')) {
    throw new Error('Не удалось проверить QR после обрезки. Сохранение отменено.');
  }
  return output.toDataURL('image/png');
}

captureButton.addEventListener('click', async () => {
  captureButton.disabled = true;
  preview.hidden = true;
  download.hidden = true;
  preview.removeAttribute('src');
  download.removeAttribute('href');
  status.textContent = 'Снимаю видимую часть вкладки…';
  try {
    const before = await currentTab();
    if (!isBuyerPage(before)) {
      throw new Error('Откройте покупательский сайт wildberries.ru с QR, затем нажмите значок расширения.');
    }
    const data = await takeScreen(before.windowId);
    const after = await currentTab();
    if (before.id !== after.id || !isBuyerPage(after)) {
      throw new Error('Вкладка переключилась. Снимок отменён; повторите на вкладке WB.');
    }
    const cropped = await cropPickupCard(data);
    preview.src = cropped;
    preview.hidden = false;
    download.href = cropped;
    download.hidden = false;
    status.textContent = 'QR обрезан и проверен. Проверьте цифровой код под ним и нажмите «Сохранить PNG». Затем передайте файл в дропбокс-бот.';
  } catch (error) {
    status.textContent = error.message;
  } finally {
    captureButton.disabled = false;
  }
});

// A durable extension page owns the test; popup closing does not cancel extraction.
document.getElementById('autotest').addEventListener('click', async () => {
  try {
    const tab = await currentTab();
    if (!isBuyerPage(tab)) throw new Error('Откройте заказы в кабинете Дисквэра на wildberries.ru.');
    await chrome.tabs.create({url:chrome.runtime.getURL('runner.html')+'?tab='+tab.id});
  } catch (error) { status.textContent = error.message; }
});

async function showSchedule(){
  try{
    const s=await chrome.runtime.sendMessage({type:'schedule-state'});
    document.getElementById('schedule-status').textContent=s?.config?.enabled?
      'Сбор включён. Ближайший запуск: '+new Date(s.next).toLocaleString('ru-RU',{timeZone:'Europe/Moscow'})+' МСК.'+
      (s.run?' Сейчас выполняется: '+s.run.stage+'.':'')+
      (s.lastResult?' Последний результат: '+(s.lastResult.ok?'QR передан в отчёт.':'Ошибка получения/передачи QR; проверьте кабинет и подключение.'):''):
      'Сбор по расписанию выключен.';
  }catch{document.getElementById('schedule-status').textContent='Не удалось прочитать расписание.';}
}
document.getElementById('enable-schedule').addEventListener('click',async()=>{
  try{
    const tab=await currentTab();if(!isBuyerPage(tab))throw new Error('Откройте заказы Дисквэра в WB.');
    const result=await chrome.runtime.sendMessage({type:'schedule-enable',tabId:tab.id,uploadToken:document.getElementById('upload-token').value.trim()});
    document.getElementById('upload-token').value='';
    if(!result?.ok)throw new Error(result?.error||'Не удалось включить сбор.');
    await showSchedule();
  }catch(error){document.getElementById('schedule-status').textContent=error.message;}
});
document.getElementById('disable-schedule').addEventListener('click',async()=>{
  await chrome.runtime.sendMessage({type:'schedule-disable'});await showSchedule();
});
showSchedule();

document.getElementById('test-transfer').addEventListener('click',async()=>{
  const r=await chrome.runtime.sendMessage({type:'schedule-test'});
  document.getElementById('schedule-status').textContent=r?.ok?'Проверка запущена. После успеха на значке будет OK; отдельной рассылки не будет.':(r?.error||'Ошибка запуска.');
});

document.getElementById('diagnostic').addEventListener('click',async()=>{
  const report=await chrome.runtime.sendMessage({type:'schedule-diagnostic'});
  const link=document.getElementById('diagnostic-download');
  link.href=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:'application/json'}));link.hidden=false;
});

if(new Date(Date.now()+10800000).toISOString().slice(0,10)>'2026-10-25')document.getElementById('oneoff-test').hidden=true;
