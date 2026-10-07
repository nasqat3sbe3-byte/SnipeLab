"""Conservative, dated event evidence for the Snipe AI exclusion gate."""
import re
from datetime import datetime

MONTH = r'(?:January|February|March|April|May|June|July|August|September|October|November|December)'
DATE = re.compile(r'\b('+MONTH+r'\s+\d{1,2},?\s+20\d{2}|20\d{2}-\d{2}-\d{2})\b', re.I)
HISTORICAL = re.compile(r'previously|historically|incorporat\w* by reference|for the (?:six|three|twelve) months|proceeds from|cash flows?|registration statement|risk factors|may (?:offer|sell|issue)|from time to time|last (?:year|quarter|month)|in '+MONTH+r'\b|in 20\d{2}|had (?:announced|entered|completed)|cancel\w*|terminat\w*|withdraw\w*|no (?:new |planned )?(?:public offering|private placement)|not (?:announc\w*|enter\w*|complet\w*)', re.I)
ACTION = re.compile(r'\b(?:announces?|announced|entered into|has entered into|priced|prices|closed|closes|completed|commenced|launched|filed|files|received|receives|issued|issues|failed|fails|did not meet|defaults?|defaulted|ceases?|ceased)\b', re.I)

def parse_day(value):
    for fmt in ('%Y-%m-%d', '%B %d, %Y', '%B %d %Y'):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None

def dated_risk_evidence(text, published_day, today, classify):
    """Require an affirmative event statement, preserve event dates and evidence.

    Filing type, boilerplate and historical accounting references are never proof.
    A results report requires an explicit date in the event statement; a fresh
    standalone announcement may use its dated publication as the announcement day.
    """
    text = re.sub(r'\s+', ' ', text)
    report = bool(re.search(r'financial results|financial statements|annual report|interim results|quarterly results|earnings results', text, re.I))
    # Avoid decimal punctuation; SEC HTML is flattened before reaching this layer.
    sentences = re.split(r'(?<=[.!?;])\s+(?=[A-Z])', text)
    for sentence in sentences:
        kind = classify(sentence)
        if not kind or not ACTION.search(sentence) or HISTORICAL.search(sentence):
            continue
        dates = [parse_day(m.group(1)) for m in DATE.finditer(sentence)]
        dates = [d for d in dates if d is not None]
        if report and not dates:
            continue
        # Multiple dates are ambiguous: do not substitute the latest date.
        if len(set(dates)) > 1:
            continue
        day = dates[0] if dates else published_day
        if not 0 <= (today-day).days <= 30:
            continue
        return {'kind': kind, 'date': day.isoformat(),
                'published_date': published_day.isoformat(),
                'evidence': sentence[:1500], 'verified': True,
                'date_basis': 'explicit_event_date' if dates else 'dated_announcement'}
    return None
