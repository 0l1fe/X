#!/usr/bin/env python3
"""Update math.RA archive.json without third-party RSS converters.

RSS announcements use the same eleven keys and values as the old rss2json
archive. Historical API backfill uses the same keys, but cannot reconstruct
all past cross-listings, revisions, or their exact RSS announcement dates.
This script does not modify the HTML, CSS or frontend JavaScript.
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
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT / 'archive.json'
RSS_URL = 'https://rss.arxiv.org/rss/math.RA'
API_URL = 'https://export.arxiv.org/api/query'
ATOM = '{http://www.w3.org/2005/Atom}'
ARXIV_ATOM = '{http://arxiv.org/schemas/atom}'
OPEN_SEARCH = '{http://a9.com/-/spec/opensearch/1.1/}'
DC_CREATOR = '{http://purl.org/dc/elements/1.1/}creator'
SOURCE_LABEL = RSS_URL
USER_AGENT = 'MathRAArchive/2.0 (+https://github.com/0l1fe/X)'
MAX_BYTES = 12 * 1024 * 1024
RETRIES = 3
API_PAGE_SIZE = 200
# Explicitly limit how many results a single run can request, to keep arXiv load modest.
MAX_API_PAGES = 20


def download(url: str) -> bytes:
    for attempt in range(RETRIES):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    'User-Agent': USER_AGENT,
                    'Accept': 'application/rss+xml, application/atom+xml, application/xml, text/xml',
                },
            )
            with urllib.request.urlopen(request, timeout=35) as response:
                if response.status != 200:
                    raise RuntimeError(f'HTTP {response.status} at {url}')
                data = response.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise ValueError(f'Response exceeds {MAX_BYTES} bytes: {url}')
                return data
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == RETRIES - 1:
                raise RuntimeError(f'Unable to download {url}: {exc}') from exc
            delay = 4 * (attempt + 1)
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                retry_after = exc.headers.get('Retry-After', '')
                if retry_after.isdigit():
                    delay = min(max(int(retry_after), delay), 120)
            print(f'Download failed ({exc}); retrying in {delay}s...', flush=True)
            time.sleep(delay)
    raise AssertionError('Unreachable')


def text(node: ET.Element, path: str) -> str:
    value = node.findtext(path)
    return value.strip() if value else ''


def utc_date(value: str) -> datetime:
    """Normalize either existing JSON dates or RSS/Atom dates to aware UTC."""
    value = value.strip()
    if not value:
        raise ValueError('Missing publication date')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        parsed = parsedate_to_datetime(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)  # existing archive stores UTC without a suffix
    return parsed.astimezone(timezone.utc)


def archive_date(value: str) -> str:
    # Compatible with rss2json's "2026-10-09 04:00:00" convention.
    return utc_date(value).strftime('%Y-%m-%d %H:%M:%S')


def record_key(entry: dict) -> str:
    """One archive record per arXiv paper, as in the original link-based updater."""
    link = str(entry.get('link') or '').strip()
    guid = str(entry.get('guid') or '').strip()
    candidate = link or guid
    m = re.search(r'(?:arxiv\.org/abs/|oai:arXiv\.org:)([^?#\s]+)', candidate, re.I)
    if m:
        return re.sub(r'v\d+$', '', m.group(1), flags=re.I).lower().rstrip('/')
    return candidate.rstrip('/').lower()


def make_entry(*, title: str, pub_date: str, link: str, guid: str, author: str,
               categories: list[str], description: str, source: str) -> dict:
    if not title or not link or not description:
        raise ValueError(f'Incomplete arXiv paper metadata: {title!r} / {link!r}')
    return {
        'title': title,
        'pubDate': archive_date(pub_date),
        'link': link,
        'guid': guid or link,
        'author': author,
        'thumbnail': '',
        'description': description,
        'content': description,
        'enclosure': {},
        'categories': categories,
        '__source': source,
    }


def parse_rss(data: bytes) -> list[dict]:
    root = ET.fromstring(data)
    if root.tag != 'rss' or root.find('channel') is None:
        raise ValueError('arXiv RSS endpoint did not return an RSS 2.0 channel')
    entries = []
    for item in root.findall('./channel/item'):
        entries.append(make_entry(
            title=text(item, 'title'),
            pub_date=text(item, 'pubDate'),
            link=text(item, 'link'),
            guid=text(item, 'guid'),
            author=text(item, DC_CREATOR),
            categories=[c.text.strip() for c in item.findall('category') if c.text and c.text.strip()],
            description=text(item, 'description'),
            source=SOURCE_LABEL,
        ))
    return entries


def parse_api(data: bytes) -> tuple[list[dict], int]:
    root = ET.fromstring(data)
    if root.tag != f'{ATOM}feed':
        raise ValueError('arXiv search API did not return an Atom feed')
    # arXiv can return HTTP 200 containing an error instead of a successful feed.
    api_title = text(root, f'{ATOM}title')
    if api_title.lower().startswith('error'):
        raise ValueError(f'arXiv search API error: {api_title}')
    total_text = text(root, f'{OPEN_SEARCH}totalResults')
    if not total_text.isdecimal():
        raise ValueError('arXiv search API did not include a result count')
    result = []
    for entry in root.findall(f'{ATOM}entry'):
        id_url = text(entry, f'{ATOM}id')
        identifier = id_url.rsplit('/', 1)[-1]
        # Atom has separate <author><name> children, rather than a string.
        authors = ', '.join(
            text(author, f'{ATOM}name')
            for author in entry.findall(f'{ATOM}author')
            if text(author, f'{ATOM}name')
        )
        categories = list(dict.fromkeys(
            c.attrib['term'] for c in entry.findall(f'{ATOM}category')
            if c.attrib.get('term')
        ))
        abstract = re.sub(r'\s*\n\s*', ' ', text(entry, f'{ATOM}summary')).strip()
        # API searches operate on original submission dates, not on announcement date.
        published = text(entry, f'{ATOM}published')
        description = f'arXiv:{identifier} Announce Type: historical recovery \nAbstract: {abstract}'
        canonical_id = re.sub(r'v\d+$', '', identifier)
        link = f'https://arxiv.org/abs/{canonical_id}'
        result.append(make_entry(
            title=re.sub(r'\s+', ' ', text(entry, f'{ATOM}title')),
            pub_date=published,
            link=link,
            guid=f'oai:arXiv.org:{identifier}',
            author=authors,
            categories=categories,
            description=description,
            source=API_URL,
        ))
    return result, int(total_text)


def load_archive(path: Path) -> list[dict]:
    if not path.exists():
        print('No existing archive.json found; creating a new archive.')
        return []
    entries = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
        raise ValueError('archive.json must be a JSON array of paper objects')
    return entries


def api_recovery(earliest: datetime, until: datetime) -> list[dict]:
    """Backfill original submissions in math.RA; cannot recover old crosslists/revisions."""
    start = earliest.strftime('%Y%m%d%H%M')
    end = until.strftime('%Y%m%d%H%M')
    search = f'cat:math.RA AND submittedDate:[{start} TO {end}]'
    results: list[dict] = []
    offset = 0
    for page in range(MAX_API_PAGES):
        params = urllib.parse.urlencode({
            'search_query': search, 'start': offset, 'max_results': API_PAGE_SIZE,
            'sortBy': 'submittedDate', 'sortOrder': 'descending',
        })
        if page:
            time.sleep(3)  # arXiv requests a minimum delay between API calls.
        response = download(f'{API_URL}?{params}')
        parsed, total = parse_api(response)
        if not parsed and offset < total:
            raise RuntimeError(f'Incomplete API response: {offset} of {total} entries')
        results.extend(parsed)
        offset += len(parsed)
        if offset >= total:
            break
    else:
        raise RuntimeError('Recovery has too many results for one run; narrow the date window')
    print(f'Historical API returned {len(results)} recent submissions.', flush=True)
    return results


def atomic_write(path: Path, entries: list[dict]) -> None:
    output = json.dumps(entries, ensure_ascii=False, indent=2) + '\n'
    handle, temp_name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as destination:
            destination.write(output)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def update_archive(path: Path = ARCHIVE, *, backfill_days: int = 14,
                   require_backfill: bool = False,
                   rebuild_since: str | None = None) -> tuple[int, int]:
    if not 1 <= backfill_days <= 120:
        raise ValueError('backfill_days must be between 1 and 120')
    # Rebuild mode is transactional: don't remove or overwrite the old archive
    # until both arXiv sources have been fetched, parsed and validated.
    cutoff = None
    if rebuild_since is not None:
        try:
            requested = date.fromisoformat(rebuild_since)
        except ValueError as exc:
            raise ValueError('rebuild_since must be YYYY-MM-DD (e.g. 2026-09-23)') from exc
        cutoff = datetime(requested.year, requested.month, requested.day, tzinfo=timezone.utc)
        if cutoff > datetime.now(timezone.utc):
            raise ValueError('Rebuild start date is in the future')
        print(f'SAFE REBUILD requested from {cutoff:%Y-%m-%d} (UTC).', flush=True)
    existing = load_archive(path)
    print(f'Loaded {len(existing)} archived papers.', flush=True)

    # RSS is authoritative for announcement dates and revisions/crosslistings.
    print(f'Fetching official daily RSS: {RSS_URL}', flush=True)
    rss_entries = parse_rss(download(RSS_URL))
    print(f'RSS supplied {len(rss_entries)} announcements.', flush=True)

    now = datetime.now(timezone.utc)
    if cutoff is not None:
        oldest = cutoff  # exact midnight UTC, rather than a rolling-days approximation
    else:
        oldest = now - timedelta(days=backfill_days)
        dates = []
        for entry in existing:
            try:
                dates.append(utc_date(str(entry.get('pubDate') or '')))
            except ValueError:
                continue
        if dates:
            latest = max(dates)
            # Include downtime from the last archive update, capped to a modest interval.
            oldest = min(oldest, latest - timedelta(days=2))
            oldest = max(oldest, now - timedelta(days=120))
            if latest < now - timedelta(days=118):
                print('::warning::Archive is older than 120 days. Manual historical recovery may be needed.')

    # If search API is down, RSS updates still proceed; a future run will retry a
    # rolling window of at least two weeks. Never misreport API recovery as complete.
    historical = []
    try:
        historical = api_recovery(oldest, now)
    except (RuntimeError, ValueError, ET.ParseError, urllib.error.URLError) as exc:
        if require_backfill or cutoff is not None:
            raise RuntimeError(f'Historical recovery failed: {exc}') from exc
        print(f'::warning::Historical recovery unavailable: {exc}. Daily RSS will still be archived.', flush=True)

    if cutoff is not None:
        # The historical API must actually return results before we replace an
        # existing archive. An empty result is suspicious for this category.
        if not historical:
            raise RuntimeError('Historical API returned no papers; refusing to replace archive.json')
        seen = set()
        new = []
        for item in [*rss_entries, *historical]:
            if utc_date(str(item['pubDate'])) < cutoff:
                continue
            key = record_key(item)
            if not key:
                raise RuntimeError('Unidentified arXiv paper; refusing to replace archive.json')
            if key not in seen:
                seen.add(key)
                new.append(item)
        if not new:
            raise RuntimeError('Rebuild produced zero records; refusing to replace archive.json')
        new.sort(key=lambda item: utc_date(str(item['pubDate'])), reverse=True)
        # Safety validation before performing the one atomic replacement.
        if any(set(item) != {
            'title', 'pubDate', 'link', 'guid', 'author', 'thumbnail',
            'description', 'content', 'enclosure', 'categories', '__source'
        } for item in new):
            raise RuntimeError('Archive schema mismatch; refusing to replace archive.json')
        atomic_write(path, new)
        print(f'REBUILD COMPLETE: wrote {len(new)} unique papers from '
              f'{cutoff:%Y-%m-%d} UTC onward. Previously stored {len(existing)} papers.',
              flush=True)
        print('Historical entries use original submission dates and cannot fully '
              'reconstruct past revision/cross-list RSS announcements.', flush=True)
        return len(new), len(new)

    seen = {key for entry in existing if (key := record_key(entry))}
    new = []
    # Give RSS metadata precedence when an item appears in both sources.
    for item in [*rss_entries, *historical]:
        key = record_key(item)
        if key and key not in seen:
            seen.add(key)
            new.append(item)
    if new:
        merged = [*existing, *new]
        merged.sort(key=lambda item: utc_date(str(item['pubDate'])), reverse=True)
        atomic_write(path, merged)
    print(f'Added {len(new)} papers. Total: {len(existing) + len(new)}. Existing records untouched.', flush=True)
    return len(new), len(existing) + len(new)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backfill-days', type=int, default=14,
                        help='Minimum historical lookback (1-120 days; default 14)')
    parser.add_argument('--require-backfill', action='store_true',
                        help='Fail rather than silently skip an unavailable historical recovery')
    parser.add_argument('--rebuild-since', type=str, default=None,
                        help='Safely replace archive.json with all retrieved math.RA '
                             'papers since the given YYYY-MM-DD (UTC).')
    args = parser.parse_args()
    try:
        update_archive(backfill_days=args.backfill_days,
                       require_backfill=args.require_backfill,
                       rebuild_since=args.rebuild_since)
    except Exception as exc:
        print(f'Archive update failed: {exc}', file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
