"""Credentials are read at runtime and never included in error messages."""
import json
import os
import time
import urllib.error
import urllib.request
import uuid


class DeliveryError(RuntimeError):
    def __init__(self, code=0, retry=30):
        super().__init__(f'Telegram delivery failed (HTTP {code})')
        try:self.retry=max(30,min(86400,int(retry)))
        except (ValueError,TypeError,OverflowError):self.retry=30


def request(method, data, photo=None):
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not token:
        raise DeliveryError()
    if photo is not None:
        boundary = uuid.uuid4().hex
        parts = []
        for key, value in data.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="seats.png"\r\nContent-Type: image/png\r\n\r\n'.encode() + photo + b'\r\n')
        body = b''.join(parts) + f'--{boundary}--\r\n'.encode()
        content_type = 'multipart/form-data; boundary=' + boundary
    else:
        body = json.dumps(data).encode()
        content_type = 'application/json'
    try:
        req = urllib.request.Request('https://api.telegram.org/bot'+token+'/'+method,
                                     data=body, headers={'Content-Type': content_type})
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.load(response)
        if not isinstance(result,dict):raise DeliveryError()
        if not result.get('ok'):
            raise DeliveryError(result.get('error_code', 0), result.get('parameters', {}).get('retry_after', 30))
        if 'result' not in result:raise DeliveryError()
        return result['result']
    except urllib.error.HTTPError as error:
        retry = 30
        try:
            retry = json.load(error).get('parameters', {}).get('retry_after', 30)
        except (ValueError, OSError):
            pass
        raise DeliveryError(error.code, retry) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, TypeError):
        raise DeliveryError() from None


def send(text, photo=None):
    chat = os.environ.get('TELEGRAM_CHAT_ID')
    if not chat:
        raise DeliveryError()
    if photo is not None:
        # Telegram captions have a shorter limit than messages.
        if len(text) > 1000:
            request('sendMessage', {'chat_id': chat, 'text': text[:4000], 'disable_web_page_preview': True})
            text = 'Current public seat map · Hyderabad'
        request('sendPhoto', {'chat_id': chat, 'caption': text}, photo)
    else:
        request('sendMessage', {'chat_id': chat, 'text': text[:4000], 'disable_web_page_preview': True})


def drain(db, commit=True, system_only=False):
    from store import meta
    if meta(db,'telegram_enabled','1')!='1':return None
    now = time.time()
    row = db.execute("SELECT * FROM outbox WHERE delivered=0 AND next_try<=?"+(" AND alert_id='__health__'" if system_only else "")+" ORDER BY CASE WHEN alert_id='__health__' THEN 0 ELSE 1 END,created LIMIT 1", (now,)).fetchone()
    if not row:
        return None
    try:
        send(row['text'], row['photo'])
    except DeliveryError as error:
        db.execute('UPDATE outbox SET attempts=attempts+1,next_try=? WHERE id=?', (now+max(30,error.retry),row['id']))
        delivered_id=None
    else:
        db.execute('UPDATE outbox SET delivered=? WHERE id=?', (time.time(), row['id']))
        delivered_id=row['alert_id'] if 'alert_id' in row.keys() else ''
    if commit:db.commit()
    return delivered_id
