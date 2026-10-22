import datetime as dt
import re
from html.parser import HTMLParser
from urllib.parse import urljoin
from zoneinfo import ZoneInfo
from source import BASE, SourceError, native, nodes, state

IST = ZoneInfo('Asia/Kolkata')


def today():
    return dt.datetime.now(IST).strftime('%Y%m%d')


def day_end(day):
    return (dt.datetime.strptime(day, '%Y%m%d').replace(tzinfo=IST)+dt.timedelta(days=1)).timestamp()


class Catalog(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_main = False
        self.href = None
        self.title = ''
        self.movies = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'main':
            self.in_main = True
        if tag == 'a' and self.in_main:
            href = urljoin(BASE, a.get('href', ''))
            self.href = href if re.fullmatch(r'https://in\.bookmyshow\.com/movies/(?:hyderabad/)?[^/]+/ET\d+', href) else None
            self.title = ''
        if tag == 'img' and self.href and a.get('alt'):
            self.title = a['alt']

    def handle_data(self, value):
        if self.href and not self.title and value.strip():
            self.title = value.strip()

    def handle_endtag(self, tag):
        if tag == 'a' and self.href:
            self.movies[self.href] = self.title
            self.href = None
        if tag == 'main':
            self.in_main = False


def movie_pages(url, document):
    event = url.rstrip('/').split('/')[-1]
    queries = state(document).get('synopsisMoviesApi', {}).get('queries', {})
    valid = [q['data'] for q in queries.values() if q.get('status')=='fulfilled'
             and q.get('originalArgs', {}).get('eventcode') == event
             and q.get('originalArgs', {}).get('regionCode') == 'HYD']
    if not valid:
        raise SourceError('Unverified Hyderabad movie data')
    data = valid[0]
    info = data.get('meta', {}).get('event', {})
    if info.get('eventCode') != event:
        raise SourceError('Movie identity mismatch')
    day = today()
    if info.get('releaseDate'):
        day = max(day, dt.datetime.strptime(info['releaseDate'], '%b %d, %Y').strftime('%Y%m%d'))
    codes = set()
    for node in nodes(data):
        if node.get('ctaType') == 'bookTickets' and node.get('isEnabled') is True:
            for option in nodes(node.get('meta', {})):
                if re.fullmatch('ET[0-9]+', str(option.get('eventCode', ''))):
                    codes.add(option['eventCode'])
            if not codes:
                codes.add(event)
    prefix = url.rsplit('/', 1)[0]
    return [{'url': f'{prefix}/buytickets/{code}/{day}', 'event': code,
             'date': day, 'movie': info.get('eventName', '')} for code in sorted(codes)]


def show_data(document, kind, code, day):
    s = state(document)
    if kind == 'venue':
        key = f'getShowtimesByVenue-{code}-{day}'
        query = s.get('venueShowtimesFunctionalApi', {}).get('queries', {}).get(key)
        if not query or query.get('status') != 'fulfilled':
            raise SourceError('Unverified cinema/date data')
        return query['data']
    queries = []
    for name, container in s.items():
        if 'showtime' in name.lower() and isinstance(container, dict):
            queries.extend(container.get('queries', {}).values())
    valid = []
    for q in queries:
        args = q.get('originalArgs', {})
        if (q.get('status') == 'fulfilled' and code in str(args.get('etCodes','')).split('|')
                and args.get('regionCode')=='HYD' and args.get('dateCode')==day):
            valid.append(q['data'])
    if not valid:
        raise SourceError('Unverified Hyderabad showtime data')
    return valid


def cinema_links(data):
    result = {}
    for n in nodes(data):
        for value in n.values():
            if not isinstance(value, str):
                continue
            u = urljoin(BASE, value)
            m = re.fullmatch(r'https://in\.bookmyshow\.com/cinemas/hyderabad/[^/]+/buytickets/([A-Z0-9]+)(?:/\d{8})?(?:\?.*)?', u)
            if m:
                base = u.split('?')[0]
                if re.search(r'/\d{8}$', base):
                    base = base.rsplit('/', 1)[0]
                result[m[1]] = {'code': m[1], 'name': n.get('text') or n.get('title') or n.get('name') or m[1], 'url': base}
    return list(result.values())


def offered_dates(data):
    result = set()
    for n in nodes(data):
        if n.get('isDisabled') is True:
            continue
        # Accept only source-provided dates. Never invent a date horizon.
        for k in ('ShowDate', 'showDate', 'date', 'Date', 'DateCode', 'dateCode'):
            value = str(n.get(k, ''))
            if re.fullmatch(r'\d{8}', value) and value >= today():
                try:
                    day_end(value)
                    result.add(value)
                except ValueError:
                    pass
        for k in ('url', 'redirectionUrl'):
            value = n.get(k, '')
            m = re.search(r'/buytickets/[^/]+/(\d{8})(?:\?|$)', value) if isinstance(value, str) else None
            if m and m[1] >= today():
                result.add(m[1])
    return sorted(result)


def parse_venue(data, code, day, url, now=None):
    now = now or dt.datetime.now(IST).timestamp()
    detail = data.get('showDetailsTransformed', {})
    if str(detail.get('Date')) != day:
        raise SourceError('Cinema date mismatch')
    venues = detail.get('Venues', [])
    if isinstance(venues, dict):
        venues = [venues]
    matching = [v for v in venues if v.get('VenueCode') == code]
    if not matching:
        raise SourceError('Cinema identity mismatch')
    output = []
    for venue in matching:
        for event in detail.get('Event', []):
            for child in event.get('ChildEvents', []):
                for show in child.get('ShowTimes', []):
                    raw = show.get('CutOffDateTime', '')
                    try:
                        parsed = (dt.datetime.strptime(raw, '%Y%m%d%H%M') if re.fullmatch(r'\d{12}', raw)
                                  else dt.datetime.fromisoformat(raw))
                        cutoff = parsed.replace(tzinfo=parsed.tzinfo or IST).timestamp()
                    except (ValueError, TypeError):
                        raise SourceError('Unknown show cutoff') from None
                    if cutoff <= now:
                        continue
                    session = str(show.get('SessionId') or show.get('SessionID') or '')
                    event_code = child.get('EventCode', '')
                    if not re.fullmatch(r'[A-Za-z0-9]+', session) or not re.fullmatch(r'ET\d+', event_code):
                        raise SourceError('Unknown session identity')
                    status = str(show.get('AvailStatus'))
                    if status not in ('0','1','2','3','4'):
                        raise SourceError('Unknown availability status')
                    output.append({'id': f'{code}:{day}:{session}:{event_code}',
                        'venue_code':code, 'venue':venue.get('VenueName',code),
                        'date':day, 'session':session, 'event':event_code,
                        'movie':event.get('EventTitle',''), 'language':child.get('EventLanguage',''),
                        'format':child.get('EventDimension',''),
                        'time':show.get('ShowTime') or show.get('ShowTimeDisplay') or show.get('Time',''),
                        'available':status in ('1','2','3'), 'cutoff':cutoff,
                        'url':url, 'seat_url':f'{BASE}/movies/hyd/seat-layout/{event_code}/{code}/{session}/{day}'})
    return output


def format_shows(shows):
    first = shows[0]
    lines = [f'📍 {first["venue"]}', f'🆕 {len(shows)} New Show(s) Added!', '']
    groups = {}
    for show in shows:
        key = (show['movie'], show['language'], show['format'], show['date'])
        groups.setdefault(key, []).append(str(show['time']))
    for (movie, language, fmt, day), times in groups.items():
        lines.extend([f'🎬 {movie} ({language}, {fmt})', f'🗓 {dt.datetime.strptime(day,"%Y%m%d"):%d %b} · '+', '.join(times), ''])
    return '\n'.join(lines) + first['url']


def format_seats(show, seats, added):
    rows = {}
    for key in added:
        item = seats[key]
        rows.setdefault(item['row'], []).append(item['seat'])
    details = '\n'.join(f'{row}: '+', '.join(nums) for row,nums in rows.items())[:2000]
    day = dt.datetime.strptime(show['date'], '%Y%m%d').strftime('%d %b')
    return (f'🎟 Seats now available!\n\n🎬 {show["movie"]} ({show["language"]}, {show["format"]})'
            f'\n📍 {show["venue"]}\n🗓 {day} · {show["time"]}\n\n{details}\n\n{show["seat_url"]}')
