// поток: ret. Moscow civil time, independent of the Windows timezone.
const WB_QR_SCHEDULE = (() => {
  const MSK=3*60*60*1000,DAY=24*60*60*1000;
  function day(now) {
    const d=new Date(now+MSK);
    const key=d.toISOString().slice(0,10);
    const sunday=d.getUTCDay()===0;
    const oneOffTest=key==='2026-10-25';
    return {key,sunday,oneOffTest,enabled:!sunday||oneOffTest,due:Date.parse(key+'T06:00:00Z')};
  }
  function next(now) {
    let due=day(now).due;
    if(due<=now)due+=DAY;
    while(!day(due).enabled)due+=DAY;
    return due;
  }
  function eligible(now,config,lastDay) {
    const d=day(now);
    return Boolean(config?.enabled && d.enabled && now>=d.due && d.due>=config.enabledAt && lastDay!==d.key);
  }
  function currentValidity(text,now) {
    if(!text)return false;
    const months=['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря'];
    const match=text.toLowerCase().match(/\b(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\b/);
    // Russian letters are not JS word characters: do not use a trailing \b for month names.
    const m=match||text.toLowerCase().match(/\b(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)(?=\s|$)/);
    const d=new Date(now+MSK);
    return Boolean(m && Number(m[1])===d.getUTCDate() && months.indexOf(m[2])===d.getUTCMonth() && /до\s+00:00/.test(text) && /мск|москв/i.test(text));
  }
  return {day,next,eligible,currentValidity};
})();
