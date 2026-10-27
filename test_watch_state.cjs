const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const memory=new Map(),requests=[];let seq=0,mode='base';
function environment(){
 const nodes=new Map(),documentEvents={},timers=[];
 function node(id){if(!nodes.has(id))nodes.set(id,{id,events:{},dataset:{},value:'',hidden:false,open:false,textContent:'',innerHTML:'',classList:{toggle(){}},addEventListener(n,f){this.events[n]=f},append(){},showModal(){this.open=true},close(){this.open=false;this.events.close?.()},setAttribute(){},querySelector(){return null}});return nodes.get(id)}
 const document={readyState:'loading',hidden:false,createElement:()=>node('created'),getElementById:node,querySelector:s=>s==='.favorites-row'?node('mount'):null,querySelectorAll:()=>[],addEventListener(n,f){documentEvents[n]=f},body:{insertAdjacentHTML(){}}};
 const root={crypto:{randomUUID:()=>`rule-${++seq}`},addEventListener(){},render(){},document};
 const localStorage={getItem:k=>memory.get(k)||null,setItem:(k,v)=>memory.set(k,v)};
 const base={available:25000,stability:2,retest:'waiting'},met={available:5000,stability:3,retest:'success'},failed={available:12000,stability:1,retest:'failed'};
 const fetch=async url=>{requests.push(url);if(mode==='error')throw Error();const symbols=new URL(url,'http://test').searchParams.get('symbols')||'';const values=mode==='met'?met:mode==='failed'?failed:base;const event=mode==='base'?[]:[{seq:mode==='met'?1:2,at:new Date(Date.now()+1000).toISOString(),symbol:'TEST',market:'split',context:'2026-10-01',changed:['available','stability','retest'],kinds:['available','stability','retest'],before:mode==='met'?base:met,after:values}];return {ok:true,json:async()=>({journal_id:'qa-journal',events:event,observations:symbols.includes('TEST')?{TEST:{values,context:'2026-10-01',market:'split'}}:{},observed_at:new Date().toISOString(),status:'ready',next_cursor:mode==='base'?0:mode==='met'?1:2,has_more:false})}};
 const context={window:root,document,localStorage,location:{pathname:'/dashboard'},fetch,URLSearchParams,AbortSignal,Date,console,setInterval:f=>timers.push(f),favorites:new Set()};
 vm.runInNewContext(fs.readFileSync('watch-plans.js','utf8'),context);
 documentEvents.DOMContentLoaded();
 return {node,root,timers,context};
}
const settle=async()=>{for(let i=0;i<5;i++)await new Promise(setImmediate)};
(async()=>{
 let app=environment();await settle();
 app.root.WatchPlans.open('TEST');await settle();
 assert(requests.some(u=>u.includes('symbols=TEST')),'Unsaved draft must receive current observations');
 const rule=app.node('watchPlanRules').innerHTML.match(/data-watch-check="([^"]+)/)[1];
 app.node('watchPlanRules').events.change({target:{dataset:{watchCheck:rule},checked:true}});
 app.node('watchPlanNote').value='ملاحظتي الخاصة';app.node('watchPlanSave').events.click();await settle();
 let state=JSON.parse(memory.get('snipelab-watch-plans-v1'));
 assert.equal(state.plans.TEST.rules[0].checked,true);assert.equal(state.plans.TEST.note,'ملاحظتي الخاصة');
 assert(JSON.parse(memory.get('snipelab-favorites')).includes('TEST'));
 mode='met';app.timers[0]();await settle();state=JSON.parse(memory.get('snipelab-watch-plans-v1'));
 assert.equal(state.events.filter(e=>e.personal).length,3);assert.match(app.root.WatchPlans.summary('TEST'),/تحقق 4 من 5/);
 mode='failed';app.timers[0]();await settle();state=JSON.parse(memory.get('snipelab-watch-plans-v1'));
 assert.equal(state.events.filter(e=>e.personal).length,6);assert.match(app.root.WatchPlans.summary('TEST'),/تحقق 1 من 5/);
 app.node('watchReadAll').events.click();assert(JSON.parse(memory.get('snipelab-watch-plans-v1')).events.every(e=>e.read));
 app=environment();await settle();assert.match(app.root.WatchPlans.summary('TEST'),/تحقق 1 من 5/);
 app.root.WatchPlans.open('TEST');await settle();assert.equal(app.node('watchPlanNote').value,'ملاحظتي الخاصة');assert.match(app.node('watchPlanRules').innerHTML,/checked/);
 assert(requests.every(u=>!u.includes('ملاحظتي')&&!u.includes('rule-')),'Personal data must never leave browser');
 mode='error';app.timers[0]();await settle();assert.match(app.root.WatchPlans.summary('TEST'),/بانتظار بيانات حديثة/);
 console.log('Plan editing, favorite sync, persistence after reload, fulfillment/reversal replay, read state, privacy and stale-data behavior passed.');
})().catch(e=>{console.error(e);process.exitCode=1});
