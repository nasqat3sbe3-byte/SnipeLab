/* Personal plans stay in this browser. The server journal contains stock data only. */
(function(root){
'use strict';
const KEY='snipelab-watch-plans-v1', labels={available:'Available',stability:'الثبات',retest:'إعادة الاختبار',ready:'قائمة الأجهز',broken:'حالة الدعم',support:'مستوى الدعم'};
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number=v=>v==null||v===''||!Number.isFinite(Number(v))?null:Number(v);
const fmt=v=>number(v)==null?'—':Number(v).toLocaleString('en-US',{maximumFractionDigits:4});
const uid=()=>root.crypto?.randomUUID?.()||Date.now().toString(36)+Math.random().toString(36).slice(2);
const now=()=>new Date().toISOString();
const date=v=>new Date(v).toLocaleString('ar-SA-u-ca-gregory',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});
function evaluate(rule,values){
 if(rule.type==='manual')return rule.checked===true;
 const value=values?.[rule.type];
 if(value==null)return null;
 if(rule.type==='retest')return value==='success';
 const n=number(value),target=number(rule.target);if(n==null||target==null)return null;
 if(rule.type==='available')return target===0?n===0:n<target;
 if(rule.type==='stability')return n>=target;
 return null;
}
function ruleLabel(r){return r.type==='manual'?r.label:r.type==='available'?(r.target===0?'Available يساوي صفرًا':'Available أقل من '+fmt(r.target)):r.type==='stability'?'الثبات '+r.target+'/4 أو أكثر':'نجاح إعادة الاختبار'}
function personalTransitions(event,plan){
 if(!plan||event.context!==plan.context||Date.parse(event.at)<Date.parse(plan.createdAt))return [];
 return plan.rules.filter(r=>r.type!=='manual'&&event.changed.includes(r.type)&&Date.parse(event.at)>=Date.parse(r.createdAt)).flatMap(r=>{
  const a=evaluate(r,event.before),b=evaluate(r,event.after);
  if(a==null||b==null||a===b)return [];
  return [{id:'p'+(event.namespace||'')+':'+event.seq+':'+r.id,symbol:event.symbol,market:event.market,at:event.at,personal:true,
           title:(b?'تحقق شرطك: ':'اختل شرطك: ')+ruleLabel(r),before:a?'متحقق':'بانتظار التحقق',after:b?'متحقق':'لم يعد متحققًا',read:false}];
 });
}
function valueText(field,value){
 if(field==='retest')return {success:'ناجح',failed:'فشل / كسر',waiting:'انتظار'}[value]||'غير متاح';
 if(field==='ready')return value?'داخل الأجهز':'خارج الأجهز';
 if(field==='broken')return value?'رُصد كسر':'لا يوجد كسر حالي';
 if(field==='stability')return Math.min(4,value)+'/4';
 if(field==='support')return '$'+fmt(value);
 return fmt(value);
}
if(typeof document==='undefined'){if(typeof module!=='undefined')module.exports={evaluate,ruleLabel,personalTransitions,valueText};return;}
let state={plans:{},events:[],cursor:null,initialized:false,gap:false},observations={},observedAt=null,feedStatus='starting',busy=false,draft=null,market=location.pathname.startsWith('/low-float')?'low_float':'split',feedFilter='all',feedLimit=150,storageError=false;
try{const saved=JSON.parse(localStorage.getItem(KEY)||'null');if(saved&&typeof saved.plans==='object'&&Array.isArray(saved.events))state={...state,...saved};}catch(e){storageError=true;}
const $=id=>document.getElementById(id);
function save(){try{localStorage.setItem(KEY,JSON.stringify(state));storageError=false;return true}catch(e){storageError=true;return false}}
function favoriteSymbols(){
 const symbols=new Set();for(const key of ['snipelab-favorites','snipelab-low-float-favorites'])try{for(const s of JSON.parse(localStorage.getItem(key)||'[]'))symbols.add(s)}catch(e){}
 return symbols;
}
function current(rule,plan){
 if(rule.type==='manual')return evaluate(rule,{});
 const row=observations[plan.symbol];
 if(feedStatus!=='ready'||!row||!observedAt||Date.now()-Date.parse(observedAt)>90000)return null;
 if(plan.context!=null&&row.context!==plan.context)return null;
 return evaluate(rule,row.values);
}
function progress(plan){
 const results=plan.rules.map(r=>current(r,plan));
 return {done:results.filter(x=>x===true).length,total:results.length,pending:results.filter(x=>x===null).length,waiting:plan.rules.filter((r,i)=>results[i]!==true).map(r=>{const value=number(observations[plan.symbol]?.values?.stability);return r.type==='stability'&&current(r,plan)===false&&value!=null?'باقي '+Math.max(0,Math.ceil(r.target-value))+' جلسة ثبات للوصول إلى '+r.target+'/4':ruleLabel(r)})};
}
function summary(symbol){
 const plan=state.plans[symbol];if(!plan)return '';
 const p=progress(plan);
 return '<button type="button" class="watch-summary" data-watch-plan="'+esc(symbol)+'"><span>☑ خطة متابعتي</span><b>تحقق '+p.done+' من '+p.total+'</b><small>'+(p.waiting.length?'باقي: '+esc(p.waiting.slice(0,2).join('، '))+(p.waiting.length>2?'…':''):'جميع شروطك متحققة حاليًا')+(p.pending?' · '+p.pending+' بانتظار بيانات حديثة':'')+'</small></button>';
}
function detail(symbol){return '<div class="watch-detail-entry"><button type="button" data-watch-plan="'+esc(symbol)+'">☑ '+(state.plans[symbol]?'تعديل خطة متابعتي':'خطة متابعتي')+'</button>'+(state.plans[symbol]?summary(symbol):'<small>حدد ما راجعته وما تنتظر تحققه في هذا السهم.</small>')+'</div>'}
root.WatchPlans={summary,detail,open:openPlan};
function updateLists(){
 if(typeof root.render==='function')root.render();
 document.querySelectorAll('.watch-detail-entry').forEach(entry=>{const button=entry.querySelector('[data-watch-plan]');if(button){const temp=document.createElement('div');temp.innerHTML=detail(button.dataset.watchPlan);entry.replaceWith(temp.firstElementChild)}});
 updateBadge();
}
function updateBadge(){
 const favorites=favoriteSymbols();const unread=state.events.filter(e=>!e.read).length;
 if($('watchChangeCount')){$('watchChangeCount').textContent=unread>99?'99+':unread;$('watchChangeCount').hidden=!unread;}
 if($('watchChangesDialog')?.open)renderFeed();
}
function pushEvents(events){
 const ids=new Set(state.events.map(e=>e.id));for(const event of events)if(!ids.has(event.id)){state.events.push(event);ids.add(event.id)}
 state.events.sort((a,b)=>Date.parse(b.at)-Date.parse(a.at)||b.id.localeCompare(a.id));state.events=state.events.slice(0,1500);
}
function ingest(events,initial){
 const additions=[];
 for(const event of events){
  for(const field of event.kinds||[])additions.push({id:'s'+(event.namespace||'')+':'+event.seq+':'+field,symbol:event.symbol,market:event.market,at:event.at,
     title:labels[field]||field,before:valueText(field,event.before[field]),after:valueText(field,event.after[field]),read:initial,personal:false});
  additions.push(...personalTransitions(event,state.plans[event.symbol]));
 }
 pushEvents(additions);
}
async function poll(){
 if(busy||document.hidden)return;busy=true;
 try{
  // Replay bounded pages in order; remaining pages continue on the next poll.
  for(let page=0;page<4;page++){
   const symbols=[...new Set([...(draft?[draft.symbol]:[]),...Object.keys(state.plans),...favoriteSymbols()])].slice(0,150);
   const query=new URLSearchParams({symbols:symbols.join(','),limit:'300'});if(state.cursor!=null)query.set('after',state.cursor);if(state.journalId)query.set('journal',state.journalId);
   const r=await fetch('/api/changes?'+query,{cache:'no-store',signal:AbortSignal.timeout(15000)});if(!r.ok)throw Error();const j=await r.json();
   feedStatus=j.status;observations=j.observations||{};observedAt=j.observed_at;
   ingest((j.events||[]).map(e=>({...e,namespace:j.journal_id})),!state.initialized);state.cursor=j.next_cursor;state.journalId=j.journal_id;state.initialized=true;
   if(j.truncated||j.reset)state.gap=true;
   // Bind plans created before the first available source snapshot once only.
   for(const [symbol,plan] of Object.entries(state.plans))if(plan.context==null&&observations[symbol]?.context!=null)plan.context=observations[symbol].context;
   save();if(!j.has_more)break;
  }
 }catch(e){feedStatus='error';}
 finally{busy=false;updateLists();if(draft&&$('watchPlanDialog')?.open)updateDraftStatus();}
}
function feedEvents(){
 const favorites=favoriteSymbols();return state.events.filter(e=>feedFilter==='favorites'?favorites.has(e.symbol):feedFilter==='plans'?e.personal:feedFilter==='unread'?!e.read:true);
}
function renderFeed(){
 const events=feedEvents();
 $('watchFeedMeta').textContent=(feedStatus==='error'?'تعذر التحديث؛ المعروض آخر سجل محفوظ. ':feedStatus==='starting'?'نجمع أول ملاحظات السوق. ':'')+(state.gap?'السجل الأقدم غير مكتمل؛ تُعرض التغييرات المتاحة فقط. ':'')+(storageError?'تعذر حفظ حالة القراءة على هذا المتصفح.':'');
 $('watchFeedList').innerHTML=events.length?events.slice(0,feedLimit).map(e=>'<article class="watch-event '+(!e.read?'unread':'')+'"><div><button type="button" data-watch-event-open="'+esc(e.id)+'"><b>'+esc(e.symbol)+' ↗</b></button><span>'+(e.personal?'شرطك الخاص':e.market==='low_float'?'قليلة الفلوت':'أسهم التقسيم')+'</span><time>'+esc(date(e.at))+'</time></div><h3>'+esc(e.title)+'</h3><div class="watch-event-values"><span>'+esc(e.before)+'</span><i>←</i><strong>'+esc(e.after)+'</strong></div><button type="button" data-watch-read="'+esc(e.id)+'" class="watch-read">'+(e.read?'مقروء ✓':'تعليم كمقروء')+'</button></article>').join(''):'<div class="watch-empty">'+(feedFilter==='favorites'?'لا توجد تغييرات مسجلة لمفضلتك بعد.':feedFilter==='plans'?'تظهر هنا الشروط التي تحققت أو اختلت بعد حفظ خطتك.':'لا توجد تغييرات مطابقة حتى الآن. أول قراءة لكل سهم تنشئ نقطة مقارنة فقط.')+'</div>';
 $('watchFeedMore').hidden=events.length<=feedLimit;
 document.querySelectorAll('[data-watch-feed-filter]').forEach(b=>{const active=b.dataset.watchFeedFilter===feedFilter;b.classList.toggle('selected',active);b.setAttribute('aria-pressed',active)});
}
function openFeed(){renderFeed();$('watchChangesDialog').showModal();poll()}
function defaultRule(type,extra={}){return {id:uid(),type,createdAt:now(),...extra}}
function openPlan(symbol){
 symbol=String(symbol).toUpperCase();if(!/^[A-Z0-9.-]{1,12}$/.test(symbol))return;
 const existing=state.plans[symbol];
 draft=existing?JSON.parse(JSON.stringify(existing)):{symbol,market,context:observations[symbol]?.context??null,createdAt:now(),note:'',rules:[defaultRule('manual',{label:'راجعت أخبار السهم',checked:false}),defaultRule('manual',{label:'راجعت الطروحات',checked:false}),defaultRule('available',{target:10000}),defaultRule('stability',{target:3}),defaultRule('retest')]};
 draft.market=market;
 $('watchPlanSymbol').textContent=symbol;$('watchPlanNote').value=draft.note||'';$('watchPlanDelete').hidden=!existing;$('watchPlanMessage').textContent='';$('watchManualText').value='';renderRules();
 if(!$('watchPlanDialog').open)$('watchPlanDialog').showModal();poll();
}
function renderRules(){
 $('watchPlanRules').innerHTML=draft.rules.map(r=>{
  const result=current(r,draft),icon=result===true?'✓':result===false?'○':'؟';
  return '<div class="watch-rule" data-rule-id="'+esc(r.id)+'">'+(r.type==='manual'?'<label><input type="checkbox" data-watch-check="'+esc(r.id)+'" '+(r.checked?'checked':'')+'><span>'+esc(r.label)+'</span></label>':'<div class="watch-auto-line"><span class="watch-rule-icon '+(result===true?'met':'')+'" data-watch-status="'+esc(r.id)+'">'+icon+'</span><span>'+esc(ruleLabel(r))+'<small>يراقبه الموقع تلقائيًا</small></span></div>')+'<button type="button" data-watch-remove="'+esc(r.id)+'" aria-label="حذف الشرط '+esc(ruleLabel(r))+'">×</button></div>';
 }).join('');updateDraftStatus();
}
function updateDraftStatus(){
 if(!draft)return;const p=progress(draft);const changed=observations[draft.symbol]&&draft.context!=null&&observations[draft.symbol].context!==draft.context;
 $('watchPlanProgress').textContent='تحقق '+p.done+' من '+p.total+' شروط'+(p.pending?' · '+p.pending+' بانتظار بيانات حديثة':'');
 $('watchPlanReview').hidden=!changed;$('watchPlanReview').textContent='تغيّر تقسيم السهم منذ حفظ الخطة. راجع شروطك ثم احفظ لاعتماد المتابعة الجديدة.';
 draft.rules.filter(r=>r.type!=='manual').forEach(r=>{const node=document.querySelector('[data-watch-status="'+r.id+'"]');if(node){const status=current(r,draft);node.textContent=status===true?'✓':status===false?'○':'؟';node.classList.toggle('met',status===true)}});
}
function savePlan(){
 if(draft&&!state.plans[draft.symbol]&&Object.keys(state.plans).length>=100){$('watchPlanMessage').textContent='وصلت إلى 100 خطة؛ احذف خطة قديمة لإضافة أخرى.';return}
 if(!draft||!draft.rules.length){$('watchPlanMessage').textContent='أضف شرطًا واحدًا على الأقل.';return}
 const old=state.plans[draft.symbol],oldEvents=state.events.slice();
 draft.note=$('watchPlanNote').value.trim().slice(0,1000);draft.updatedAt=now();
 if(observations[draft.symbol]?.context!=null)draft.context=observations[draft.symbol].context;
 if(old){for(const r of draft.rules.filter(r=>r.type==='manual')){const previous=old.rules.find(x=>x.id===r.id);if(previous&&previous.checked!==r.checked)pushEvents([{id:'m'+uid(),symbol:draft.symbol,market:draft.market,at:now(),personal:true,title:r.label,before:previous.checked?'تمت المراجعة':'بانتظار المراجعة',after:r.checked?'تمت المراجعة':'أُلغيت علامة المراجعة',read:false}])}}
 state.plans[draft.symbol]=JSON.parse(JSON.stringify(draft));
 if(!save()){if(old)state.plans[draft.symbol]=old;else delete state.plans[draft.symbol];state.events=oldEvents;$('watchPlanMessage').textContent='تعذر الحفظ على المتصفح. الخطة لم تُحفظ؛ اترك النافذة مفتوحة وحاول مجددًا.';return}
 // Keep the existing favorite keys and in-page Set in sync.
 try{if(typeof favorites!=='undefined'){favorites.add(draft.symbol);localStorage.setItem(draft.market==='low_float'?'snipelab-low-float-favorites':'snipelab-favorites',JSON.stringify([...favorites]));}}catch(e){}
 $('watchPlanDialog').close();draft=null;updateLists();poll();
}
function init(){
 const mount=document.querySelector('.favorites-row')||document.querySelector('.search-row');if(mount){const button=document.createElement('button');button.type='button';button.className='watch-changes-button';button.id='watchChangesOpen';button.innerHTML='↺ وش تغيّر؟ <b id="watchChangeCount" hidden>0</b>';mount.append(button);button.addEventListener('click',openFeed)}
 document.body.insertAdjacentHTML('beforeend','<dialog id="watchChangesDialog" class="watch-dialog" aria-labelledby="watchChangesTitle"><header><div><small>SNIPELAB · سجل المتابعة</small><h2 id="watchChangesTitle">وش تغيّر؟</h2></div><button type="button" data-watch-close="watchChangesDialog" aria-label="إغلاق">×</button></header><p class="watch-intro">التغييرات الجديدة تبقى مميزة حتى تقرأها. الوقت هو وقت رصد التغيّر، وقد تكون هناك حركات بين التحديثات.</p><div class="watch-feed-filters"><button data-watch-feed-filter="all" type="button">الكل</button><button data-watch-feed-filter="favorites" type="button">مفضلتي</button><button data-watch-feed-filter="plans" type="button">شروطي</button><button data-watch-feed-filter="unread" type="button">غير المقروء</button></div><div class="watch-feed-actions"><button type="button" id="watchReadAll">تعليم الكل كمقروء</button><span id="watchFeedMeta" role="status"></span></div><div id="watchFeedList"></div><button type="button" id="watchFeedMore" class="watch-more" hidden>عرض تغييرات أقدم</button><footer>السجل العام يُحفظ على السيرفر منذ تشغيل الميزة. خططك وحالة القراءة محفوظة على هذا المتصفح.</footer></dialog><dialog id="watchPlanDialog" class="watch-dialog" aria-labelledby="watchPlanTitle"><header><div><small>خطة متابعة خاصة</small><h2 id="watchPlanTitle">خطة <span id="watchPlanSymbol"></span></h2></div><button type="button" data-watch-close="watchPlanDialog" aria-label="إغلاق">×</button></header><p class="watch-intro">علّم على ما راجعته بنفسك، وحدد ما تنتظره ليراقبه الموقع. الحفظ يضيف السهم لمفضلتك.</p><div id="watchPlanProgress" class="watch-progress" role="status"></div><p id="watchPlanReview" class="watch-warning" hidden></p><div id="watchPlanRules"></div><div class="watch-add"><h3>أضف شرطًا يدويًا</h3><div><input id="watchManualText" maxlength="120" placeholder="مثال: راجعت أخبار السهم" aria-label="نص الشرط اليدوي"><button type="button" id="watchAddManual">إضافة</button></div><h3>أضف شرطًا تلقائيًا</h3><div><select id="watchAutoType" aria-label="نوع الشرط"><option value="available">Available أقل من</option><option value="stability">الثبات يصل إلى</option><option value="retest">نجاح Retest</option></select><input id="watchAutoTarget" type="number" value="10000" min="0" max="10000000" step="1" aria-label="قيمة الشرط"><button type="button" id="watchAddAuto">إضافة</button></div><small>Available بقيمة 0 يعني يساوي صفرًا. الثبات من 2 إلى 4.</small></div><label class="watch-note-label">ملاحظتي على السهم<textarea id="watchPlanNote" maxlength="1000" rows="3" placeholder="ليش أتابعه؟ وش أنتظر منه؟"></textarea></label><p id="watchPlanMessage" role="status" class="watch-warning"></p><div class="watch-save-row"><button type="button" id="watchPlanSave">حفظ الخطة</button><button type="button" id="watchPlanDelete">حذف الخطة</button></div><footer>محفوظة على هذا المتصفح. علامة المراجعة اليدوية من اختيارك، ولا تعني أن الموقع تحقق من الخبر أو الطرح. التلقائي يعتمد على أحدث بيانات متاحة.</footer></dialog>');
 document.addEventListener('click',e=>{
  const plan=e.target.closest('[data-watch-plan]');if(plan){e.preventDefault();e.stopPropagation();openPlan(plan.dataset.watchPlan);return}
  const close=e.target.closest('[data-watch-close]');if(close){$(close.dataset.watchClose).close();return}
  const filter=e.target.closest('[data-watch-feed-filter]');if(filter){feedFilter=filter.dataset.watchFeedFilter;feedLimit=150;renderFeed();return}
  const remove=e.target.closest('[data-watch-remove]');if(remove&&draft){draft.rules=draft.rules.filter(r=>r.id!==remove.dataset.watchRemove);renderRules();return}
  const read=e.target.closest('[data-watch-read]'),open=e.target.closest('[data-watch-event-open]');
  if(read||open){const id=read?.dataset.watchRead||open.dataset.watchEventOpen,item=state.events.find(e=>e.id===id);if(!item)return;item.read=true;save();updateBadge();if(open){$('watchChangesDialog').close();if(item.market===market){if(market==='split'&&typeof root.openStockRoom==='function')root.openStockRoom(item.symbol);else if(market==='low_float'&&typeof root.detail==='function')root.detail(item.symbol);}else location.href=(item.market==='low_float'?'/low-float':'/dashboard')+'?focus='+encodeURIComponent(item.symbol);}return}
 });
 $('watchPlanRules').addEventListener('change',e=>{const id=e.target.dataset.watchCheck,r=draft?.rules.find(r=>r.id===id);if(r){r.checked=e.target.checked;updateDraftStatus()}});
 $('watchAddManual').addEventListener('click',()=>{const label=$('watchManualText').value.trim();if(!label||draft.rules.length>=20)return;draft.rules.push(defaultRule('manual',{label,checked:false}));$('watchManualText').value='';renderRules()});
 $('watchAutoType').addEventListener('change',()=>{const type=$('watchAutoType').value;$('watchAutoTarget').hidden=type==='retest';$('watchAutoTarget').value=type==='stability'?3:10000});
 $('watchAddAuto').addEventListener('click',()=>{const type=$('watchAutoType').value,target=number($('watchAutoTarget').value);if(draft.rules.length>=20)return;if(type!=='retest'&&(target==null||!Number.isInteger(target)||target<0||target>10000000||type==='stability'&&(target<2||target>4))){$('watchPlanMessage').textContent='أدخل قيمة صحيحة؛ الثبات من 2 إلى 4.';return}if(draft.rules.some(r=>r.type===type&&(type==='retest'||r.target===target))){$('watchPlanMessage').textContent='هذا الشرط موجود في الخطة.';return}draft.rules.push(defaultRule(type,type==='retest'?{}:{target}));$('watchPlanMessage').textContent='';renderRules()});
 $('watchPlanSave').addEventListener('click',savePlan);
 $('watchPlanDelete').addEventListener('click',()=>{if(!draft)return;const previous=state.plans[draft.symbol];delete state.plans[draft.symbol];if(!save()){state.plans[draft.symbol]=previous;$('watchPlanMessage').textContent='تعذر حفظ حذف الخطة.';return}$('watchPlanDialog').close();draft=null;updateLists()});
 $('watchFeedMore').addEventListener('click',()=>{feedLimit+=150;renderFeed()});
 $('watchReadAll').addEventListener('click',()=>{state.events.forEach(e=>e.read=true);save();updateBadge()});
 $('watchPlanDialog').addEventListener('close',()=>{draft=null});
 document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll()});
 root.addEventListener('storage',e=>{if(e.key===KEY){try{const saved=JSON.parse(e.newValue);if(saved){state=saved;updateLists()}}catch(e){}}else if(e.key?.includes('favorites'))updateBadge()});
 updateBadge();poll();setInterval(poll,20000);
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})(typeof window==='undefined'?globalThis:window);
