"""Browser-free public reads. Showtime structure informed by aviiciii/bms-ticket-notifier (MIT; see third_party/)."""
import datetime as dt
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

IST=ZoneInfo('Asia/Kolkata')
BASE='https://in.bookmyshow.com'
API=BASE+'/api/movies-data/v4/showtimes-by-event/primary-dynamic'
CATALOG='https://in-cdn.bmscdn.com/discovery-catalog/response-cache/movies-hyd-web.json'

class ReadError(RuntimeError):
    def __init__(self,message,status=0,retry=300):
        super().__init__(message);self.status=status;self.retry=retry

def today():return dt.datetime.now(IST).strftime('%Y%m%d')
def end_day(day):return (dt.datetime.strptime(day,'%Y%m%d').replace(tzinfo=IST)+dt.timedelta(days=1)).timestamp()
def seat_layout_url(show):
    event=str(show.get('event',''));venue=str(show.get('venue_code',''));session=str(show.get('session',''));day=str(show.get('date',''))
    if not re.fullmatch(r'ET[A-Z0-9]{6,}',event) or not re.fullmatch(r'[A-Z0-9]+',venue) or not re.fullmatch(r'[A-Za-z0-9]+',session) or not re.fullmatch(r'\d{8}',day):
        raise ReadError('Invalid showtime link identity')
    return f'{BASE}/movies/hyderabad/seat-layout/{event}/{venue}/{session}/{day}'
def nodes(value):
    if isinstance(value,dict):
        yield value
        for child in value.values():yield from nodes(child)
    elif isinstance(value,list):
        for child in value:yield from nodes(child)

def get_json(url):
    p=urllib.parse.urlparse(url)
    if p.scheme!='https' or p.netloc not in ('in.bookmyshow.com','in-cdn.bmscdn.com'):raise ReadError('Invalid source host')
    req=urllib.request.Request(url,headers={'User-Agent':'BMSLocalNotifier/0.1','Accept':'application/json',
        'x-app-code':'WEB','x-region-code':'HYD','x-region-slug':'hyderabad','x-platform-code':'DESKTOP-WEB','x-location-selection':'manual'})
    try:
        with urllib.request.urlopen(req,timeout=25) as response:return json.load(response)
    except urllib.error.HTTPError as error:
        status=error.code
        retry=error.headers.get('Retry-After','300')
        error.close()
        raise ReadError(f'Source HTTP {status}',status,max(300,int(retry) if retry.isdigit() else 300)) from None
    except (OSError,ValueError,urllib.error.URLError):raise ReadError('Source connection or JSON error') from None

def movies(data):
    if not isinstance(data.get('listings'),list):raise ReadError('Catalogue schema changed')
    result={}
    for item in nodes(data.get('listings',[])):
        url=item.get('ctaUrl','')
        match=re.fullmatch(r'https://in\.bookmyshow\.com/movies/hyderabad/[^/]+/(ET\d+)',str(url))
        if not match:continue
        analytics=item.get('analytics',{})
        if analytics.get('region_code') not in (None,'HYD'):raise ReadError('Catalogue city mismatch')
        title=analytics.get('title') or item.get('image',{}).get('altText') or match[1]
        result[match[1]]={'event':match[1],'title':title,'url':url,'image':item.get('image',{}).get('url','')}
    if not result:raise ReadError('No verified Hyderabad movie listings')
    return list(result.values())

def show_url(event,day=''):
    if not re.fullmatch(r'ET\d+',event) or (day and not re.fullmatch(r'\d{8}',day)):raise ReadError('Invalid screening request')
    return API+'?'+urllib.parse.urlencode({'eventCode':event,'dateCode':day,'isDesktop':'true','regionCode':'HYD',
        'xLocationShared':'false','memberId':'','lsId':'','subCode':'','lat':'17.385','lon':'78.487'})

def no_regional_shows(data):
    body = data.get('data')
    metadata = data.get('metadata')
    if not isinstance(body, dict) or not isinstance(metadata, dict):
        return False
    error = metadata.get('error')
    return (
        isinstance(error, dict)
        and error.get('errorCode') == '1a.1b.2001'
        and body.get('additionalData') == {}
        and not body.get('showtimeWidgets')
    )


def parse(data,event,requested_day='',now=None):
    now=dt.datetime.now(IST).timestamp() if now is None else now
    body=data.get('data',{});identity=body.get('additionalData',{})
    day=str(identity.get('dateCode',''))
    if identity.get('eventCode')!=event or not re.fullmatch(r'\d{8}',day) or (requested_day and requested_day!=day):
        raise ReadError('Screening event/date mismatch')
    if not isinstance(body.get('showtimeWidgets'),list):raise ReadError('Showtime schema changed')
    dates=set();variants=set();language='';film=''
    for widget in body.get('topStickyWidgets',[]):
        if widget.get('type')=='horizontal-block-list':
            for item in widget.get('data',[]):
                value=str(item.get('id',''))
                if re.fullmatch(r'\d{8}',value) and value>=today() and item.get('styleId') in ('date-selected','date-default'):
                    end_day(value);dates.add(value)
        if widget.get('type')=='horizontal-text-list':
            for n in nodes(widget.get('data',[])):
                text=n.get('text','')
                if isinstance(text,str) and '•' in text:language=text
    for n in nodes(body.get('bottomSheetData',{}).get('format-selector',{})):
        if n.get('styleId')=='bottomsheet-subtitle':film=n.get('text','')
        for key in ('eventCode','eventcode'):
            value=n.get(key)
            if re.fullmatch(r'ET\d+',str(value)):variants.add(value)
        url=n.get('redirectionUrl') or n.get('url')
        if isinstance(url,str) and '/hyderabad/' in url:
            variants.update(re.findall(r'ET\d+',url))
    shows={}
    for widget in body['showtimeWidgets']:
        if widget.get('type')!='groupList':continue
        for group in widget.get('data',[]):
            if group.get('type')!='venueGroup':continue
            for card in group.get('data',[]):
                if card.get('type')!='venue-card':continue
                venue=card.get('additionalData',{});code=venue.get('venueCode','')
                if not re.fullmatch(r'[A-Z0-9]+',code):raise ReadError('Unknown cinema identity')
                booking=next((n.get('redirectionUrl') or n.get('href') for n in nodes(card.get('header',{}))
                    if isinstance(n.get('redirectionUrl') or n.get('href'),str) and '/cinemas/hyderabad/' in (n.get('redirectionUrl') or n.get('href'))),None)
                if not booking or not re.fullmatch(r'https://in\.bookmyshow\.com/cinemas/hyderabad/[^/]+/buytickets/'+re.escape(code)+'/'+day+r'(?:\?.*)?',booking):
                    raise ReadError('Unverified Hyderabad cinema link')
                for raw in card.get('showtimes',[]):
                    item=raw.get('additionalData',{});session=str(item.get('sessionId',''))
                    date=str(item.get('showDateCode') or item.get('dateCode') or '')
                    if date!=day or not re.fullmatch(r'[A-Za-z0-9]+',session):raise ReadError('Show identity mismatch')
                    try:cutoff=dt.datetime.strptime(item['cutOffDateTime'],'%Y%m%d%H%M').replace(tzinfo=IST).timestamp()
                    except (KeyError,ValueError,TypeError):raise ReadError('Unknown show cutoff') from None
                    start_raw=str(item.get('showDateTime') or (day+str(item.get('showTimeCode','')).zfill(4)))
                    try:start=dt.datetime.strptime(start_raw,'%Y%m%d%H%M').replace(tzinfo=IST).timestamp()
                    except ValueError:raise ReadError('Unknown show start time') from None
                    cutoff=min(cutoff,start)
                    if cutoff<=now:continue
                    if not isinstance(item.get('categories'),list) or not item['categories']:
                        raise ReadError('Missing category availability')
                    categories={}
                    for c in item.get('categories',[]):
                        status=str(c.get('availStatus'))
                        if status not in ('0','1','2','3','4'):raise ReadError('Unknown category availability')
                        key=str(c.get('priceCode') or c.get('priceDesc') or '')
                        if not key:raise ReadError('Unknown seat category')
                        categories[key]={'name':c.get('priceDesc',key),'status':status,'price':str(c.get('curPrice',''))}
                    key=f'{code}:{day}:{session}:{event}'
                    shows[key]={'id':key,'event':event,'session':session,'venue_code':code,'venue':venue.get('venueName',code),
                        'date':day,'movie':film,'language':language,'format':raw.get('screenAttr') or item.get('attributes',''),
                        'time':raw.get('title') or item.get('showTime',''),'cutoff':cutoff,'categories':categories,
                        'url':booking or f'{BASE}/movies/hyderabad/movie/buytickets/{event}/{day}'}
    return {'day':day,'dates':sorted(dates),'variants':sorted(variants),'shows':shows,'film':film}
