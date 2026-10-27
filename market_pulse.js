(()=>{
'use strict';
let data=null, busy=false, threshold=25, group='all', order='recent', query='', count=30;
const el=id=>document.getElementById(id);
const escape=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=v=>v==null?'—':Number(v).toLocaleString('en-US',{maximumFractionDigits:2});
const price=v=>v==null?'—':'$'+Number(v).toLocaleString('en-US',{maximumFractionDigits:4});
const pct=v=>v==null?'—':(v>=0?'+':'')+num(v)+'%';
function render(){
 if(!data)return;
 const ready=data.status==='ready';
 el('pulseState').textContent=data.label||'جارٍ جمع البيانات';
 el('pulseState').dataset.state=ready?'ready':'pending';
 el('pulseMeta').textContent=(data.as_of?'جلسة '+data.as_of+' · ':'')+(data.provisional?'جلسة جارية · ':'')+'تغطية '+(data.covered||0)+' / '+(data.total||0)+' سهم · '+(data.coverage||0)+'%';
 el('pulseReasons').innerHTML=(ready?data.reasons||[]:['التقييم معلّق حتى اكتمال بيانات حديثة وكافية.']).map(v=>'<li>'+escape(v)+'</li>').join('');
 el('pulseCounts').innerHTML=[10,25,50,100].map(n=>'<div><b>'+num(data.counts?.[n]??0)+'</b><small>وصلوا +'+n+'%</small></div>').join('');
 el('pulseTrend').innerHTML=(data.sessions||[]).map(s=>'<div class="pulse-day"><small>'+escape(s.date.slice(5))+'</small><div class="pulse-bar" style="height:'+Math.max(3,Math.round(56*s.counts['25']/Math.max(1,...data.sessions.map(v=>v.counts['25']))))+'px"></div><b>'+s.counts['25']+'</b><small>من '+s.covered+'</small></div>').join('');
 const last=data.sessions?.at(-1);
 el('pulseContinuation').textContent=last?'استمرارية متحركي أمس +25%: '+last.continued+' من '+last.prior_movers+' أغلقوا أعلى من أمس · حجم ≥2× المعتاد: '+last.elevated_volume+' من '+last.volume_samples+' سهم ببيانات حجم كافية.':'';
 let rows=(data.rows||[]).map(r=>({...r,matches:r.moves.filter(m=>m.gain>=threshold)})).filter(r=>r.matches.length&&(group==='all'||r.group===group)&&r.symbol.toLowerCase().includes(query.toLowerCase()));
 rows=rows.map(r=>({...r,selected:order==='gain'?[...r.matches].sort((a,b)=>b.gain-a.gain)[0]:r.matches[0]}));
 rows.sort((a,b)=>order==='gain'?b.selected.gain-a.selected.gain:b.selected.date.localeCompare(a.selected.date)||b.selected.gain-a.selected.gain);
 el('pulseResultCount').textContent=rows.length+' سهم · آخر 10 جلسات تداول';
 el('pulseCards').innerHTML=rows.length?rows.slice(0,count).map(r=>{
  const m=r.selected;
  return '<article class="pulse-card"><div class="pulse-card-top"><div><a href="'+(r.group==='أسهم التقسيم'?'/dashboard':'/low-float')+'?focus='+encodeURIComponent(r.symbol)+'" class="pulse-symbol">'+escape(r.symbol)+'</a><small>'+escape(r.group)+'</small></div><div class="pulse-gain">'+pct(m.gain)+'<small>أعلى صعود الجلسة</small></div></div><div class="pulse-date">'+escape(m.date)+' · قبل '+m.since+' جلسات'+(m.provisional?' · قيد التداول':'')+'</div><div class="pulse-metrics"><div><small>الإغلاق السابق ← أعلى الجلسة</small><b dir="ltr">'+price(m.reference)+' → '+price(m.peak)+'</b></div><div><small>ارتفاع الإغلاق</small><b dir="ltr">'+pct(m.close_gain)+'</b></div><div><small>الحجم / متوسط سابق</small><b>'+num(m.volume_ratio)+'×</b></div><div><small>آخر سعر يومي · '+escape(r.price_date)+'</small><b dir="ltr">'+price(r.price)+'</b></div></div><div class="pulse-behavior">'+(m.since===0?'جلسة الحركة؛ المتابعة تبدأ بالجلسة التالية':escape(m.behavior)+' · '+pct(m.after_gain)+' من إغلاق الحركة')+'</div>'+(r.moves.length>1?'<details><summary>'+r.moves.length+' جلسات صعود +25% خلال الفترة</summary>'+r.moves.map(x=>'<p>'+escape(x.date)+' · أعلى '+pct(x.gain)+' · إغلاق '+pct(x.close_gain)+'</p>').join('')+'</details>':'')+'</article>';
 }).join(''):'<div class="pulse-empty">'+(data.status==='ready'?'لا توجد أسهم تطابق الفلتر في آخر 10 جلسات.':'جارٍ جمع البيانات؛ ستظهر الحركات المؤكدة تلقائيًا.')+'</div>';
 el('pulseMore').hidden=rows.length<=count;
 el('pulseFresh').textContent=data.generated_at?'آخر حساب: '+new Date(data.generated_at).toLocaleTimeString('ar-SA')+' · تحديث تلقائي':'';
 document.querySelectorAll('[data-pulse-threshold]').forEach(b=>{b.classList.toggle('selected',Number(b.dataset.pulseThreshold)===threshold);b.setAttribute('aria-pressed',Number(b.dataset.pulseThreshold)===threshold)});
}
async function refresh(){
 if(busy||document.hidden||el('pulsePage').hidden)return;
 busy=true;
 try{
  const response=await fetch('/api/market-pulse',{cache:'no-store',signal:AbortSignal.timeout(15000)});
  if(!response.ok)throw Error('HTTP '+response.status);
  data=await response.json();render();
 }catch(e){el('pulseState').textContent='تعذر التحديث؛ التقييم معلّق';el('pulseFresh').textContent='تعذر الاتصال. ستتم إعادة المحاولة تلقائيًا.';}
 finally{busy=false;}
}
document.querySelectorAll('[data-pulse-threshold]').forEach(b=>b.addEventListener('click',()=>{threshold=Number(b.dataset.pulseThreshold);count=30;render()}));
el('pulseGroup').addEventListener('change',e=>{group=e.target.value;count=30;render()});
el('pulseOrder').addEventListener('change',e=>{order=e.target.value;count=30;render()});
el('pulseQuery').addEventListener('input',e=>{query=e.target.value.trim();count=30;render()});
el('pulseMore').addEventListener('click',()=>{count+=30;render()});
new MutationObserver(()=>refresh()).observe(el('pulsePage'),{attributes:true,attributeFilter:['hidden']});
document.addEventListener('visibilitychange',refresh);setInterval(refresh,20000);refresh();
})();
