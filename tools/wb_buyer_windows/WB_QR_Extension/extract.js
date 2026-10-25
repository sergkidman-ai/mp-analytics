// поток: ret. Runs in an isolated world on the two exact WB buyer hosts only.
(async () => {
  const diagnostics = {version:4, hostAllowed:false, clickedQr:false, navigationRequested:false,
    qrControls:0, scopes:0, assets:0, inaccessibleAssets:0, candidateImages:0};
  const fail = reason => ({ok:false, reason, diagnostics});
  if (location.protocol !== 'https:' || !['wildberries.ru','www.wildberries.ru'].includes(location.hostname)) return fail('wrong_host');
  diagnostics.hostAllowed=true;
  const visible = el => {
    const r=el.getBoundingClientRect(),s=getComputedStyle(el);
    return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden' && s.opacity!=='0';
  };
  const norm = s => (s||'').replace(/\s+/g,' ').trim();
  const qrWord = /qr[\s\-–]?код/i;
  const pageBlocked = () => {
    const t=(document.body?.innerText||'').slice(0,30000);
    return /проверяем браузер|подозрительная активность|подтвердите.{0,20}не робот/i.test(t) ||
      [...document.querySelectorAll('input[type="tel"], input[autocomplete="one-time-code"]')].some(visible);
  };
  if (pageBlocked()) return fail('login_or_protection');
  function scopes() {
    const nodes=[...document.querySelectorAll('[role="dialog"],dialog,[aria-modal="true"],[class*="popup"],[class*="modal"]')];
    // Avoid collecting the full order page: only small visible QR-specific containers.
    return [...new Set(nodes)].filter(el=>visible(el) && qrWord.test(el.innerText||'') &&
      (el.innerText||'').length<6000 && /код|получател|пункт.{0,10}выдачи/i.test(el.innerText||''));
  }
  let containers=scopes();
  if (!containers.length) {
    const controls=[...document.querySelectorAll('button,a,[role="button"],span,div')].filter(el=>{
      if (!visible(el)) return false;
      const text=norm(el.getAttribute('aria-label') || el.getAttribute('title') || el.innerText);
      return text.length>0 && text.length<90 && qrWord.test(text) &&
        !/украсить|скачать|скопировать|оплат|поделиться|возврат/i.test(text) &&
        /^(?:(?:показать|получить|открыть|мой|ваш)\s+)?qr[\s\-–]?код(?:\s+(?:для\s+)?(?:получения|заказа|заказов))?$/i.test(text);
    }).map(el=>el.closest('button,a,[role="button"]') || el);
    const unique=[...new Set(controls)];
    // Nested label and button are the same action, not two different QR actions.
    const candidates=unique.filter(el=>!unique.some(other=>other!==el && other.contains(el)));
    diagnostics.qrControls=candidates.length;
    if (candidates.length===1) {
      const c=candidates[0];
      if (c.tagName==='A') {
        const u=new URL(c.href,location.href);
        if (u.origin!==location.origin || u.pathname!==location.pathname || u.search!==location.search) return fail('qr_link_requires_navigation');
      }
      c.click();diagnostics.clickedQr=true;
    } else if (candidates.length>1) return fail('ambiguous_qr_controls');
    else {
      const orders=[...document.querySelectorAll('a[href]')].filter(el=>visible(el) && /^(?:заказы|доставки)$/i.test(norm(el.innerText))).map(el=>{
        try {const u=new URL(el.href,location.href);return u.origin===location.origin && /^\/lk\//.test(u.pathname) && !u.search ? u.href : null;}catch{return null;}
      }).filter(Boolean);
      const urls=[...new Set(orders)];
      if(urls.length===1 && urls[0]!==location.href) return {ok:false,reason:'navigate_orders',navigate:urls[0],diagnostics};
      return fail('qr_control_not_found');
    }
    const deadline=Date.now()+15000;
    while(Date.now()<deadline) {
      if(pageBlocked()) return fail('login_or_protection');
      containers=scopes();if(containers.length)break;
      await new Promise(r=>setTimeout(r,250));
    }
  }
  diagnostics.scopes=containers.length;
  if(!containers.length) return fail('qr_window_not_found');
  const candidates=[];
  const seen=new Set();
  for(const scope of containers) {
    const text=norm(scope.innerText);
    let codes=[...text.matchAll(/\b(\d{3})[\s\u00a0]?(\d{3})\b/g)].map(m=>m[1]+m[2]);
    codes=[...new Set(codes)];
    const code=codes.length===1?codes[0]:null;
    // Keep only the validity sentence, never full page/order text or URLs.
    const validity=(text.match(/(?:коды?\s+действу[ею]т|действител[еьн])[\s\S]{0,130}?(?:мск|москве)/i)||[])[0]||null;
    const assets=[...scope.querySelectorAll('img,canvas,svg')].filter(el=>{
      const r=el.getBoundingClientRect();return visible(el) && r.width>=90 && r.height>=90 && r.width/r.height>.5 && r.width/r.height<2;
    });
    diagnostics.assets+=assets.length;
    for(const el of assets.slice(0,12)) {
      try {
        let image=el;
        if(el.tagName.toLowerCase()==='svg') {
          const clone=el.cloneNode(true);
          clone.querySelectorAll('script,foreignObject').forEach(n=>n.remove());
          for(const n of [clone,...clone.querySelectorAll('*')]) for(const a of [...n.attributes]) {
            if(/^on/i.test(a.name) || /(?:href|src)$/i.test(a.name) && !a.value.startsWith('#')) n.removeAttribute(a.name);
          }
          clone.setAttribute('xmlns','http://www.w3.org/2000/svg');
          image=new Image();image.src='data:image/svg+xml;charset=utf-8,'+encodeURIComponent(new XMLSerializer().serializeToString(clone));await image.decode();
        } else if(el.tagName==='IMG') await el.decode();
        const w=image.naturalWidth||image.width?.baseVal?.value||image.width;
        const h=image.naturalHeight||image.height?.baseVal?.value||image.height;
        if(!(w>=90 && h>=90) || w*h>16000000) continue;
        const c=document.createElement('canvas');c.width=w;c.height=h;
        const x=c.getContext('2d');x.fillStyle='white';x.fillRect(0,0,w,h);x.drawImage(image,0,0,w,h);
        const png=c.toDataURL('image/png');
        if(!seen.has(png)) {seen.add(png);candidates.push({png,code,validity});}
      } catch {diagnostics.inaccessibleAssets++;}
    }
  }
  diagnostics.candidateImages=candidates.length;
  if(!candidates.length) return fail('qr_image_unavailable');
  return {ok:true,candidates,diagnostics};
})()
