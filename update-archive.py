#!/usr/bin/env python3
"""Archive every arXiv math.RA announcement by its *announcement day*.

Uses official /catchup/math.RA/YYYY-MM-DD pages to get the day's complete set
of new/cross-listed/replacement announcements and the arXiv Atom API to fill
metadata, with the official daily RSS preferred when available. No rss2json.

Rebuild replaces archive.json only after *all* requested days and metadata are
validated. Fail closed instead of quietly publishing an incomplete archive.
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
from pathlib import Path
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

CATEGORY = 'math.RA'
ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT / 'archive.json'
RSS = f'https://rss.arxiv.org/rss/{CATEGORY}'
API = 'https://export.arxiv.org/api/query'
CATCHUP = f'https://arxiv.org/catchup/{CATEGORY}'
FIELDS = ('title', 'pubDate', 'link', 'guid', 'author', 'thumbnail',
          'description', 'content', 'enclosure', 'categories', '__source')
ATOM = '{http://www.w3.org/2005/Atom}'
DC = '{http://purl.org/dc/elements/1.1/}creator'
NY = ZoneInfo('America/New_York')
UA = 'math.RA archive github.com/0l1fe/X (academic RSS reader)'
MAX_BYTES = 16 * 1024 * 1024
# arXiv's published 2026 no-announcement holiday dates (availability calendar).
# Unknown weekday gaps are never silently treated as holidays.
HOLIDAYS_2026 = {date.fromisoformat(s) for s in (
    '2026-01-01', '2026-01-19', '2026-06-19', '2026-07-03',
    '2026-09-07', '2026-10-12', '2026-11-26', '2026-12-25', '2026-12-29',
    '2026-12-31')}

_last_request = 0.0
ID_PATTERN = re.compile(r'(?:arxiv:|/abs/)(\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?', re.I)
COUNT_PATTERN = re.compile(r'\bTotal\s+of\s+([\d,]+)\s+entries\b', re.I)
SECTION_PATTERN = re.compile(r'\b(New\s+submissions|Cross[ -]?lists|Replacements)\s*\(\s*([\d,]+)\s*\)', re.I)


def download(url: str, retries: int = 4) -> bytes:
    global _last_request
    for attempt in range(retries):
        delay = max(0, 4 - (time.monotonic() - _last_request))
        if delay:
            time.sleep(delay)
        _last_request = time.monotonic()
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': UA,
                'Accept': 'text/html, application/rss+xml, application/atom+xml, application/xml',
            })
            with urllib.request.urlopen(req, timeout=45) as res:
                if res.status != 200:
                    raise RuntimeError(f'HTTP {res.status}: {url}')
                data = res.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise RuntimeError(f'Oversized response: {url}')
                return data
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt == retries-1:
                raise RuntimeError(f'Cannot fetch {url}: {e}') from e
            cooldown = 5 * (2**attempt)
            if isinstance(e, urllib.error.HTTPError) and e.code == 429:
                value = e.headers.get('Retry-After', '')
                if value.isdecimal():
                    cooldown = min(120, max(cooldown, int(value)))
            print(f'Fetching failed ({e}), retrying in {cooldown}s', flush=True)
            time.sleep(cooldown)
    raise AssertionError('unreachable')


def paper_id(text: str) -> str:
    m = ID_PATTERN.search(text)
    if not m:
        raise ValueError(f'Cannot parse arXiv ID: {text[:100]!r}')
    return m.group(1).lower()


def event_key(entry: dict) -> tuple[str, str]:
    return (entry['pubDate'][:10], paper_id(str(entry.get('link') or entry.get('guid') or '')))


def announcement_stamp(day: date) -> str:
    """arXiv RSS uses 00:00 US Eastern; DST correctly yields 04/05 UTC."""
    return datetime(day.year, day.month, day.day, tzinfo=NY).astimezone(
        timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def standardized_stamp(text: str) -> str:
    try:
        stamp = datetime.fromisoformat(text.strip().replace('Z', '+00:00'))
    except ValueError:
        stamp = parsedate_to_datetime(text.strip())
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def record(*, title: str, stamp: str, pid: str, guid: str,
           author: str, description: str, categories: list[str], source: str) -> dict:
    if not title or not description or not pid:
        raise ValueError(f'Missing metadata: {pid}')
    return dict(zip(FIELDS, (title, stamp, f'https://arxiv.org/abs/{pid}',
                             guid or f'oai:arXiv.org:{pid}', author, '', description,
                             description, {}, categories, source)))


def parse_rss(raw: bytes) -> dict[tuple[str, str], dict]:
    root = ET.fromstring(raw)
    if root.tag != 'rss' or root.find('channel') is None:
        raise ValueError('Official RSS endpoint returned something other than RSS 2.0')
    result = {}
    for item in root.findall('./channel/item'):
        def field(name):
            return (item.findtext(name) or '').strip()
        pid = paper_id(field('link') or field('guid'))
        stamp = standardized_stamp(field('pubDate'))
        entry = record(title=field('title'), stamp=stamp, pid=pid,
                       guid=field('guid'), author=field(DC),
                       description=field('description'),
                       categories=[n.text.strip() for n in item.findall('category') if n.text and n.text.strip()],
                       source=RSS)
        key = event_key(entry)
        if key in result:
            raise ValueError(f'RSS contains duplicate announcement {key}')
        result[key] = entry
    return result


def parse_catchup_html(raw: bytes, day: date) -> tuple[list[tuple[str, str]], int | None, set[int]]:
    """Read one official dated catch-up page; capture IDs, types and pagination.

    Most arXiv list pages use <dl><dt> entries. Section headings identify
    new/cross-list/replacement types. Check page totals and all page links.
    """
    soup = BeautifulSoup(raw, 'html.parser')
    text = soup.get_text(' ', strip=True)
    if not soup.find('html') or not ('catchup' in text.lower() or 'catch-up' in text.lower()):
        raise ValueError(f'Not an arXiv catch-up document for {day}')
    if not (re.search(rf'\b{day.day:02d}\s+{day.strftime("%b")}\s+{day.year}\b', text, re.I)
            or re.search(rf'\b{day.day}\s+{day.strftime("%b")}\s+{day.year}\b', text, re.I)
            or day.isoformat() in text):
        raise ValueError(f'Catch-up document does not verify the requested day {day}')
    totals = [int(c.replace(',', '')) for c in COUNT_PATTERN.findall(text)]
    section_counts = [int(n.replace(',', '')) for _, n in SECTION_PATTERN.findall(text)]
    total = max(totals) if totals else (sum(section_counts) if section_counts else None)
    if total is None:
        raise ValueError(f'Cannot verify arXiv listing total for {day}')
    # Parse sections in document order. Look in the paper-list only.
    group = soup.find(id='content') or soup.find('main') or soup
    kind = 'unknown'
    entries = []
    for tag in group.find_all(['h2','h3','h4','dt']):
        if tag.name != 'dt':
            heading = tag.get_text(' ', strip=True).lower()
            if 'new submissions' in heading:
                kind = 'new'
            elif 'cross-list' in heading or 'cross list' in heading:
                kind = 'cross'
            elif 'replacement' in heading:
                kind = 'replace'
            continue
        link = tag.find('a', href=re.compile(r'/(?:abs|pdf)/'))
        hit = (link.get('href', '') if link else tag.get_text(' ', strip=True))
        try:
            pid = paper_id(hit)
        except ValueError:
            continue
        label = tag.get_text(' ', strip=True).lower()
        actual = kind
        if 'cross-list' in label:
            actual = 'cross'
        elif 'replac' in label:
            actual = 'replace'
        entries.append((pid, actual))
    links = set()
    for a in group.find_all('a', href=True):
        m = re.search(r'(?:[?&]|&amp;)page=(\d+)', a['href'])
        if m:
            links.add(int(m.group(1)))
    if not entries and total:
        raise ValueError(f'No paper entries were decoded from {day} listing')
    if len({p for p,_ in entries}) != len(entries):
        raise ValueError(f'An arXiv ID is repeated on one dated page {day}')
    return entries, total, links


def catchup_day(day: date) -> list[tuple[str, str]]:
    if day.weekday() >= 5 or day in HOLIDAYS_2026:
        return []
    found = []
    total = None
    page = 1
    known_pages = {1}
    while page <= max(known_pages):
        if page > 100:
            raise RuntimeError('Over 100 pages for one category-day; refusing truncation')
        url = f'{CATCHUP}/{day.isoformat()}?abs=True&page={page}'
        try:
            raw = download(url)
        except RuntimeError as exc:
            # A holiday's empty daily catchup may be a 404; never silently
            # skip it because a missing day could also be a real outage.
            raise RuntimeError(f'Dated arXiv listing unavailable for {day}: {exc}') from exc
        batch, batch_total, pages = parse_catchup_html(raw, day)
        if total is not None and batch_total != total:
            raise RuntimeError(f'Inconsistent total on {day}: {total} vs {batch_total}')
        total = batch_total
        found.extend(batch)
        known_pages.update(p for p in pages if p >= 1)
        if total is not None and len(found) == total and page >= max(known_pages):
            break
        # If a document says more items exist, but hides next-page links,
        # abort rather than silently truncating the listing.
        if len(found) < total and page >= max(known_pages):
            raise RuntimeError(f'Pagination missing on {day}: {len(found)} of {total}')
        page += 1
    if len(found) != total:
        raise RuntimeError(f'Incomplete {day} listing: retrieved {len(found)} of {total}')
    if len({p for p,_ in found}) != len(found):
        raise RuntimeError(f'Duplicate paper IDs across {day} pages')
    print(f'  {day}: verified {len(found)} announcements across {page} page(s)', flush=True)
    return found


def api_details(ids: set[str]) -> dict[str,dict]:
    result = {}
    sorted_ids = sorted(ids)
    for pos in range(0,len(sorted_ids), 35):
        subset=sorted_ids[pos:pos+35]
        # arXiv API expects a CSV id_list, and throttles successive calls.
        url = API + '?' + urllib.parse.urlencode({'id_list': ','.join(subset), 'max_results': len(subset)})
        data = download(url)
        root = ET.fromstring(data)
        if root.tag != ATOM+'feed':
            raise ValueError('arXiv metadata API returned invalid Atom XML')
        if (root.findtext(ATOM+'title') or '').strip().lower().startswith('error'):
            raise ValueError('arXiv API reported an error feed')
        for entry in root.findall(ATOM+'entry'):
            id_url = (entry.findtext(ATOM+'id') or '')
            pid = paper_id(id_url)
            title = ' '.join((entry.findtext(ATOM+'title') or '').split())
            abstract = ' '.join((entry.findtext(ATOM+'summary') or '').split())
            authors = ', '.join(' '.join((n.findtext(ATOM+'name') or '').split())
                                for n in entry.findall(ATOM+'author'))
            cats = list(dict.fromkeys(el.attrib['term'] for el in entry.findall(ATOM+'category')
                                      if el.attrib.get('term')))
            version = id_url.rsplit('/',1)[-1]
            if pid in result:
                raise ValueError(f'Duplicate API metadata for {pid}')
            if not title or not abstract or not authors:
                raise ValueError(f'Incomplete API metadata for {pid}')
            result[pid] = dict(title=title, abstract=abstract, authors=authors,
                               categories=cats, guid=f'oai:arXiv.org:{version}')
        print(f'  Metadata downloaded: {len(result)} / {len(ids)}', flush=True)
    missing = ids - set(result)
    extra = set(result) - ids
    if missing or extra:
        raise RuntimeError(f'Incomplete API metadata: {len(missing)} missing, {len(extra)} unexpected; examples {sorted(missing)[:5]}')
    return result


def load_archive() -> list[dict]:
    if not ARCHIVE.exists():
        return []
    data = json.loads(ARCHIVE.read_text(encoding='utf-8'))
    if not isinstance(data,list) or any(not isinstance(n,dict) for n in data):
        raise ValueError('Existing archive.json is not an array of objects')
    return data


def safe_write(entries: list[dict]) -> None:
    if not entries:
        raise RuntimeError('Refusing to write an empty archive')
    if any(set(e) != set(FIELDS) for e in entries):
        raise ValueError('Record schema is incompatible with the existing website')
    encoded = json.dumps(entries, indent=2, ensure_ascii=False) + '\n'
    fd,path = tempfile.mkstemp(dir=ARCHIVE.parent, prefix='.archive-', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(encoded)
        os.replace(path, ARCHIVE)
    finally:
        if os.path.exists(path):
            os.unlink(path)


def collect_days(days: list[date], rss: dict[tuple[str,str],dict]) -> dict[tuple[str,str],dict]:
    announcements = {}
    for day in days:
        # The current day's listing sometimes lags the RSS; trust today's RSS
        # rather than inventing a historical date from submission timestamp.
        if day == datetime.now(timezone.utc).date():
            todays = {k:v for k,v in rss.items() if k[0] == day.isoformat()}
            if todays:
                announcements.update(todays)
                print(f'  {day}: {len(todays)} current-day RSS announcements', flush=True)
                continue
            # no RSS available for current day -- tomorrow's run will revisit it
            continue
        for pid, kind in catchup_day(day):
            key = (day.isoformat(), pid)
            if key in announcements:
                raise RuntimeError(f'Repeated event {key}')
            announcements[key] = (day, pid, kind)
    ids = {item[1] for item in announcements.values() if isinstance(item,tuple) and len(item)==3
           and (item[0].isoformat(),item[1]) not in rss}
    meta = api_details(ids) if ids else {}
    entries = {}
    for key, evt in announcements.items():
        if key in rss:
            entries[key] = rss[key]
            continue
        day,pid,kind = evt
        m = meta[pid]
        descriptor = f'arXiv:{m["guid"].split(":")[-1]} Announce Type: {kind} \nAbstract: {m["abstract"]}'
        entries[key] = record(title=m['title'], stamp=announcement_stamp(day),
                              pid=pid, guid=m['guid'], author=m['authors'],
                              description=descriptor, categories=m['categories'],
                              source=f'{CATCHUP}/{day.isoformat()}')
    # RSS entries beyond the catchup's lookback are still authoritative today.
    # Keep RSS only for the dates being rebuilt/verified; a stale feed
    # must never pull entries earlier than the chosen rebuild start.
    entries.update(rss)
    return entries


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--rebuild-since', metavar='YYYY-MM-DD', help='Replace archive after successful complete date-by-date rebuild')
    ap.add_argument('--lookback-days', type=int, default=8, help='Days to recheck on scheduled update')
    ap.add_argument('--preview', action='store_true', help='Fetch and validate but do not write archive.json')
    args=ap.parse_args()
    today=datetime.now(timezone.utc).date()
    previous=load_archive()
    if args.rebuild_since:
        start=date.fromisoformat(args.rebuild_since)
        if start > today or (today-start).days > 85:
            raise ValueError('Rebuild start must be in the last 85 days')
    else:
        if not 3 <= args.lookback_days <= 30:
            raise ValueError('lookback-days must be 3..30')
        start=today-timedelta(days=args.lookback_days)
        if previous:
            # A missed scheduled run can exceed the normal 8-day lookback.
            # Catch up from the last archived announcement automatically.
            last=max(date.fromisoformat(e['pubDate'][:10]) for e in previous)
            start=min(start, last-timedelta(days=2))
            if (today-start).days > 85:
                raise RuntimeError('Archive is over 85 days behind; automatic dated recovery no longer guaranteed')
    print(f'Checking math.RA announcements {start} through {today}.', flush=True)
    rss={}
    try:
        rss=parse_rss(download(RSS))
        print(f'RSS: {len(rss)} verified announcements', flush=True)
    except (RuntimeError,ValueError,ET.ParseError) as exc:
        print(f'::warning::Today RSS unavailable; using dated historical listings: {exc}', flush=True)
    days=[start+timedelta(days=i) for i in range((today-start).days+1)]
    rss = {key:value for key,value in rss.items() if key[0] >= start.isoformat()}
    events=collect_days(days,rss)
    if not events:
        raise RuntimeError('No announcements retrieved; archive left untouched')
    if args.rebuild_since:
        # Rebuild excludes any events earlier than the requested date.
        archive=events
    else:
        # Recheck the recent period; replace it with verified dated events.
        archive={}
        for e in previous:
            key=event_key(e)
            if key[0] < start.isoformat():
                archive[key]=e
        archive.update(events)
    ordered=sorted(archive.values(), key=lambda n:(n['pubDate'],n['link']), reverse=True)
    counts=Counter(e['pubDate'][:10] for e in ordered)
    print(f'ARCHIVE VALIDATED: {len(ordered)} records; {len(counts)} distinct announcement days.', flush=True)
    for day in sorted(counts):
        if day >= start.isoformat():
            print(f'  {day}: {counts[day]} articles', flush=True)
    if args.preview:
        print('PREVIEW ONLY: archive.json was not modified.', flush=True)
    else:
        safe_write(ordered)
        print(f'WROTE archive.json safely ({len(ordered)} records).', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'::error::Archive NOT replaced: {exc}', file=sys.stderr, flush=True)
        raise SystemExit(1)
