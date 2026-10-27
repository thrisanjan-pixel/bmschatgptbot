"""Read a public BookMyShow showtime's live seat layout without a browser."""
import base64
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from api_source import seat_layout_url

ENDPOINT='https://services-in.bookmyshow.com/doTrans.aspx'
# This key is published in BookMyShow's seat-layout client bundle. If it changes,
# decryption fails closed and the previous good observation remains intact.
LAYOUT_KEY=b'kYp3s6v9y$B&E)H+MbQeThWmZq4t7w!z'
MAX_LAYOUT_BYTES=2_000_000
_area_cache={}


def _areas(show,timeout):
    key=(show['event'],show['venue_code'],show['date'],show['session'])
    cached=_area_cache.get(key)
    if cached and time.time()-cached[0]<600:return cached[1]
    query=urllib.parse.urlencode({'eventCode':show['event'],'dateCode':show['date'],
        'regionCode':'HYD','venueCode':show['venue_code']})
    url='https://in.bookmyshow.com/api/movies-data/seatlayout/v1/primary?'+query
    request=urllib.request.Request(url,headers={'User-Agent':'BMSLocalNotifier/0.1','Accept':'application/json',
        'x-app-code':'WEB','x-region-code':'HYD','x-region-slug':'hyderabad',
        'x-platform-code':'DESKTOP-WEB','x-location-selection':'manual'})
    try:
        with urllib.request.urlopen(request,timeout=timeout) as response:data=json.load(response)['data']
    except urllib.error.HTTPError as error:raise ValueError(f'Seat metadata HTTP {error.code}') from error
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
    _area_cache[key]=(time.time(),areas)
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
    for item in sections[0].split('|'):
        if not item:continue
        parts=item.split(':')
        if len(parts)<3 or not parts[1] or not parts[2].isdigit():raise ValueError('Seat area format changed')
        name=category_areas.get(parts[2])
        if name:areas[parts[1]]=name
    if not areas:raise ValueError('No matching seat categories in layout')
    rows={};available=set();seats=[]
    for item in sections[1].split('|'):
        if not item:continue
        parts=item.split(':')
        if len(parts)<3 or not re.fullmatch(r'[A-Za-z0-9]{1,32}',parts[1]):
            raise ValueError('Seat row format changed')
        label=parts[1]
        for cell in parts[2:]:
            encoded_seat,*extra=cell.split('+')
            if len(encoded_seat)<3 or not encoded_seat[1].isdigit():raise ValueError('Seat status format changed')
            area=encoded_seat[0]
            if area not in areas:continue
            status=int(encoded_seat[1])
            if status not in (0,1,2,3,4,7,8,9):raise ValueError('Unknown seat status')
            if status==0:continue
            category=areas[area]
            row_id=category+' / '+label
            rows[row_id]={'id':row_id,'category':category,'row':label}
            number=extra[0] if extra and extra[0] not in ('0','00') else encoded_seat[2:]
            if not re.fullmatch(r'[A-Za-z0-9._ -]{1,20}',number):raise ValueError('Seat number format changed')
            seats.append({'row_id':row_id,'seat':number,'available':status in (1,4)})
            if status in (1,4):
                available.add((row_id,number))
    if not rows:raise ValueError('No readable seat rows')
    return {'rows':list(rows.values()),
            'available':[{'row_id':row_id,'seat':number} for row_id,number in sorted(available)],
            'seats':seats,
            'url':seat_layout_url(show)}


def read(show,timeout=20):
    url=seat_layout_url(show)
    category_areas=_areas(show,timeout)
    boundary='----Screenwatch'+secrets.token_hex(12)
    fields={'strAppCode':'WEB','lngTransactionIdentifier':'0','strCommand':'GETSEATLAYOUT',
        'strVenueCode':show['venue_code'],'strParam1':show['session'],'strParam2':'WEB',
        'strParam3':'','strParam4':'','strParam5':'Y','strParam6':'Y','strParam7':'N',
        'strParam8':'','strParam9':'','strParam10':'','strFormat':'json'}
    body=b''.join((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode()
        for key,value in fields.items())+f'--{boundary}--\r\n'.encode()
    headers={'Content-Type':'multipart/form-data; boundary='+boundary,'Accept':'application/json',
        'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/145.0.0.0 Safari/537.36',
        'x-app-code':'WEB','x-region-code':'HYD','x-region-slug':'hyderabad','x-platform-code':'DESKTOP-WEB',
        'x-location-selection':'manual','Referer':url}
    request=urllib.request.Request(ENDPOINT,body,headers)
    try:
        with urllib.request.urlopen(request,timeout=timeout) as response:
            raw=response.read(MAX_LAYOUT_BYTES+1)
            if len(raw)>MAX_LAYOUT_BYTES:raise ValueError('Seat response is too large')
    except urllib.error.HTTPError as error:
        raise ValueError(f'Seat source HTTP {error.code}') from error
    except (urllib.error.URLError,TimeoutError) as error:
        raise ValueError('Seat source could not be reached') from error
    try:
        result=json.loads(raw)['BookMyShow']
        if result.get('blnSuccess')!='true':raise ValueError('Seat source refused layout')
        return parse_layout(show,result['strData'],category_areas)
    except (KeyError,TypeError,json.JSONDecodeError) as error:
        raise ValueError('Seat source response changed') from error
