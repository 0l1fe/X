#!/usr/bin/env python3
"""Reliable arXiv math.RA daily archive with loss-prevention and verifiable dates.

Daily RSS is authoritative for date, version, and abstract. Dated catch-up pages
are used for missed days; reconstruction may not recover the historically exact
abstract/version for a cross-listing or replacement. Routine runs NEVER delete
old events or overwrite their metadata with a reconstructed latest version.
All writes are atomic and follow validation. Failures leave archive.json intact.
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
AVAILABILITY_URL = 'https://info.arxiv.org/help/availability.html'
_holiday_cache = None
# arXiv's published 2026 no-announcement holiday dates (availability calendar).
# Unknown weekday gaps are never silently treated as holidays.
HOLIDAYS_2026 = {date.fromisoformat(s) for s in (
    '2026-01-01', '2026-01-19', '2026-06-19', '2026-07-03',
    '2026-09-07', '2026-10-12', '2026-10-13', '2026-11-26', '2026-12-25', '2026-12-29',
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


def published_holidays() -> set[date]:
    """Read arXiv's current public holiday list, for future years too.

    A failure to fetch the calendar must never be interpreted as a holiday.
    Special ad-hoc deferrals (e.g. 2026-10-12/13) remain explicitly listed.
    """
    global _holiday_cache
    if _holiday_cache is None:
        raw=download(AVAILABILITY_URL)
        soup=BeautifulSoup(raw,'html.parser')
        calendar=soup.get_text(' ',strip=True)
        candidates=re.findall(r'20\d\d-\d\d-\d\d',calendar)
        _holiday_cache={date.fromisoformat(x) for x in candidates}
        if not _holiday_cache:
            raise RuntimeError('Could not parse arXiv published holiday calendar')
    return _holiday_cache


def is_holiday(day: date) -> bool:
    if day in HOLIDAYS_2026: return True
    if day.year <= 2026: return False
    return day in published_holidays()


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
        if CATEGORY not in entry['categories']:
            raise ValueError(f'RSS returned a paper outside {CATEGORY}: {key}')
        if key in result:
            raise ValueError(f'RSS contains duplicate announcement {key}')
        result[key] = entry
    if len(result) >= 2000:
        raise RuntimeError('RSS returned 2000 items: possible service cap; cannot guarantee completeness')
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
    if day.weekday() >= 5 or is_holiday(day):
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


def api_details(requests: set[tuple[str, bool]]) -> dict[tuple[str, bool], dict]:
    """Request v1 for new papers; latest-version metadata otherwise.

    Reconstructed cross/replacements deliberately remain versionless because
    a daily listing alone does not establish the announced version number.
    Separate v1 and latest calls so both can be requested for the same ID.
    """
    result = {}
    for is_new in (True, False):
        ids=sorted(pid for pid, mode in requests if mode == is_new)
        for pos in range(0, len(ids), 30):
            subset=ids[pos:pos+30]
            requested_ids=[pid+('v1' if is_new else '') for pid in subset]
            url=API+'?'+urllib.parse.urlencode({'id_list': ','.join(requested_ids),
                                                  'max_results': len(subset)})
            root=ET.fromstring(download(url))
            if root.tag != ATOM+'feed':
                raise ValueError('arXiv API returned invalid Atom XML')
            if (root.findtext(ATOM+'title') or '').strip().lower().startswith('error'):
                raise ValueError('arXiv API returned an error feed')
            for entry in root.findall(ATOM+'entry'):
                id_url=entry.findtext(ATOM+'id') or ''
                pid=paper_id(id_url)
                key=(pid,is_new)
                if pid not in subset or key in result:
                    raise ValueError(f'Unexpected or duplicate API record: {pid}')
                if is_new and not id_url.endswith(pid+'v1'):
                    raise ValueError(f'Version-specific request did not return v1: {pid}')
                title=' '.join((entry.findtext(ATOM+'title') or '').split())
                abstract=' '.join((entry.findtext(ATOM+'summary') or '').split())
                authors=', '.join(' '.join((n.findtext(ATOM+'name') or '').split())
                                  for n in entry.findall(ATOM+'author'))
                cats=list(dict.fromkeys(e.attrib['term'] for e in entry.findall(ATOM+'category')
                                        if e.attrib.get('term')))
                if not (title and abstract and authors):
                    raise ValueError(f'Incomplete API metadata for {pid}')
                if CATEGORY not in cats: cats.append(CATEGORY)
                result[key]=dict(title=title,abstract=abstract,authors=authors,
                                 categories=cats,
                                 guid=f'oai:arXiv.org:{pid}'+('v1' if is_new else ''))
            print(f'  Retrieved {len(result)} / {len(requests)} historical metadata entries',
                  flush=True)
    missing=requests-set(result)
    if missing:
        raise RuntimeError(f'Historical API omitted {len(missing)} records, '
                           f'examples: {sorted(missing)[:5]}')
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


def collect_days(days: list[date], rss: dict[tuple[str,str],dict],
                 existing: dict[tuple[str,str],dict]) -> dict[tuple[str,str],dict]:
    """Verify catch-up lists, but do not fetch or replace metadata for known events."""
    announcements = {}
    today = datetime.now(timezone.utc).date()
    for day in days:
        # RSS is authoritative on its own batch day; catch-up pages can lag it.
        todays = {k:v for k,v in rss.items() if k[0] == day.isoformat()}
        if day == today:
            announcements.update(todays)
            print(f'  {day}: {len(todays)} same-day RSS items', flush=True)
            continue
        listed = catchup_day(day)
        for pid, kind in listed:
            key = (day.isoformat(),pid)
            if key in announcements:
                raise RuntimeError(f'Duplicate event in catch-up: {key}')
            announcements[key] = (day,pid,kind)
        if todays:
            listing_ids = {pid for pid,_ in listed}
            rss_ids = {k[1] for k in todays}
            if listing_ids != rss_ids:
                raise RuntimeError(f'RSS and historical listing disagree for {day}: '
                                   f'{len(rss_ids)} RSS vs {len(listing_ids)} listing, '
                                   f'RSS-only={sorted(rss_ids-listing_ids)[:3]}, '
                                   f'listing-only={sorted(listing_ids-rss_ids)[:3]}')
    # Fetch API metadata ONLY for new, missing archive keys. Prefer original RSS.
    needs = set()
    for key, evt in announcements.items():
        if key not in existing and key not in rss and isinstance(evt,tuple):
            day,pid,kind = evt
            needs.add((pid, kind == 'new'))
    meta = api_details(needs) if needs else {}
    entries = {}
    for key, evt in announcements.items():
        if key in rss:
            entries[key] = rss[key]
        elif key in existing:
            entries[key] = existing[key]
        else:
            day,pid,kind = evt
            m=meta[(pid,kind == 'new')]
            label=m['guid'].split(':')[-1]
            description=f'arXiv:{label} Announce Type: {kind} \nAbstract: {m["abstract"]}'
            entries[key]=record(title=m['title'], stamp=announcement_stamp(day),
                                pid=pid, guid=m['guid'], author=m['authors'],
                                description=description, categories=m['categories'],
                                source=f'{CATCHUP}/{day.isoformat()}')
            if kind != 'new':
                print(f'::warning::Historical {kind} metadata for {pid} on {day} '
                      'may reflect a newer abstract; version intentionally unspecified.', flush=True)
    # RSS may cover a batch date outside the dated verification window.
    entries.update(rss)
    return entries


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--rebuild-since', metavar='YYYY-MM-DD',
                    help='Explicit full replacement ONLY after every requested day validates')
    ap.add_argument('--lookback-days', type=int, default=5)
    ap.add_argument('--preview', action='store_true')
    args = ap.parse_args()
    today = datetime.now(timezone.utc).date()
    previous = load_archive()
    existing = {}
    for e in previous:
        if set(e) != set(FIELDS):
            raise ValueError('Existing archive schema is incompatible with website')
        key = event_key(e)
        if key in existing:
            raise ValueError(f'Existing archive has a duplicate announcement: {key}')
        existing[key] = e
    if args.rebuild_since:
        start = date.fromisoformat(args.rebuild_since)
        if start > today or (today-start).days > 85:
            raise ValueError('Historical rebuild start must be in the last 85 days')
    else:
        if not 2 <= args.lookback_days <= 14:
            raise ValueError('lookback-days must be between 2 and 14')
        start = today - timedelta(days=args.lookback_days)
        if previous:
            last=max(date.fromisoformat(e['pubDate'][:10]) for e in previous)
            # Allow for missed GitHub schedules, with overlapping coverage.
            start=min(start,last - timedelta(days=2))
            if (today-start).days > 85:
                raise RuntimeError('Archive >85 days behind: unable to guarantee backfill')
    print(f'Checking math.RA announcement days {start} through {today}', flush=True)
    raw=download(RSS)  # Invalid/unavailable RSS is an error: never silently publish.
    rss=parse_rss(raw)
    # Keep source data for dates >= requested window, not a stale old batch.
    rss={key:e for key,e in rss.items() if key[0] >= start.isoformat()}
    print(f'Official RSS contains {len(rss)} entries in the processing window.', flush=True)
    days=[start+timedelta(days=i) for i in range((today-start).days+1)]
    new=collect_days(days,rss,existing if not args.rebuild_since else {})
    # On working weekdays after the daily publication time, a missing batch
    # should be a visible failure, not silently treated as a zero-paper day.
    if (today.weekday() < 5 and not is_holiday(today)
            and datetime.now(timezone.utc).hour >= 10
            and not any(k[0] == today.isoformat() for k in rss)):
        raise RuntimeError('Expected an arXiv announcement today but RSS is empty. '
                           'This could be a holiday, late feed, or outage. '
                           'Archive unchanged; automatic retry will run later.')
    if not new:
        # No announcements on holidays/weekends is legitimate. Never destroy old archive.
        print('No new announcements; existing archive unchanged.', flush=True)
        return
    if args.rebuild_since:
        merged={k:e for k,e in new.items() if k[0] >= start.isoformat()}
    else:
        # Strictly additive: only an authentic daily RSS item may update an
        # existing event. Catch-up API metadata may never replace archived data.
        merged=dict(existing)
        for key,e in new.items():
            if key not in merged or e.get('__source') == RSS:
                merged[key]=e
        missing_keys=set(existing)-set(merged)
        if missing_keys:
            raise AssertionError(f'Incremental update attempted deletion: {len(missing_keys)}')
    ordered=sorted(merged.values(), key=lambda x:(x['pubDate'],x['link']), reverse=True)
    if len(ordered) != len(merged):
        raise AssertionError('Duplicate items in output')
    print(f'VALIDATED: {len(ordered)} archived announcements across '
          f'{len(set(e["pubDate"][:10] for e in ordered))} days.',flush=True)
    counts=Counter(e['pubDate'][:10] for e in ordered)
    for d in sorted(counts):
        if d>=start.isoformat(): print(f'  {d}: {counts[d]}',flush=True)
    if args.preview:
        print('PREVIEW: no files modified.',flush=True)
    else:
        if ordered == previous:
            print('No archive changes.',flush=True)
        else:
            safe_write(ordered)
            print(f'WROTE validated archive safely: {len(ordered)} records.',flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'::error::Archive NOT replaced: {exc}',file=sys.stderr,flush=True)
        raise SystemExit(1)
