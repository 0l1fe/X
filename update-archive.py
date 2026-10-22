#!/usr/bin/env python3
"""math.RA announcement archive; no rss2json, no submission-date guessing.

Daily: official arXiv RSS (correct announcement day and event types).
Rebuild: arXiv's *dated* catchup listing, then arXiv API for metadata.
If the catchup page does not provide a verifiable, complete dated listing,
this program REFUSES to replace the archive instead of silently switching to
submission dates. The HTML parser is deliberately strict and should be checked
against the live arXiv page in GitHub Actions before enabling a rebuild.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT / 'archive.json'
RSS = 'https://rss.arxiv.org/rss/math.RA'
CATCHUP = 'https://arxiv.org/catchup/math.RA'
API = 'https://export.arxiv.org/api/query'
ATOM = '{http://www.w3.org/2005/Atom}'
DC = '{http://purl.org/dc/elements/1.1/}creator'
NEW_YORK = ZoneInfo('America/New_York')
USER_AGENT = 'X-mathRA-archive/3.0 (https://github.com/0l1fe/X)'
FIELDS = ('title','pubDate','link','guid','author','thumbnail','description',
          'content','enclosure','categories','__source')
LAST_REQUEST = 0.0


def get(url: str, *, retries: int = 4) -> bytes:
    """Rate-limited, bounded read; fail loudly for non-XML/non-HTML responses."""
    global LAST_REQUEST
    for attempt in range(retries):
        delta = time.monotonic() - LAST_REQUEST
        if delta < 3.5:
            time.sleep(3.5 - delta)
        LAST_REQUEST = time.monotonic()
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': USER_AGENT,
                'Accept': 'application/xml, application/rss+xml, application/atom+xml, text/html',
            })
            with urllib.request.urlopen(req, timeout=45) as res:
                if res.status != 200:
                    raise RuntimeError(f'HTTP {res.status}: {url}')
                raw = res.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise RuntimeError('Refusing response larger than 16 MB')
                return raw
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            if attempt == retries - 1:
                raise RuntimeError(f'Cannot fetch {url}: {exc}') from exc
            wait = 5 * (2 ** attempt)
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                retry_after = exc.headers.get('Retry-After','')
                if retry_after.isdigit():
                    wait = min(120, max(wait, int(retry_after)))
            print(f'Fetch failed; retrying after {wait}s: {exc}', flush=True)
            time.sleep(wait)
    raise AssertionError('unreachable')


def elem_text(e, path):
    return ' '.join((e.findtext(path) or '').split())


def normalized_date(value: str) -> str:
    try:
        d = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        d = parsedate_to_datetime(value)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def announcement_time(day: date) -> str:
    """Official RSS dates are midnight Eastern converted to UTC (DST aware)."""
    midnight = datetime(day.year,day.month,day.day,tzinfo=NEW_YORK)
    return midnight.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def bare_id(value: str) -> str:
    value = value.split('/abs/')[-1].replace('oai:arXiv.org:', '').strip('/')
    return re.sub(r'v\d+$', '', value, flags=re.I)


def event_key(record: dict) -> tuple[str, str]:
    """One paper per announcement day; a later revision day remains visible.

    The original RSS and a historical listing may call the same event by
    different announcement types; those must not create duplicate cards.
    """
    return (record['pubDate'][:10], bare_id(record.get('guid') or record['link']).lower())


def pack(title, pub_date, link, guid, author, description, categories, source):
    if not title or not link or not description or not pub_date:
        raise ValueError(f'Incomplete metadata for {link}')
    return dict(zip(FIELDS, (title, normalized_date(pub_date), link, guid,
        author, '', description, description, {}, categories, source)))


def parse_rss(raw: bytes) -> list[dict]:
    root = ET.fromstring(raw)
    channel = root.find('channel')
    if root.tag != 'rss' or channel is None:
        raise ValueError('Expected official RSS 2.0 channel')
    out=[]
    for it in channel.findall('item'):
        def val(path):return (it.findtext(path) or '').strip()
        categories=[x.text.strip() for x in it.findall('category') if x.text and x.text.strip()]
        out.append(pack(val('title'),val('pubDate'),val('link'),val('guid'),
                        val(DC),val('description'),categories,RSS))
    if len({event_key(x) for x in out}) != len(out):
        raise ValueError('Repeated announcement keys in RSS; refusing silent data loss')
    return out


DATE_HEADING = re.compile(r'\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),?\s+(\d{1,2})\s+'
                          r'([A-Z][a-z]{2})\s+(20\d\d)\b')
COUNT_HEADING = re.compile(r'\bshowing\s+(\d+)\s+of\s+(\d+)\s+entries\b', re.I)
TOTAL_COUNT = re.compile(r'\bTotal of\s+(\d+)\s+entries\b', re.I)
ABS_RE = re.compile(r'^/(?:abs|pdf)/([^?#]+)', re.I)


class DatedListing(HTMLParser):
    """Extract paper IDs and days from a genuine arXiv <dl><dt> listing.

    Date headings must be present. A category-selection form does NOT qualify.
    Counts in the HTML are checked before we permit a destructive rebuild.
    """
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.heading_depth = 0
        self.heading_text = []
        self.in_dt = False
        self.date = None
        self.kind = 'historical listing'
        self.days = {}
        self.stated_counts = {}
        self.paging_counts = []
        self.notices = []
        self.pending_id = None
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {'h2','h3','h4'}:
            self.heading_depth += 1
            self.heading_text = []
        if tag == 'dt':
            self.in_dt = True
            self.pending_id = None
        if self.in_dt and tag == 'a':
            m = ABS_RE.match(attrs.get('href',''))
            if m and not self.pending_id:
                self.pending_id = bare_id(m.group(1))
    def handle_data(self,data):
        if self.heading_depth:
            self.heading_text.append(data)
    def handle_endtag(self,tag):
        if tag in {'h2','h3','h4'} and self.heading_depth:
            self.heading_depth -= 1
            self._heading(' '.join(' '.join(self.heading_text).split()))
            self.heading_text=[]
        if tag == 'dt' and self.in_dt:
            if self.pending_id and self.date:
                self.days.setdefault(self.date,[]).append((self.pending_id,self.kind))
            self.in_dt=False
    def _heading(self, line):
        m=DATE_HEADING.search(line)
        if m:
            self.date = datetime.strptime(' '.join(m.groups()), '%d %b %Y').date()
            self.kind='historical listing'
            cm=COUNT_HEADING.search(line)
            if cm:
                showing,total=map(int,cm.groups())
                if showing != total:
                    raise ValueError(f'Only partial listing for {self.date}: {showing}/{total}')
                self.stated_counts[self.date]=total
            return
        lower=line.lower()
        if 'new submissions' in lower: self.kind='new'
        elif 'cross-lists' in lower: self.kind='cross'
        elif 'replacements' in lower: self.kind='replace'


def parse_dated_listing(raw: bytes, *, since: date, today: date) -> dict[date,list[tuple[str,str]]]:
    page=DatedListing()
    page.feed(raw.decode('utf-8','replace'))
    if not page.days:
        raise ValueError('arXiv catchup did not expose dated <dt> entries; REFUSING rebuild')
    # A page without explicit totals cannot prove the listing is complete.
    # Reject it instead of manufacturing a reassuring archive.
    if len(page.stated_counts) != len(page.days):
        raise ValueError('No verified entry counts for every arXiv announcement day')
    # Explicit per-day total verification.
    for day, total in page.stated_counts.items():
        if len(page.days.get(day, [])) != total:
            raise ValueError(f'Incomplete arXiv listing for {day}: '
                             f'{len(page.days.get(day, []))}/{total}')
    if max(page.days) < today - timedelta(days=3):
        raise ValueError('Historical listing is stale; missing recent announcements')
    earliest=min(page.days)
    if earliest > since + timedelta(days=2):
        raise ValueError(f'Historical listing starts on {earliest}, not {since}; '
                         'rebuild range incomplete')
    desired={d:rows for d,rows in page.days.items() if since <= d <= today}
    if not desired:
        raise ValueError('No announcements in requested rebuild range')
    for d,entries in desired.items():
        if len(entries)!=len(set(p for p,_ in entries)):
            raise ValueError(f'Listing duplicated a date/type/paper event on {d}')
    return desired


def read_atom_metadata(raw: bytes) -> dict[str,dict]:
    root=ET.fromstring(raw)
    if root.tag != ATOM+'feed':
        raise ValueError('arXiv API did not supply Atom XML')
    title=elem_text(root, ATOM+'title')
    if title.lower().startswith('error'):
        raise ValueError('arXiv API error: '+title)
    found={}
    for en in root.findall(ATOM+'entry'):
        paper_id=(en.findtext(ATOM+'id') or '').split('/')[-1]
        key=bare_id(paper_id)
        categories=list(dict.fromkeys([x.attrib['term'] for x in en.findall(ATOM+'category')
                                       if x.get('term')]))
        authors=', '.join(elem_text(x,ATOM+'name') for x in en.findall(ATOM+'author'))
        summary=elem_text(en,ATOM+'summary')
        if not key or not summary:
            raise ValueError('Incomplete arXiv API metadata')
        found[key]={
            'title':elem_text(en,ATOM+'title'),
            'guid':'oai:arXiv.org:'+paper_id,
            'author':authors,
            'description_abstract':summary,
            'categories':categories,
            'link':'https://arxiv.org/abs/'+key,
        }
    return found


def metadata_for_ids(ids: set[str]) -> dict[str,dict]:
    """All pages checked; no fixed total-result cap and no incomplete responses."""
    out={}
    unique=sorted(ids)
    for n in range(0,len(unique),80):
        batch=unique[n:n+80]
        url=API+'?'+urllib.parse.urlencode({'id_list':','.join(batch),'max_results':len(batch)})
        got=read_atom_metadata(get(url))
        missing=set(batch)-set(got)
        if missing:
            raise ValueError(f'API missing metadata for {len(missing)} papers: {sorted(missing)[:4]}')
        out.update(got)
    return out


def from_dated_listing(days:dict[date,list[tuple[str,str]]], info:dict[str,dict]) -> list[dict]:
    out=[]
    for day, events in days.items():
        for paper_id,kind in events:
            entry=info[paper_id]
            guid=entry['guid']
            identifier=guid.replace('oai:arXiv.org:','')
            abstract=entry['description_abstract']
            # Current version metadata can be newer than the version announced
            # historically; type may also be unresolvable for some listings.
            description=f'arXiv:{identifier} Announce Type: {kind} \nAbstract: {abstract}'
            out.append(pack(entry['title'],announcement_time(day),entry['link'],guid,
                            entry['author'],description,entry['categories'],CATCHUP))
    return out


def load(path=ARCHIVE):
    if not path.exists(): return []
    rows=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(rows,list) or any(set(x)!=set(FIELDS) for x in rows):
        raise ValueError('Existing archive does not have the expected 11-field structure')
    return rows


def replace_atomically(path,rows):
    data=json.dumps(rows,ensure_ascii=False,indent=2)+'\n'
    with tempfile.NamedTemporaryFile(mode='w',encoding='utf8',delete=False,
                                    dir=path.parent,prefix='.pending-archive-',suffix='.json') as f:
        name=f.name
        try:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        except Exception:
            f.close()
            os.unlink(name)
            raise
    os.replace(name,path)


def merge(rows, incoming, *, authoritative=True):
    """Keep exact RSS metadata where a dated-listing record describes same event."""
    output={event_key(x): x for x in rows}
    for x in incoming:
        key=event_key(x)
        if key not in output or x['__source'] == RSS:
            output[key]=x
    return sorted(output.values(),key=lambda x:(x['pubDate'],x['guid']),reverse=True)


def get_dated_announcements(since: date, today: date, *, max_age=90):
    if since > today:
        raise ValueError('Start date must not be in the future')
    if (today-since).days > max_age:
        raise ValueError('Historical category catchup supports only about 90 days. '
                         'Refusing to guess announcement dates for older records.')
    url=CATCHUP+'?'+urllib.parse.urlencode({'days':(today-since).days+3,'show':2000})
    return parse_dated_listing(get(url),since=since,today=today)


def do_audit(path=ARCHIVE, *, since: str):
    """Non-destructive reconciliation against arXiv's official dated listing."""
    current=load(path)
    first=date.fromisoformat(since)
    today=datetime.now(NEW_YORK).date()
    days=get_dated_announcements(first,today)
    actual={(d.isoformat(),paper.lower()) for d, entries in days.items() for paper,_ in entries}
    present={event_key(row) for row in current
             if first.isoformat()<=row['pubDate'][:10]<=today.isoformat()}
    missing=sorted(actual-present)
    unexpected=sorted(present-actual)
    found_days=sorted(set(d.isoformat() for d in days))
    details=[]
    for day in found_days:
        expected=sum(d==day for d, _ in actual)
        observed=sum(d==day for d,_ in present)
        details.append({'date':day,'arxiv':expected,'archive':observed})
    # A misdated paper is present in archive but under the wrong day.
    shifted=[]
    by_paper={}
    for d, paper in present:
        by_paper.setdefault(paper,[]).append(d)
    for d, paper in missing:
        if paper in by_paper:
            shifted.append({'id':paper,'official_day':d,'archive_days':by_paper[paper]})
    report={'category':'math.RA','since':since,'through':today.isoformat(),
            'arxiv_total':len(actual),'archive_total':len(present),
            'missing_count':len(missing),'extra_count':len(unexpected),
            'different_date_examples':shifted[:100],
            'by_day':details, 'missing_announcements':missing,
            'unexpected_archive_records':unexpected,
            'note':'Listing is official, but current-version API metadata is not the original RSS snapshot.'}
    report_file=path.parent/'archive-audit-report.json'
    report_file.write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    print('COMPLETE ARCHIVE AUDIT\n',json.dumps({k:report[k] for k in
          ('arxiv_total','archive_total','missing_count','extra_count','by_day')},indent=2),flush=True)
    print(f'Full audit report: {report_file}; '
          f'{len(shifted)} probable wrong-date records',flush=True)
    if missing or unexpected:
        print(f'::warning:: AUDIT MISMATCH: {len(missing)} missing and '
              f'{len(unexpected)} unexpected dated records',flush=True)
    return report


def do_update(path=ARCHIVE, *, rebuild_since=None, preview=False, max_age=90, rolling_days=14):
    current=load(path)
    rss=parse_rss(get(RSS))
    today=datetime.now(NEW_YORK).date()
    if rebuild_since:
        since=date.fromisoformat(rebuild_since)
        if since>today: raise ValueError('Date is in the future')
        dated=get_dated_announcements(since,today,max_age=max_age)
        all_ids={p for events in dated.values() for p,_ in events}
        meta=metadata_for_ids(all_ids)
        rebuilt=from_dated_listing(dated,meta)
        # Use full RSS versions and exact event types when both identify the same day+paper.
        keyed={(e['pubDate'][:10],bare_id(e['guid']).lower()):e for e in rebuilt}
        for e in rss:
            key=(e['pubDate'][:10],bare_id(e['guid']).lower())
            if key in keyed:
                keyed[key]=e
            elif since <= date.fromisoformat(e['pubDate'][:10]) <= today:
                # If RSS has an event absent in dated catchup, do not silently discard it;
                # instead flag the discrepancy for investigation.
                raise ValueError(f'RSS announcement absent from catchup listing: {e["guid"]}')
        new=sorted(keyed.values(),key=lambda x:(x['pubDate'],x['guid']),reverse=True)
        if len(new) != sum(len(x) for x in dated.values()):
            raise ValueError('Rebuild count differs from authoritative dated listing')
        print('REBUILD AUDIT', json.dumps({str(d):len(x) for d,x in sorted(dated.items())}),flush=True)
        print(f'REBUILD VERIFIED: {len(new)} dated events from {since} through {today}; '
              f'existing archive has {len(current)}',flush=True)
    else:
        # Self-heal missed announcements during outages: re-check the last
        # rolling_days against the *dated* category listing every scheduled run.
        # Exclude TODAY from reconciliation to avoid racing arXiv publication:
        # today's records come authoritatively from today's official RSS.
        since=today-timedelta(days=rolling_days)
        recent_end=today-timedelta(days=1)
        url=CATCHUP+'?'+urllib.parse.urlencode({'days':rolling_days+3,'show':2000})
        try:
            dated=parse_dated_listing(get(url),since=since,today=recent_end)
            # Retain exact RSS metadata already captured; fetch metadata only
            # for announcements not in the existing archive.
            originals={event_key(x):x for x in current if x['__source']==RSS}
            originals.update({event_key(x):x for x in rss})
            needed_ids={paper for day,events in dated.items() for paper,_ in events
                        if (day.isoformat(),paper.lower()) not in originals}
            metadata=metadata_for_ids(needed_ids) if needed_ids else {}
            repaired=[]
            for day,events in dated.items():
                for paper,kind in events:
                    key=(day.isoformat(),paper.lower())
                    if key in originals:
                        repaired.append(originals[key])
                    else:
                        repaired.extend(from_dated_listing({day:[(paper,kind)]},metadata))
            # The rebuild is a replacement of the verified rolling window;
            # preserve all data prior to it, and preserve today's RSS too.
            preserved=[x for x in current if x['pubDate'][:10]<since.isoformat()
                       or x['pubDate'][:10]>recent_end.isoformat()]
            new=merge(preserved,repaired+rss)
            expected=sum(len(events) for events in dated.values())
            actual=sum(since.isoformat()<=x['pubDate'][:10]<=recent_end.isoformat()
                       for x in new)
            if actual!=expected:
                raise ValueError(f'Rolling audit failed: {actual}/{expected} events')
            print('ROLLING AUDIT',json.dumps({str(d):len(x) for d,x in sorted(dated.items())}),flush=True)
            print(f'Reconciled {rolling_days} days and RSS ({len(rss)} entries); total {len(new)}',flush=True)
        except Exception as exc:
            # Do NOT falsely report that historical completeness is verified.
            # RSS still receives fresh records, and the error is clearly logged.
            # GitHub workflow marks the run degraded after persisting updates.
            print('::warning:: ROLLING AUDIT UNAVAILABLE: '+str(exc),flush=True)
            new=merge(current,rss)
            warning=path.parent/'.arxiv-reconciliation-warning'
            warning.write_text(str(exc)+'\n',encoding='utf8')
            print(f'RSS-only fallback: {len(rss)} entries; total {len(new)}',flush=True)
        else:
            (path.parent/'.arxiv-reconciliation-warning').unlink(missing_ok=True)
    if preview:
        print('PREVIEW MODE: archive.json was not altered.',flush=True)
    else:
        if new != current: replace_atomically(path,new)
        print(f'ARCHIVE WRITTEN: {len(new)} records.',flush=True)
    return len(new)-len(current),len(new)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    exclusive=p.add_mutually_exclusive_group()
    exclusive.add_argument('--rebuild-since',help='Strict rebuild from dated official announcements, YYYY-MM-DD; within past 90 days')
    exclusive.add_argument('--audit-since',help='Compare archive with dated arXiv listing, save archive-audit-report.json; NO data changes')
    p.add_argument('--preview',action='store_true',help='Validate and count without modifying archive.json')
    p.add_argument('--rolling-days',type=int,default=14,help='Rolling announcement reconciliation window, default 14 days')
    args=p.parse_args()
    try:
        if args.rolling_days<2 or args.rolling_days>60:
            raise ValueError('--rolling-days must be between 2 and 60')
        if args.audit_since:
            do_audit(since=args.audit_since)
        else:
            do_update(rebuild_since=args.rebuild_since,preview=args.preview,rolling_days=args.rolling_days)
    except Exception as ex:
        print('::error:: Archive NOT replaced:',ex,file=sys.stderr,flush=True)
        sys.exit(1)

if __name__=='__main__':
    main()
