(()=>{
'use strict';
let data=null,busy=false,threshold=25,group='tracked',order='recent',query='',quality='all',day='',count=24;
const el=id=>document.getElementById(id);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=v=>v==null?'—':Number(v).toLocaleString('en-US',{maximumFractionDigits:2});
const price=v=>v==null?'—':'$'+Number(v).toLocaleString('en-US',{maximumFractionDigits:4});
const pct=v=>v==null?'—':(v>=0?'+':'')+num(v)+'%';
const qualityLabel={strong:'صعود متماسك',fade:'صعد ورجع',partial:'احتفاظ جزئي'};
const groupLabel={tracked:'أسهمنا',split:'أسهم التقسيم',low_float:'قليلة الفلوت',reference:'العينة الصغيرة'};
function belongs(r){return group==='tracked'?r.group!=='عينة الأسهم الصغيرة':group==='split'?r.group==='أسهم التقسيم':group==='low_float'?r.group==='قليلة الفلوت':r.group==='عينة الأسهم الصغيرة'}
function spark(m){
 const values=[0,...(m.path||[]).map(p=>p.gain)];if(values.length<2)return '';
 const min=Math.min(...values),max=Math.max(...values),span=max-min||1;
 const x=i=>6+i*268/(values.length-1),y=v=>53-(v-min)/span*43;
 const points=values.map((v,i)=>x(i).toFixed(1)+','+y(v).toFixed(1)).join(' ');
 return '<div class="pulse-path"><svg viewBox="0 0 280 62" role="img" aria-label="مسار الإغلاقات من قبل الحركة إلى آخر جلسة"><line x1="6" x2="274" y1="'+y(0)+'" y2="'+y(0)+'" stroke="currentColor" opacity=".18" stroke-dasharray="3 4"/><polyline points="'+points+'" fill="none" stroke="'+(values.at(-1)>=0?'#36856f':'#ba5b66')+'" stroke-width="2.4" stroke-linejoin="round"/><circle cx="274" cy="'+y(values.at(-1))+'" r="3" fill="currentColor"/></svg><div><span>قبل الحركة</span><b>آخر إغلاق '+pct(m.current_gain)+'</b></div></div>';
}
function render(){
 if(!data)return;
 const view=data.views?.[group]||data,ready=view.status==='ready'&&data.status!=='stale'&&data.status!=='error';
 const series=view.sessions||[];
 if(day&&!series.some(s=>s.date===day))day='';
 const selected=series.find(s=>s.date===day)||series.at(-1);
 el('pulseState').textContent=ready?view.label:(data.status==='stale'||data.status==='error'?data.label:view.label||'جارٍ جمع البيانات');
 el('pulseState').dataset.state=ready?'ready':'pending';
 el('pulseMeta').textContent=groupLabel[group]+' · '+(view.as_of||'—')+(view.provisional?' · جلسة جارية':' · آخر جلسة متاحة');
 el('pulseCoverage').textContent='تغطية '+(view.covered||0)+' / '+(view.total||0)+' سهم ('+(view.coverage||0)+'%)';
 el('pulseReasons').innerHTML=(ready?view.reasons||[]:['نجمع بيانات حديثة وكافية قبل الحكم على حالة المجموعة.']).map(x=>'<li>'+esc(x)+'</li>').join('');
 el('pulseComparison').textContent=data.comparison||'جارٍ إعداد المقارنة';
 el('pulseCountsTitle').textContent=day?'جلسة '+day:'آخر جلسة · '+(view.as_of||'—');
 el('pulseCounts').innerHTML=[10,25,50,100].map(n=>'<div><b>'+(selected?.covered?num(selected.counts[n]):'—')+'</b><small>وصلوا +'+n+'%</small></div>').join('');
 el('pulseGroups').innerHTML=['split','low_float','reference'].map(key=>{
  const v=data.views?.[key],s=v?.sessions?.find(x=>x.date===(day||v.as_of));
  const sufficient=v?.status==='ready'&&data.status!=='stale'&&data.status!=='error';
  const rate=s?.covered?100*s.counts['25']/s.covered:null;
  return '<button type="button" data-pulse-group="'+key+'" class="pulse-group-card '+(group===key?'selected':'')+'"><span>'+groupLabel[key]+'</span><b>'+(sufficient?num(s.mean_return)+'%':'—')+'</b><small>متوسط تغيّر الإغلاق</small><div><strong>'+(sufficient?num(rate)+'%':'—')+'</strong> وصلت +25%</div><small>تغطية '+(v?.covered||0)+' / '+(v?.total||0)+'</small></button>';
 }).join('');
 const max=Math.max(1,...series.map(s=>s.counts['25']));
 el('pulseTrend').innerHTML=series.map(s=>'<button type="button" class="pulse-day '+(day===s.date?'selected':'')+'" data-pulse-day="'+esc(s.date)+'" aria-pressed="'+(day===s.date)+'" aria-label="جلسة '+esc(s.date)+'، '+s.counts['25']+' سهمًا وصل +25 بالمئة"><b>'+s.counts['25']+'</b><span class="pulse-bar-track"><i style="height:'+Math.max(3,Math.round(66*s.counts['25']/max))+'px"></i></span><small>'+esc(s.date.slice(5))+'</small></button>').join('');
 el('pulseAllDays').classList.toggle('selected',!day);
 el('pulseSessionInfo').textContent=selected?'الأسهم الصاعدة '+num(selected.advance_pct)+'% · احتفظ بنصف الصعود '+num(selected.holding_pct)+'% · استمرارية متحركي أمس '+num(selected.continuation_pct)+'%':'';
 el('pulseContinuation').textContent=selected?'الحجم ≥2× المعتاد: '+selected.elevated_volume+' من '+selected.volume_samples+' سهمًا ببيانات كافية. '+(day?'القائمة الآن تعرض حركات هذه الجلسة.':'اضغط جلسة لعرض أسهمها.'):'';
 let rows=(data.rows||[]).filter(belongs).filter(r=>r.symbol.toLowerCase().includes(query.toLowerCase())).map(r=>({...r,matches:r.moves.filter(m=>m.gain>=threshold&&(!day||m.date===day)&&(quality==='all'||quality==='continue'?(quality!=='continue'||m.next_gain>0&&!m.next_provisional):!m.provisional&&m.quality===quality))})).filter(r=>r.matches.length);
 rows=rows.map(r=>({...r,selected:order==='gain'?[...r.matches].sort((a,b)=>b.gain-a.gain)[0]:order==='retention'?[...r.matches].sort((a,b)=>b.retention-a.retention)[0]:r.matches[0]}));
 rows.sort((a,b)=>order==='gain'?b.selected.gain-a.selected.gain:order==='retention'?b.selected.retention-a.selected.retention:b.selected.date.localeCompare(a.selected.date)||b.selected.gain-a.selected.gain);
 el('pulseResultCount').textContent=rows.length+' سهم · '+(day||'آخر 10 جلسات');
 const qualityCounts={strong:0,fade:0,partial:0,continue:0,pending:0};
 (data.rows||[]).filter(belongs).forEach(r=>{const moves=r.moves.filter(m=>m.gain>=threshold&&(!day||m.date===day));if(!moves.length)return;const m=moves[0];qualityCounts[m.provisional?'pending':m.quality]++;if(m.next_gain>0&&!m.next_provisional)qualityCounts.continue++});
 el('pulseQuality').innerHTML=[['strong','متماسك'],['fade','صعد ورجع'],['continue','واصل ثاني يوم']].map(([key,label])=>'<button type="button" data-pulse-quality="'+key+'" class="'+(quality===key?'selected':'')+'"><b>'+qualityCounts[key]+'</b><span>'+label+'</span></button>').join('');
 el('pulseCards').innerHTML=rows.length?rows.slice(0,count).map(r=>{
  const m=r.selected,q=m.provisional?'pending':m.quality;
  const link=r.group==='أسهم التقسيم'?'/dashboard?focus='+encodeURIComponent(r.symbol):r.group==='قليلة الفلوت'?'/low-float?focus='+encodeURIComponent(r.symbol):null;
  return '<article class="pulse-card"><div class="pulse-card-top"><div><small>'+esc(r.group)+'</small>'+(link?'<a href="'+link+'" class="pulse-symbol">'+esc(r.symbol)+' ↗</a>':'<b class="pulse-symbol">'+esc(r.symbol)+'</b>')+'</div><div class="pulse-gain">'+pct(m.gain)+'<small>أعلى صعود الجلسة</small></div></div><div class="pulse-card-meta"><span class="pulse-quality-badge '+q+'">'+(m.provisional?'قيد التداول':qualityLabel[q])+'</span><span>'+esc(m.date)+' · قبل '+m.since+' جلسات</span></div><div class="pulse-return-row"><div><small>ارتفاع الإغلاق</small><b>'+pct(m.close_gain)+'</b></div><div><small>آخر إغلاق مقابل ما قبل الحركة</small><b>'+pct(m.current_gain)+'</b></div></div>'+spark(m)+'<div class="pulse-metrics"><div><small>الإغلاق السابق ← أعلى الجلسة</small><b dir="ltr">'+price(m.reference)+' → '+price(m.peak)+'</b></div><div><small>آخر سعر يومي · '+esc(r.price_date)+'</small><b>'+price(r.price)+'</b></div><div><small>احتفظ من صعود الجلسة</small><b>'+num(m.retention)+'%</b></div><div><small>الحجم / متوسط سابق</small><b>'+num(m.volume_ratio)+'×</b></div></div><div class="pulse-behavior">'+(m.since===0?'جلسة الحركة؛ المتابعة تبدأ بالجلسة التالية':esc(m.behavior)+' · '+pct(m.after_gain)+' من إغلاق الحركة')+(m.next_gain!=null?' · ثاني جلسة '+pct(m.next_gain)+(m.next_provisional?' (جارية)':''):'')+'</div><details><summary>مسار الحركة'+(r.moves.length>1?' · '+r.moves.length+' جلسات صعود':'')+'</summary><p>التماسك: إغلاق يحتفظ بـ70% على الأقل من صعود القمة. صعد ورجع: احتفظ بـ25% أو أقل. الجلسة الجارية لا تُصنّف نهائيًا.</p>'+(m.path||[]).map(x=>'<div class="pulse-history"><span>'+esc(x.date)+'</span><b>'+pct(x.gain)+'</b></div>').join('')+'</details></article>';
 }).join(''):'<div class="pulse-empty">'+(view.covered?'لا توجد حركات تطابق الاختيارات. جرّب جميع الجلسات أو غيّر الفلتر.':'جارٍ جمع البيانات؛ ستظهر الحركات المؤكدة تلقائيًا.')+'</div>';
 el('pulseMore').hidden=rows.length<=count;
 el('pulseFresh').textContent=data.generated_at?'آخر حساب '+new Date(data.generated_at).toLocaleTimeString('ar-SA')+' · تحديث تلقائي':'';
 el('pulseReferenceInfo').textContent=(data.reference?.method||'')+' '+(data.reference?.available?'المصدر متاح.':'تعذّر تحميل المصدر؛ المقارنة معلّقة.');
 document.querySelectorAll('[data-pulse-threshold]').forEach(b=>{const active=Number(b.dataset.pulseThreshold)===threshold;b.classList.toggle('selected',active);b.setAttribute('aria-pressed',active)});
 el('pulseGroup').value=group;el('pulseClearQuality').hidden=quality==='all';
}
async function refresh(){if(busy||document.hidden||el('pulsePage').hidden)return;busy=true;try{const r=await fetch('/api/market-pulse',{cache:'no-store',signal:AbortSignal.timeout(15000)});if(!r.ok)throw Error();data=await r.json();render()}catch(e){el('pulseState').textContent='تعذر التحديث؛ التقييم معلّق';el('pulseFresh').textContent='نعيد المحاولة تلقائيًا؛ البيانات المعروضة من آخر حساب.'}finally{busy=false}}
el('pulsePage').addEventListener('click',e=>{
 const bar=e.target.closest('[data-pulse-day]'),g=e.target.closest('[data-pulse-group]'),q=e.target.closest('[data-pulse-quality]'),t=e.target.closest('[data-pulse-threshold]');
 if(bar)day=day===bar.dataset.pulseDay?'':bar.dataset.pulseDay;
 else if(g)group=g.dataset.pulseGroup;
 else if(q)quality=quality===q.dataset.pulseQuality?'all':q.dataset.pulseQuality;
 else if(t)threshold=Number(t.dataset.pulseThreshold);
 else return;
 count=24;render();
});
el('pulseGroup').addEventListener('change',e=>{group=e.target.value;count=24;render()});
el('pulseOrder').addEventListener('change',e=>{order=e.target.value;count=24;render()});
el('pulseQuery').addEventListener('input',e=>{query=e.target.value.trim();count=24;render()});
el('pulseAllDays').addEventListener('click',()=>{day='';count=24;render()});
el('pulseClearQuality').addEventListener('click',()=>{quality='all';count=24;render()});
el('pulseMore').addEventListener('click',()=>{count+=24;render()});
new MutationObserver(refresh).observe(el('pulsePage'),{attributes:true,attributeFilter:['hidden']});
document.addEventListener('visibilitychange',refresh);setInterval(refresh,20000);refresh();
})();
