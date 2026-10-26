// поток: ret. Execute the real MV3 worker with an isolated Chrome API and clock.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const ext=path.join(__dirname,'../tools/wb_buyer_windows/WB_QR_Extension');
const RealDate=Date;let now=Date.parse('2026-10-26T06:20:00Z');
class Clock extends RealDate {constructor(...a){super(...(a.length?a:[now]));}static now(){return now;}}
const values={schemaVersion:7,config:{enabled:true,enabledAt:Date.parse('2026-10-25T06:00:00Z'),targetUrl:'https://www.wildberries.ru/lk/orders'},uploadToken:'isolated-fixture-token',lastSuccessDay:'2026-10-26'};
const session={},alarms=new Map();let windows=0,alarmListener;
function storage(data){return {
  get:async names=>Object.fromEntries((Array.isArray(names)?names:[names]).filter(k=>k in data).map(k=>[k,data[k]])),
  set:async items=>Object.assign(data,items),remove:async k=>{delete data[k];},setAccessLevel:async()=>{}
};}
const chrome={storage:{local:storage(values),session:storage(session)},
  action:{setBadgeText:async()=>{},setBadgeBackgroundColor:async()=>{}},
  alarms:{create:async(n,a)=>alarms.set(n,a),clear:async n=>alarms.delete(n),get:async n=>alarms.get(n),onAlarm:{addListener:f=>{alarmListener=f;}}},
  runtime:{id:'fixture',onInstalled:{addListener:()=>{}},onStartup:{addListener:()=>{}},onMessage:{addListener:()=>{}},getURL:p=>'chrome-extension://fixture/'+p},
  windows:{create:async()=>{windows++;throw Error('isolated_fixture_failure');}},
  tabs:{get:async()=>{throw Error('missing');},remove:async()=>{throw Error('must_not_close_user_tab');}}
};
const context=vm.createContext({chrome,Date:Clock,URL,crypto:require('node:crypto').webcrypto,setTimeout,clearTimeout,AbortController});
context.importScripts=(...names)=>names.forEach(n=>vm.runInContext(fs.readFileSync(path.join(ext,n),'utf8'),context));
vm.runInContext(fs.readFileSync(path.join(ext,'background.js'),'utf8'),context);
(async()=>{
  await vm.runInContext('serial',context);
  assert.equal(windows,0);assert.equal(alarms.has('wb-qr-hourly'),false);
  Object.assign(values,{lastSuccessDay:null,attemptDay:'2026-10-26',attemptHour:'2026-10-26T09',attempts:3});
  await vm.runInContext('enqueue(recover)',context);
  assert.equal(windows,0,'exhausted current hour must wait');
  assert.equal(alarms.get('wb-qr-hourly').when,Date.parse('2026-10-26T07:00:00Z'));
  now=Date.parse('2026-10-26T07:00:00Z');
  await alarmListener({name:'wb-qr-hourly'});
  assert.equal(windows,1,'next hour must retry even after 3 earlier failures');
  assert.equal(values.attempts,1);assert.equal(values.attemptHour,'2026-10-26T10');
  assert.equal(alarms.get('wb-qr-retry').when,now+60000);
  assert.equal(alarms.get('wb-qr-hourly').when,Date.parse('2026-10-26T08:00:00Z'));
  Object.assign(values,{run:{id:'fixture-success',day:'2026-10-26'},attempts:2});
  await vm.runInContext("enqueue(()=>completeUpload('fixture-success'))",context);
  assert.equal(values.lastSuccessDay,'2026-10-26');assert.equal(alarms.has('wb-qr-hourly'),false);
  await alarmListener({name:'wb-qr-hourly'});assert.equal(windows,1,'successful day must stop capture');
  // Browser wake/startup catches up immediately; migration preserves pairing.
  now=Date.parse('2026-10-27T10:37:00Z');
  Object.assign(values,{schemaVersion:6,attempts:3});
  await vm.runInContext('enqueue(recover)',context);
  assert.equal(windows,2);assert.equal(values.schemaVersion,7);assert.equal(values.attempts,1);
  assert.equal(values.uploadToken,'isolated-fixture-token');assert.equal(values.config.enabled,true);
  console.log('hourly worker: exhaustion reset, retry, stop after success, wake and migration PASS');
})().catch(e=>{console.error(e);process.exitCode=1;});
