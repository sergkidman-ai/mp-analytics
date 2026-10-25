// поток: ret. A newly created tab may expose only pendingUrl until navigation commits.
const WB_QR_NAV = (() => {
  function allowed(url){try{const u=new URL(url);return u.protocol==='https:'&&['www.wildberries.ru','wildberries.ru'].includes(u.hostname);}catch{return false;}}
  function summary(tab){
    const safe=u=>{try{const v=new URL(u);return {scheme:v.protocol,host:v.hostname};}catch{return null;}};
    return {status:tab.status||null,current:safe(tab.url),pending:safe(tab.pendingUrl)};
  }
  async function waitReady(tabId,timeout=45000){
    const end=Date.now()+timeout;
    while(Date.now()<end){
      const t=await chrome.tabs.get(tabId);
      if(allowed(t.url)&&t.status==='complete'&&!t.pendingUrl)return t;
      // Undefined URL/about:blank with a WB pending URL is a startup state, not wrong_host.
      if(t.url && t.url!=='about:blank' && !allowed(t.url))throw new Error('wrong_host');
      if(t.pendingUrl&&!allowed(t.pendingUrl))throw new Error('wrong_host');
      await new Promise(r=>setTimeout(r,250));
    }
    throw new Error('navigation_timeout');
  }
  return {allowed,summary,waitReady};
})();
