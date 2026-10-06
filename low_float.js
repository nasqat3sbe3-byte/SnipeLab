'use strict';
const $ = id => document.getElementById(id);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num = v => v == null || v === '' || !Number.isFinite(Number(v)) ? null : Number(v);
const fmt = v => num(v) == null ? '—' : Number(v).toLocaleString('en-US', {maximumFractionDigits:2});
const money = v => num(v) == null ? '—' : '$' + Number(v).toLocaleString('en-US', {maximumFractionDigits:4});
const pct = v => num(v) == null ? '—' : fmt(v) + '%';
const compact = v => num(v) == null ? '—' : Number(v).toLocaleString('en-US', {notation:'compact', maximumFractionDigits:2});
let payload = {rows:{},status:{}}, selected = 'all', loading = false, limit = 24, opened = null, boot = null;
const favorites = new Set(JSON.parse(localStorage.getItem('snipelab-low-float-favorites') || '[]'));

function filterValues(){
  const values = {};
  document.querySelectorAll('[data-filter-kind]:checked').forEach(el => (values[el.dataset.filterKind] ??= []).push(Number(el.value)));
  return values;
}
function matches(row, filters){
  const values = {rsi:row.rsi_daily, available:row.borrow?.available, float:row.free_float, cap:row.market_cap, price:row.price?.price};
  return Object.entries(filters).every(([kind, limits]) => {
    const value = num(values[kind]);
    return value != null && limits.some(max => kind === 'available' && max === 0 ? value === 0 :
      kind === 'float' || kind === 'rsi' && max !== 15 ? value <= max : value < max);
  });
}
function allRows(){return Object.values(payload.rows || {}).filter(row=>num(row.price?.price)!=null && Number(row.price.price)>0 && Number(row.price.price)<5);}
function rows(){
  let list = allRows().filter(row => matches(row, filterValues()));
  const query = $('query').value.trim().toLowerCase();
  if(query) list = list.filter(row => (row.symbol+' '+(row.company_name || '')).toLowerCase().includes(query));
  if(selected === 'borrow') list = list.filter(row => num(row.borrow?.available) != null && Number(row.borrow.available) < 10000);
  if(selected === 'rsi') list = list.filter(row => num(row.rsi_daily) != null && Number(row.rsi_daily) < 30);
  if(selected === 'float') list = list.filter(row => Number(row.free_float) <= 1000000);
  const kind = $('approvedSort').value;
  const value = row => num(kind === 'rsi' ? row.rsi_daily : kind === 'available' ? row.borrow?.available : kind === 'cap' ? row.market_cap : row.free_float) ?? Infinity;
  return list.sort((a,b) => value(a)-value(b) || a.symbol.localeCompare(b.symbol));
}
function sparkline(row,cls){
  const values = (row.price?.sparkline || []).filter(v => num(v) != null && v > 0);
  if(values.length<2) return '<span class="sl-spark sl-spark-empty">—</span>';
  const lo=Math.min(...values), span=Math.max(...values)-lo || 1;
  const points=values.map((v,i)=>(i*100/(values.length-1)).toFixed(1)+','+(29-(v-lo)/span*25).toFixed(1)).join(' ');
  return '<span class="sl-spark '+cls+'"><svg viewBox="0 0 100 33" aria-label="حركة السعر"><polyline points="'+points+'" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg></span>';
}
function card(row){
  const p=num(row.price?.price), prev=num(row.price?.previous_close), change=p!=null && prev>0 ? (p/prev-1)*100 : null;
  const cls=change==null?'flat':change>=0?'up':'down';
  const metric=(label,value,cls='')=>'<div class="sl-metric"><small>'+esc(label)+'</small><b class="'+cls+'">'+esc(value)+'</b></div>';
  return '<article class="sl-row state-watch" role="button" tabindex="0" data-symbol="'+esc(row.symbol)+'" aria-label="تفاصيل '+esc(row.symbol)+'"><div class="sl-mainline"><span class="sl-change '+cls+'">'+esc(change==null?'—':(change>0?'+':'')+change.toFixed(2)+'%')+'</span>'+sparkline(row,cls)+'<div class="sl-quote"><span class="sl-price">'+esc(money(p))+'</span><span class="sl-dollar '+cls+'">'+esc(p!=null&&prev>0?(p-prev>=0?'+':'-')+money(Math.abs(p-prev)):'—')+'</span></div><div class="sl-identity"><div class="sl-identity-top"><button class="sl-fav '+(favorites.has(row.symbol)?'on':'')+'" data-favorite="'+esc(row.symbol)+'" aria-label="المفضلة" aria-pressed="'+favorites.has(row.symbol)+'">'+(favorites.has(row.symbol)?'★':'☆')+'</button><span class="sl-symbol">'+esc(row.symbol)+'</span></div><span class="sl-company">'+esc(row.company_name || '—')+'</span><span class="sl-open-hint" aria-hidden="true">›</span></div></div><div class="sl-metrics">'+metric('Available',fmt(row.borrow?.available),num(row.borrow?.available)!=null&&Number(row.borrow.available)<10000?'good':'borrow-warn')+metric('RSI',fmt(row.rsi_daily),num(row.rsi_daily)!=null&&Number(row.rsi_daily)<30?'good':'')+metric('Free Float',compact(row.free_float))+metric('Market Cap','$'+compact(row.market_cap))+metric('CTB',pct(row.borrow?.ctb))+metric('Rebate',pct(row.borrow?.rebate),num(row.borrow?.rebate)<0?'bad':'')+'</div></article>';
}
function updateFilterCount(){
  const n=document.querySelectorAll('[data-filter-kind]:checked').length;
  $('approvedFilterCount').textContent=n+' شروط';
  $('approvedFilterCount').hidden=!n;
  $('approvedFilterDone').textContent='عرض النتائج ('+fmt(rows().length)+' سهم)';
}
function render(){
  const base=allRows().filter(row=>matches(row,filterValues())), list=rows();
  const pending=!allRows().length && payload.status?.status!=='ready' && !payload.status?.error;
  $('countAll').textContent=pending?'—':fmt(base.length);
  $('countBorrow').textContent=fmt(base.filter(row=>num(row.borrow?.available)!=null&&Number(row.borrow.available)<10000).length);
  $('countReady').textContent=fmt(base.filter(row=>num(row.rsi_daily)!=null&&Number(row.rsi_daily)<30).length);
  $('countNear').textContent=fmt(base.filter(row=>Number(row.free_float)<=1000000).length);
  document.querySelectorAll('[data-filter]').forEach(button=>{const active=button.dataset.filter===selected;button.classList.toggle('on',active);button.setAttribute('aria-pressed',String(active));button.querySelector('.sum-action').textContent=active?'القائمة الحالية':'عرض القائمة ←';});
  $('filteredResultCount').textContent=pending?'جاري التحقق من الأسهم':fmt(list.length)+' سهم مطابق';
  const caps=filterValues().cap || [300000000];$('referenceMarket').textContent='Market Cap < $'+compact(Math.max(...caps));
  const status=payload.status || {}, complete=status.last_complete_scan?new Date(status.last_complete_scan).toLocaleString('ar-SA'):null;
  $('status').textContent=(status.error || (status.status==='ready'?'القائمة محدثة':'جاري اكتشاف الأسهم وتحديثها في الخلفية'))+' · '+fmt(status.scanned || 0)+' سهم فُحص · تم التحقق من '+fmt(status.verification?.checked || status.checked || 0)+' من '+fmt(status.candidates || 0)+' مرشحًا'+(complete?' · آخر مسح: '+complete:'');
  $('stocks').innerHTML=list.length?list.slice(0,limit).map(card).join('')+(list.length>limit?'<button id="more" type="button" class="loadmore">عرض المزيد</button>':''):'<div class="empty">'+esc(allRows().length?'لا توجد أسهم مطابقة للفلاتر الحالية.':status.error || 'جاري التحقق من '+fmt(status.verification?.checked || status.checked || 0)+' من '+fmt(status.candidates || 0)+' مرشحًا. ستظهر الأسهم المؤهلة تلقائيًا؛ القائمة لم تكتمل بعد.')+'</div>';
  updateFilterCount();
}
function detail(sym, scroll=true){
  const row=payload.rows[sym];if(!row)return;
  opened=sym;$('huntPage').hidden=true;$('stockDetailPage').hidden=false;
  const item=(label,value)=>'<span>'+esc(label)+'</span><b>'+esc(value)+'</b>';
  $('stockDetailBody').innerHTML='<div class="ref-room lf-detail"><button class="ref-back" id="roomBack" type="button">‹ رجوع للقائمة</button><h2>'+esc(sym)+'</h2><p>'+esc(row.company_name || '')+'</p><div class="room-panel"><div class="room-data">'+item('السعر',money(row.price?.price))+item('RSI Daily (14)',fmt(row.rsi_daily))+item('Free Float',fmt(row.free_float))+item('Market Cap',money(row.market_cap))+item('Available',fmt(row.borrow?.available))+item('CTB',pct(row.borrow?.ctb))+item('Rebate',pct(row.borrow?.rebate))+item('البورصة',row.primary_exchange || '—')+'</div><div class="lf-asof">'+esc('آخر سعر: '+(row.price?.market_timestamp?new Date(row.price.market_timestamp).toLocaleString('ar-SA'):'—')+' · آخر RSI: '+(row.rsi_last_bar_date || '—'))+'</div><div class="lf-asof">'+esc('تاريخ قياس الفلوت: '+(row.float_effective_date || '—')+' · المصدر: Massive')+'</div></div><div class="lf-detail-links"><a href="https://www.tradingview.com/chart/?symbol='+encodeURIComponent(sym)+'" target="_blank" rel="noopener noreferrer">الشارت ↗</a><a href="https://finance.yahoo.com/quote/'+encodeURIComponent(sym)+'/news/" target="_blank" rel="noopener noreferrer">أخبار السهم ↗</a></div></div>';
  if(scroll)window.scrollTo({top:0});
}
function closeDetail(){opened=null;$('stockDetailPage').hidden=true;$('huntPage').hidden=false;}
async function refresh(){
  if(loading||document.hidden)return;loading=true;
  try{
    const response=await fetch('/api/low-float',{cache:'no-store',signal:AbortSignal.timeout(20000)});
    if(!response.ok)throw Error('HTTP '+response.status);
    payload=await response.json();boot=Date.now()-payload.uptime_seconds*1000;
    $('headerServerLabel').textContent='Server online';$('headerServerLine').dataset.state='online';
    render();if(opened){if(payload.rows[opened])detail(opened,false);else closeDetail();}
  }catch(e){$('headerServerLabel').textContent='Server offline';$('headerServerLine').dataset.state='offline';$('status').textContent='تعذر الاتصال؛ ستتم إعادة المحاولة تلقائيًا.';}
  finally{loading=false;}
}
function themeLabel(){$('themeToggle').textContent=document.body.classList.contains('black-gold')?'◐ Crystal':'◐ Black Gold';}
if(localStorage.getItem('snipelab-theme')==='black-gold')document.body.classList.add('black-gold');themeLabel();
$('themeToggle').onclick=()=>{document.body.classList.toggle('black-gold');localStorage.setItem('snipelab-theme',document.body.classList.contains('black-gold')?'black-gold':'crystal');themeLabel();};
$('referenceMenu').onclick=()=>$('referenceMenuDialog').showModal();$('referenceMenuClose').onclick=()=>$('referenceMenuDialog').close();
$('approvedFilterOpen').onclick=()=>{updateFilterCount();$('approvedFilterDialog').showModal();};
$('approvedFilterClose').onclick=()=>$('approvedFilterDialog').close();
$('approvedFilterDone').onclick=()=>{$('approvedFilterDialog').close();limit=24;render();};
function clearFilters(){document.querySelectorAll('[data-filter-kind]').forEach(el=>el.checked=false);limit=24;render();}
$('approvedFilterClear').onclick=clearFilters;$('clearAppliedFilters').onclick=clearFilters;
document.querySelectorAll('[data-filter-kind]').forEach(el=>el.addEventListener('change',updateFilterCount));
$('query').oninput=()=>{limit=24;render();};$('approvedSort').onchange=()=>{limit=24;render();};
document.querySelectorAll('[data-filter]').forEach(button=>button.onclick=()=>{selected=button.dataset.filter;limit=24;render();});
document.querySelectorAll('[data-href]').forEach(button=>button.onclick=()=>location.href=button.dataset.href);
document.addEventListener('click',event=>{
  const favorite=event.target.closest('[data-favorite]');if(favorite){const sym=favorite.dataset.favorite;favorites.has(sym)?favorites.delete(sym):favorites.add(sym);localStorage.setItem('snipelab-low-float-favorites',JSON.stringify([...favorites]));render();return;}
  if(event.target.closest('#roomBack')){closeDetail();return;}
  if(event.target.closest('#more')){limit+=24;render();return;}
  const row=event.target.closest('.sl-row');if(row)detail(row.dataset.symbol);
});
document.addEventListener('keydown',event=>{const row=event.target.closest('.sl-row');if(row&&event.target===row&&['Enter',' '].includes(event.key)){event.preventDefault();detail(row.dataset.symbol);}if(event.key==='Escape'&&opened)closeDetail();});
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
setInterval(()=>{if(boot!=null){const seconds=Math.max(0,Math.floor((Date.now()-boot)/1000));$('headerServerTime').textContent=Math.floor(seconds/86400)+'.'+[Math.floor(seconds/3600)%24,Math.floor(seconds/60)%60,seconds%60].map(v=>String(v).padStart(2,'0')).join('.');}},1000);
refresh();setInterval(refresh,30000);
