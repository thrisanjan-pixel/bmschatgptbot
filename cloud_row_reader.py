"""Read a public BookMyShow showtime's live seat layout without a browser."""
import base64
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from http_source import SourceWait, open_request

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from api_source import retry_delay, seat_layout_url, refusal_details

ENDPOINT='https://services-in.bookmyshow.com/doTrans.aspx'
# This key is published in BookMyShow's seat-layout client bundle. If it changes,
# decryption fails closed and the previous good observation remains intact.
LAYOUT_KEY=b'kYp3s6v9y$B&E)H+MbQeThWmZq4t7w!z'
MAX_LAYOUT_BYTES=2_000_000
_area_cache={}
AREA_TTL=6*3600  # Seat categories of a showtime rarely change.

class CategoryMappingError(ValueError):
    pass


# BookMyShow answers a seat-layout request for a sold-out show with HTTP 200 and
# {"blnSuccess":"false","intException":"3306",...,"strException":"... sold out ... (#159)"}.
SOLD_OUT_EXCEPTION='3306'


class SoldOut(ValueError):
    """The show has no seats left. This is a normal state, not a source error."""
    sold_out=True
    def __init__(self):
        super().__init__('Sold out')


def refusal_kind(result):
    """'sold_out' for BookMyShow's sold-out reply, otherwise 'refused'."""
    code=str(result.get('intException','')).strip()
    text=str(result.get('strException',''))
    if code==SOLD_OUT_EXCEPTION or '(#159)' in text:return 'sold_out'
    return 'refused'


class SeatHTTPError(ValueError):
    def __init__(self, source, error):
        super().__init__(f'{source} HTTP {error.code}')
        self.diagnostic = refusal_details(error)
        self.status = error.code
        self.retry = retry_delay(error.code, error.headers.get('Retry-After') if error.headers else None)
        error.close()


def _areas(show,timeout,manual=False):
    key=(show['event'],show['venue_code'],show['date'],show['session'])
    cached=_area_cache.get(key)
    if cached and time.time()-cached[0]<AREA_TTL:return cached[1]
    query=urllib.parse.urlencode({'eventCode':show['event'],'dateCode':show['date'],
        'regionCode':'HYD','venueCode':show['venue_code']})
    url='https://in.bookmyshow.com/api/movies-data/seatlayout/v1/primary?'+query
    request=urllib.request.Request(url,headers={'User-Agent':'BMSLocalNotifier/0.1','Accept':'application/json',
        'x-app-code':'WEB','x-region-code':'HYD','x-region-slug':'hyderabad',
        'x-platform-code':'DESKTOP-WEB','x-location-selection':'manual'})
    try:
        try:
            with open_request(request,timeout=timeout,manual=manual,metadata=True) as response:data=json.load(response)['data']
        except SourceWait as wait:
            # Listing checks share this host on the same 60 s beat as seat reads, so the
            # 10 s metadata gap can fall due every time. Wait out a short gap once.
            if wait.until-time.time()>15:raise
            time.sleep(max(0,wait.until-time.time())+0.2)
            with open_request(request,timeout=timeout,manual=manual,metadata=True) as response:data=json.load(response)['data']
    except urllib.error.HTTPError as error:raise SeatHTTPError('Seat metadata', error) from None
    except (urllib.error.URLError,TimeoutError,ValueError,KeyError) as error:
        raise ValueError('Seat metadata unavailable') from error
    if data.get('eventData',{}).get('eventCode')!=show['event'] or data.get('venue',{}).get('venueCode')!=show['venue_code']:
        raise ValueError('Seat metadata identity mismatch')
    matching=[item for item in data.get('showTimes',[]) if str(item.get('sessionId'))==show['session']]
    if len(matching)!=1:raise ValueError('Showtime missing from seat metadata')
    areas={}
    for item in matching[0].get('categories',[]):
        code=str(item.get('priceCode',''))
        area=str(item.get('areaCatCode',''))
        name=show['categories'].get(code,{}).get('name')
        if area and name:areas[area]=name
    if not areas:raise ValueError('No matching seat categories in metadata')
    now=time.time()
    # Drop stale entries so a long-running worker does not keep every past show.
    for old in [k for k,v in _area_cache.items() if now-v[0]>=AREA_TTL]:del _area_cache[old]
    _area_cache[key]=(now,areas)
    return areas


def _decode(data):
    if not isinstance(data,str) or not 0<len(data)<MAX_LAYOUT_BYTES:
        raise ValueError('Empty or oversized seat layout')
    try:
        encrypted=base64.b64decode(data,validate=True)
        decryptor=Cipher(algorithms.AES(LAYOUT_KEY),modes.CBC(bytes(16))).decryptor()
        padded=decryptor.update(encrypted)+decryptor.finalize()
        unpad=padding.PKCS7(128).unpadder()
        return (unpad.update(padded)+unpad.finalize()).decode('utf-8')
    except (ValueError,UnicodeError) as error:
        raise ValueError('Seat layout could not be decoded') from error


def parse_layout(show,encoded,category_areas):
    text=_decode(encoded)
    sections=text.split('||',2)
    if len(sections)<2:raise ValueError('Seat layout format changed')
    areas={}
    names={}
    for category in show.get('categories',{}).values():
        name=category.get('name','')
        names.setdefault(' '.join(name.casefold().split()),set()).add(name)
    for item in sections[0].split('|'):
        if not item:continue
        parts=item.split(':')
        if len(parts)<3 or not parts[1] or not parts[2].isdigit():raise ValueError('Seat area format changed')
        name=category_areas.get(parts[2])
        if not name:
            candidates=names.get(' '.join(parts[0].casefold().split()),set())
            if len(candidates)==1:name=next(iter(candidates))
        if name:areas[parts[1]]=name
    if not areas:raise CategoryMappingError('No matching seat categories in layout')
    rows={};available=set();seats=[]
    area_codes=sorted(areas,key=len,reverse=True)
    def area_for(cell):
        return next((code for code in area_codes if cell.startswith(code) and
            (len(cell)>len(code) or '+' in cell)),None)
    for item in sections[1].split('|'):
        if not item:continue
        parts=item.split(':')
        indexed=bool(re.fullmatch(r'\d+',parts[0].strip()))
        if len(parts)<(3 if indexed else 2):continue
        label=parts[1 if indexed else 0].strip()
        parsed=[]
        for cell in parts[2 if indexed else 1:]:
            encoded_seat,*extra=cell.split('+')
            # Area IDs in the encrypted layout are usually one character, but
            # they are not guaranteed to stay that way. Match the longest
            # known category area before reading the status digit.
            area=area_for(cell)
            # Ignore other categories; their seat encoding is irrelevant here.
            if area is None:continue
            seat_state=encoded_seat[len(area):]
            if not seat_state or not seat_state[0].isdigit():raise ValueError('Seat status format changed')
            status=int(seat_state[0])
            if status not in (0,1,2,3,4,7,8,9):raise ValueError('Unknown seat status')
            number=extra[0] if extra and extra[0] not in ('0','00') else seat_state[1:]
            # Real AMB layouts include blank aisle records such as
            # 0::B000:B0+0. Parse the cells before validating the row label so
            # these empty records cannot invalidate every real seating row.
            if status==0 and (not number or not number.strip('0')):continue
            if not re.fullmatch(r'[A-Za-z0-9._ -]{1,20}',number):raise ValueError('Seat number format changed')
            parsed.append((areas[area],number,status))
        if not parsed:continue
        if not label or not re.fullmatch(r'[\w .()/#-]{1,32}',label,re.UNICODE):
            raise ValueError('Seat row format changed')
        for category,number,status in parsed:
            row_id=category+' / '+label
            rows[row_id]={'id':row_id,'category':category,'row':label}
            seats.append({'row_id':row_id,'seat':number,'available':status in (1,4)})
            if status in (1,4):
                available.add((row_id,number))
    if not rows:raise ValueError('No readable seat rows')
    return {'rows':list(rows.values()),
            'available':[{'row_id':row_id,'seat':number} for row_id,number in sorted(available)],
            'seats':seats,
            'url':seat_layout_url(show)}


def read(show,timeout=20,manual=False):
    url=seat_layout_url(show)
    boundary='----Screenwatch'+secrets.token_hex(12)
    fields={'strAppCode':'WEB','lngTransactionIdentifier':'0','strCommand':'GETSEATLAYOUT',
        'strVenueCode':show['venue_code'],'strParam1':show['session'],'strParam2':'WEB',
        'strParam3':'','strParam4':'','strParam5':'Y','strParam6':'Y','strParam7':'N',
        'strParam8':'','strParam9':'','strParam10':'','strFormat':'json'}
    body=b''.join((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode()
        for key,value in fields.items())+f'--{boundary}--\r\n'.encode()
    headers={'Content-Type':'multipart/form-data; boundary='+boundary,'Accept':'application/json',
        'User-Agent':'BMSLocalNotifier/0.1',
        'x-app-code':'WEB','x-region-code':'HYD','x-region-slug':'hyderabad','x-platform-code':'DESKTOP-WEB',
        'x-location-selection':'manual','Referer':url}
    request=urllib.request.Request(ENDPOINT,body,headers)
    try:
        with open_request(request,timeout=timeout,manual=manual) as response:
            raw=response.read(MAX_LAYOUT_BYTES+1)
            if len(raw)>MAX_LAYOUT_BYTES:raise ValueError('Seat response is too large')
    except urllib.error.HTTPError as error:
        raise SeatHTTPError('Seat source', error) from None
    except (urllib.error.URLError,TimeoutError) as error:
        raise ValueError('Seat source could not be reached') from error
    try:
        result=json.loads(raw)['BookMyShow']
        if result.get('blnSuccess')!='true':
            if refusal_kind(result)=='sold_out':raise SoldOut()
            raise ValueError('Seat source refused layout')
        # The layout includes category names. Prefer a unique name match to
        # the verified showtime categories, avoiding a second endpoint that
        # may be blocked even while the live layout endpoint works.
        try:return parse_layout(show,result['strData'],{})
        except CategoryMappingError:
            return parse_layout(show,result['strData'],_areas(show,timeout,manual))
    except (KeyError,TypeError,AttributeError,json.JSONDecodeError) as error:
        raise ValueError('Seat source response changed') from error
