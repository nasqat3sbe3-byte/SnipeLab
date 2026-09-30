import asyncio, json, os, re, sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import httpx
from bs4 import BeautifulSoup

YAHOO="https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS={"User-Agent":os.environ.get("SEC_USER_AGENT","SnipeLab research contact@snipelab.app")}

def safe_ratio(raw):
    try:
        a,b=[float(x) for x in re.split(r"[:/]",str(raw))[:2]]
        return a,b
    except Exception:return None,None

def wave(bars,start,end,limit=None):
    best=None;first100=None
    end=min(end,len(bars))
    for li in range(start,end):
        low=bars[li]["low"]
        hi_end=end if limit is None else min(end,li+limit+1)
        for hi in range(li,hi_end):
            high=bars[hi]["high"]
            if low<=0 or high<=low:continue
            g=round((high/low-1)*100,2)
            x={"gain_pct":g,"low":round(low,6),"high":round(high,6),"low_date":bars[li]["date"],"high_date":bars[hi]["date"],"sessions":hi-li,"same_day":hi==li}
            if best is None or g>best["gain_pct"]:best=x
            if g>=100 and (first100 is None or (x["high_date"],x["low_date"])<(first100["high_date"],first100["low_date"])):first100=x
    return best,first100

def sec_rows(sub):
    r=((sub.get("filings") or {}).get("recent") or {});n=max((len(v) for v in r.values() if isinstance(v,list)),default=0)
    out=[]
    for i in range(n):
        row={k:(r.get(k) or [None]*n)[i] if i<len(r.get(k) or []) else None for k in ("accessionNumber","filingDate","reportDate","form","primaryDocument","primaryDocDescription")}
        if row["accessionNumber"]:out.append(row)
    return out

async def fetch_text(client,cik,row):
    acc=str(row["accessionNumber"]).replace("-","");doc=row.get("primaryDocument")
    if not doc:return ""
    u=f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
    try:
        r=await client.get(u,timeout=10);r.raise_for_status()
        return BeautifulSoup(r.text,"html.parser").get_text(" ",strip=True)[:650000]
    except Exception:return ""

def holder_name(text):
    for p in (r"Name of Reporting Person\s*[:\-]?\s*([A-Z][A-Za-z0-9 &.,'()/-]{2,100})",r"NAME OF REPORTING PERSON\s*([A-Z][A-Z0-9 &.,'()/-]{2,100})"):
        m=re.search(p,text,re.I)
        if m:
            n=re.split(r"\s{2,}|Item\s+\d|I\.R\.S",m.group(1).strip())[0].strip(" .:-")
            if 2<len(n)<110:return n
    return None

def offering_status(form,text):
    t=text.lower()
    if any(x in t for x in ("closing of its previously announced","offering has closed","completed its previously announced","completed the offering")):return "مكتمل"
    if any(x in t for x in ("pricing of its","priced public offering","offering price of","purchase price of")):return "تم التسعير / لم يظهر تأكيد إغلاق في نفس الإفصاح"
    if "at-the-market" in t or "at the market offering" in t or "sales agreement" in t:return "برنامج ATM"
    if form in ("S-1","S-1/A","F-1","F-1/A"):return "تسجيل فقط / ليس دليلاً على التنفيذ"
    if form=="EFFECT":return "أصبح التسجيل Effective / لا يثبت البيع وحده"
    return "إفصاح مرتبط بالطرح / التنفيذ غير محسوم"

async def main(symbol):
    out={"symbol":symbol,"generated_at":datetime.now(timezone.utc).isoformat(),"splits":[],"reverse_split_count":0,
         "split_cycles":[],"last_strong_move":None,"extended_hours":None,"offerings":[],"dilution":[],"ownership":[],"filings":[],
         "summary":[],"warnings":[],"sources":[]}
    async with httpx.AsyncClient(timeout=12,follow_redirects=True,headers=HEADERS,limits=httpx.Limits(max_connections=5,max_keepalive_connections=3)) as client:
        # Price/splits
        try:
            r=await client.get(YAHOO.format(symbol=symbol),params={"range":"10y","interval":"1d","events":"splits","includePrePost":"false"});r.raise_for_status()
            ch=(r.json().get("chart",{}).get("result") or [None])[0]
            ts=ch.get("timestamp") or [];q=((ch.get("indicators") or {}).get("quote") or [{}])[0];bars=[]
            for i,t in enumerate(ts):
                try:
                    lo=float(q["low"][i]);hi=float(q["high"][i])
                    if lo>0 and hi>=lo:bars.append({"date":datetime.fromtimestamp(int(t),timezone.utc).date().isoformat(),"low":lo,"high":hi})
                except Exception:pass
            events=((ch.get("events") or {}).get("splits") or {});spl=[]
            for ev in events.values():
                raw=ev.get("splitRatio") or (f'{ev.get("numerator")}:{ev.get("denominator")}' if ev.get("numerator") and ev.get("denominator") else None)
                a,b=safe_ratio(raw);d=datetime.fromtimestamp(int(ev["date"]),timezone.utc).date().isoformat() if ev.get("date") else None
                spl.append({"date":d,"ratio":raw,"reverse":bool(a and b and a<b)})
            spl.sort(key=lambda x:x["date"] or "");out["splits"]=spl;rev=[x for x in spl if x["reverse"]];out["reverse_split_count"]=len(rev)
            for i,sp in enumerate(rev):
                cut=next((n for n,b in enumerate(bars) if b["date"]>=sp["date"]),len(bars))
                nxt=rev[i+1]["date"] if i+1<len(rev) else None
                end=next((n for n,b in enumerate(bars) if nxt and b["date"]>=nxt),len(bars))
                if cut>=len(bars):continue
                day=bars[cut];best,first100=wave(bars,cut,end,None)
                out["split_cycles"].append({"date":sp["date"],"ratio":sp["ratio"],"split_day_low":round(day["low"],6),"split_day_high":round(day["high"],6),
                    "split_day_range_pct":round((day["high"]/day["low"]-1)*100,2) if day["low"] else None,"strongest_run":best,"first_100_run":first100})
            latest=None
            for li in range(len(bars)):
                low=bars[li]["low"]
                for hi in range(li+1,min(len(bars),li+11)):
                    if low>0 and bars[hi]["high"]/low>=2:
                        x={"gain_pct":round((bars[hi]["high"]/low-1)*100,2),"low":round(low,6),"high":round(bars[hi]["high"],6),"low_date":bars[li]["date"],"high_date":bars[hi]["date"],"sessions":hi-li}
                        if latest is None or (x["high_date"],x["gain_pct"])>(latest["high_date"],latest["gain_pct"]):latest=x
            out["last_strong_move"]=latest;out["sources"].append("Yahoo daily + split events")
            # Extended hours recent coverage
            if rev and (datetime.now(timezone.utc).date()-datetime.fromisoformat(rev[-1]["date"]).date()).days<=60:
                er=await client.get(YAHOO.format(symbol=symbol),params={"range":"60d","interval":"5m","includePrePost":"true","events":"history"});er.raise_for_status()
                ec=(er.json().get("chart",{}).get("result") or [None])[0];ets=ec.get("timestamp") or [];eq=((ec.get("indicators") or {}).get("quote") or [{}])[0];ny=ZoneInfo("America/New_York");eb=[]
                for i,t in enumerate(ets):
                    try:
                        lo=float(eq["low"][i]);hi=float(eq["high"][i]);dt=datetime.fromtimestamp(int(t),timezone.utc).astimezone(ny)
                        if lo>0 and hi>=lo and dt.date().isoformat()>=rev[-1]["date"]:eb.append({"at":dt.isoformat(),"low":lo,"high":hi})
                    except Exception:pass
                if eb:
                    low=min(eb,key=lambda x:x["low"]);after=[x for x in eb if x["at"]>=low["at"]];high=max(after,key=lambda x:x["high"])
                    out["extended_hours"]={"low":round(low["low"],6),"low_at":low["at"],"high":round(high["high"],6),"high_at":high["at"],"gain_pct":round((high["high"]/low["low"]-1)*100,2)}
                    out["sources"].append("Yahoo 5m extended hours")
        except Exception as e:out["warnings"].append("تعذر جزء السعر/التقسيم: "+type(e).__name__)
        # SEC
        try:
            tr=await client.get("https://www.sec.gov/files/company_tickers.json");tr.raise_for_status()
            mp={str(v.get("ticker","")).upper():int(v["cik_str"]) for v in tr.json().values() if v.get("ticker")}
            cik=mp.get(symbol);out["cik"]=cik
            if cik:
                sr=await client.get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json");sr.raise_for_status();sub=sr.json();out["company_name"]=sub.get("name")
                rows=sec_rows(sub)
                # Do not let a flood of 8-Ks crowd out ownership/offering forms.
                offer_forms={"S-1","S-1/A","F-1","F-1/A","424B3","424B4","424B5","EFFECT","8-K","8-K/A","6-K"}
                owner_forms={"SC 13D","SC 13D/A","SC 13G","SC 13G/A","3","3/A","4","4/A","5","5/A"}
                material_forms={"8-K","8-K/A","6-K","10-Q","10-K","20-F","DEF 14A","PRE 14A"}
                selected=[]
                seen=set()
                for pool,limit in (
                    ([x for x in rows if x.get("form") in offer_forms],18),
                    ([x for x in rows if x.get("form") in owner_forms],18),
                    ([x for x in rows if x.get("form") in material_forms],18)):
                    for x in pool[:limit]:
                        acc=x.get("accessionNumber")
                        if acc and acc not in seen:
                            seen.add(acc);selected.append(x)
                sem=asyncio.Semaphore(4)
                async def enrich(row):
                    async with sem:
                        txt=await fetch_text(client,cik,row);low=txt.lower()
                        topics=[k for k in ("reverse split","public offering","registered direct","at-the-market","sales agreement","warrant","convertible","nasdaq","compliance","delisting","merger","acquisition","bankruptcy","going concern","authorized shares","shareholder approval","unregistered sales of equity securities","item 3.02") if k in low]
                        base={"date":row.get("filingDate"),"form":row.get("form"),"description":row.get("primaryDocDescription"),"accession":row.get("accessionNumber"),"topics":topics}
                        evidence=[]
                        for k in topics[:4]:
                            p=low.find(k)
                            if p>=0:evidence.append(re.sub(r"\s+"," ",txt[max(0,p-120):p+420]))
                        base["evidence"]=evidence

                        # Offering / registration classification.
                        if row.get("form") in offer_forms:
                            if any(k in low for k in ("offering","at-the-market","sales agreement","registered direct","public offering","securities purchase")) or row.get("form") in ("S-1","S-1/A","F-1","F-1/A","424B3","424B4","424B5","EFFECT"):
                                base["offering"]={"kind":"ATM" if ("at-the-market" in low or "at the market offering" in low or "sales agreement" in low) else ("Registered Direct" if "registered direct" in low else "Offering / Registration"),"status":offering_status(row.get("form"),txt)}

                        # Equity issuance / dilution can be disclosed in 8-K Item 3.02
                        # without the word "offering", so classify it separately.
                        is_302=("item 3.02" in low or "unregistered sales of equity securities" in low)
                        issue_hits=[]
                        for pat in (r"(?:issue|issued|issuance of|agreed to issue)\s+(?:an aggregate (?:amount )?of\s+)?([0-9][0-9,]*)\s+shares",
                                    r"([0-9][0-9,]*)\s+shares of (?:its )?common stock"):
                            for m in re.finditer(pat,txt,re.I):
                                try: issue_hits.append(int(m.group(1).replace(",","")))
                                except Exception: pass
                        if is_302 or issue_hits:
                            base["dilution"]={"kind":"Unregistered equity issuance / exchange" if is_302 else "Share issuance",
                                "shares_mentioned":max(issue_hits) if issue_hits else None,
                                "status":"تم الإفصاح عن إصدار/اتفاق إصدار أسهم؛ راجع الإفصاح لتفاصيل التسوية"}

                        if row.get("form") in owner_forms:
                            name=holder_name(txt)
                            change="إفصاح ملكية"
                            remaining=None
                            if row.get("form") in ("3","3/A"):
                                change="إفصاح ملكية أولي"
                            elif row.get("form") in ("4","4/A"):
                                sale=bool(re.search(r"\bTransaction Code\b.{0,220}\bS\b",txt,re.I) or
                                          re.search(r"\bS\b\s+\d[\d,]*\s+\$?[0-9.]+",txt))
                                change="بيع / خفض ملكية" if sale else "تغير ملكية مُبلغ عنه"
                                m=re.search(r"Amount of Securities Beneficially Owned Following Reported Transaction\(s\).*?([0-9][0-9,]*)",txt,re.I|re.S)
                                if m:
                                    try: remaining=int(m.group(1).replace(",",""))
                                    except Exception: pass
                            elif row.get("form","").startswith("SC 13"):
                                # 13D/G are ownership snapshots/amendments; do not label exit without evidence.
                                change="ملكية كبيرة 13D/13G؛ لا نثبت الخروج دون إفصاح واضح"
                            base["ownership"]={"holder":name,"change":change,"remaining_shares":remaining}
                        return base

                enriched=await asyncio.gather(*(enrich(x) for x in selected))
                out["filings"]=enriched
                out["offerings"]=[{"date":x["date"],"form":x["form"],**x["offering"],"description":x.get("description")} for x in enriched if x.get("offering")][:14]
                out["dilution"]=[{"date":x["date"],"form":x["form"],**x["dilution"],"description":x.get("description")} for x in enriched if x.get("dilution")][:14]
                out["ownership"]=[{"date":x["date"],"form":x["form"],**x["ownership"]} for x in enriched if x.get("ownership")][:18]
                out["sources"].append("SEC EDGAR recent relevant filings")
            else:out["warnings"].append("لم يتم ربط الرمز بـ CIK في SEC")
        except Exception as e:out["warnings"].append("تعذر جزء SEC: "+type(e).__name__)
    # Dense factual summary
    latest_split=next((x for x in reversed(out["splits"]) if x.get("reverse")),None)
    if latest_split:out["summary"].append(f"قسم السهم عكسيًا {out['reverse_split_count']} مرة ضمن التاريخ المتاح؛ آخر تقسيم {latest_split.get('ratio')} بتاريخ {latest_split.get('date')}.")
    if out["split_cycles"]:
        x=out["split_cycles"][-1];r=x.get("strongest_run")
        s=f"في يوم آخر تقسيم كان مدى الشمعة اليومية +{x.get('split_day_range_pct')}% من {x.get('split_day_low')} إلى {x.get('split_day_high')}."
        if r:s+=f" أقوى حركة بعده بلغت +{r.get('gain_pct')}% من {r.get('low')} إلى {r.get('high')} بين {r.get('low_date')} و{r.get('high_date')}."
        out["summary"].append(s)
    if out.get("extended_hours"):
        x=out["extended_hours"];out["summary"].append(f"مع الساعات الممتدة الحديثة رُصد مسار +{x['gain_pct']}% من {x['low']} إلى {x['high']}.")
    if out.get("last_strong_move"):
        x=out["last_strong_move"];out["summary"].append(f"آخر حركة قوية +100% أو أكثر ضمن قاعدة 10 جلسات كانت +{x['gain_pct']}% بين {x['low_date']} و{x['high_date']}.")
    if out["offerings"]:
        x=out["offerings"][0];out["summary"].append(f"آخر حدث طرح/تسجيل مرصود بتاريخ {x['date']}: {x['kind']} — {x['status']}.")
    if out["dilution"]:
        x=out["dilution"][0]
        qty=f" وذكر {x.get('shares_mentioned'):,} سهم" if x.get("shares_mentioned") else ""
        out["summary"].append(f"آخر تخفيف/إصدار أسهم مرصود بتاريخ {x['date']}: {x['kind']}{qty}.")
    if out["ownership"]:
        sells=[x for x in out["ownership"] if "بيع" in x.get("change","") or "خفض" in x.get("change","")]
        if sells:out["summary"].append("رُصدت إفصاحات بيع/خفض ملكية: "+"، ".join((x.get("holder") or "مالك مُبلغ") for x in sells[:4])+".")
        else:out["summary"].append("توجد إفصاحات ملكية حديثة، لكن لم نثبت خروجًا صريحًا ضمن الملفات المفحوصة.")
    out["coverage_note"]="SEC: تم فحص مجموعات مستقلة من أحدث ملفات الطرح، الملكية، والإفصاحات الجوهرية حتى لا تطغى كثرة 8-K على Forms 4 أو S-1/424B. النتيجة لا تعني فحص كل التاريخ القديم للشركة. الساعات الممتدة متاحة فقط ضمن نافذة Yahoo الحديثة."
    return out

if __name__=="__main__":
    sym=re.sub(r"[^A-Z0-9.-]","",sys.argv[1].upper())[:12] if len(sys.argv)>1 else ""
    print(json.dumps(asyncio.run(main(sym)),ensure_ascii=False))
