"""Credentials are read at runtime and never included in error messages."""
import datetime as dt
import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid


FAILED_ATTEMPTS = 5


def credential(key):
    """Environment value without surrounding whitespace or a copied U+FEFF byte-order mark."""
    return (os.environ.get(key) or '').strip().strip('﻿').strip()


def configured():
    return bool(credential('TELEGRAM_BOT_TOKEN') and credential('TELEGRAM_CHAT_ID'))


class DeliveryError(RuntimeError):
    def __init__(self, code=0, retry=30, reason=''):
        super().__init__(f'Telegram delivery failed (HTTP {code})')
        try:self.code=int(code)
        except (ValueError,TypeError,OverflowError):self.code=0
        try:self.retry=max(30,min(86400,int(retry)))
        except (ValueError,TypeError,OverflowError):self.retry=30
        # Telegram's own description, never the token, URL or chat id.
        self.reason=' '.join(str(reason or '').split())[:120]


def request(method, data, photo=None):
    token = credential('TELEGRAM_BOT_TOKEN')
    if not token:
        raise DeliveryError(reason='bot token is not configured')
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
        if not isinstance(result,dict):raise DeliveryError(reason='malformed response')
        if not result.get('ok'):
            parameters = result.get('parameters') if isinstance(result.get('parameters'), dict) else {}
            raise DeliveryError(result.get('error_code', 0), parameters.get('retry_after', 30), result.get('description', ''))
        if 'result' not in result:raise DeliveryError(reason='malformed response')
        return result['result']
    except DeliveryError:
        raise
    except urllib.error.HTTPError as error:
        retry = 30;reason = ''
        try:
            detail = json.load(error)
            parameters = detail.get('parameters') if isinstance(detail.get('parameters'), dict) else {}
            retry = parameters.get('retry_after', 30);reason = detail.get('description', '')
        except (ValueError, OSError, AttributeError):
            pass
        finally:
            error.close()
        raise DeliveryError(error.code, retry, reason) from None
    except Exception as error:
        # Includes http.client.IncompleteRead and other non-OSError transport failures.
        raise DeliveryError(reason=type(error).__name__) from None


def send(text, photo=None):
    chat = credential('TELEGRAM_CHAT_ID')
    if not chat:
        raise DeliveryError(reason='chat id is not configured')
    message = {'chat_id': chat, 'text': text[:4000], 'disable_web_page_preview': True}
    if photo is None:
        request('sendMessage', message);return
    if len(text) > 1000:
        # Telegram captions have a shorter limit. The full text (with its booking
        # link) goes first; the map is optional, so its failure is not retried.
        request('sendMessage', message)
        try:request('sendPhoto', {'chat_id': chat, 'caption': 'Current public seat map · Hyderabad'}, photo)
        except DeliveryError as error:logging.warning('Telegram seat map skipped: HTTP %s', error.code)
        return
    try:
        request('sendPhoto', {'chat_id': chat, 'caption': text}, photo)
    except DeliveryError as error:
        # Rate limits and missing setup apply to every method; honour them.
        if error.code == 429 or not configured():raise
        # No HTTP status (timeout) or 5xx: Telegram may have accepted the photo, so retry the photo, never send text too.
        if not 400 <= error.code < 500:raise
        logging.warning('Telegram photo failed (HTTP %s); sending text only', error.code)
        request('sendMessage', message)


def permanent(error):
    """A message Telegram rejects itself, not a setup, rate-limit or network problem."""
    reason = error.reason.casefold()
    return error.code == 400 and 'chat' not in reason and 'token' not in reason


def drain(db, commit=True, system_only=False):
    from store import meta
    if meta(db,'telegram_enabled','1')!='1':return None
    now = time.time()
    row = db.execute("SELECT * FROM outbox WHERE delivered=0 AND next_try<=?"+(" AND alert_id='__health__'" if system_only else "")+" ORDER BY CASE WHEN alert_id='__health__' THEN 0 ELSE 1 END,created LIMIT 1", (now,)).fetchone()
    if not row:
        return None
    text = row['text']
    if now - row['created'] > 600:
        # Queued while alerts were switched off or Telegram was down; say so instead of looking current.
        ist = dt.datetime.fromtimestamp(row['created'], dt.timezone(dt.timedelta(hours=5, minutes=30)))
        text = f"Delayed alert from {ist:%H:%M} IST (may be out of date)\n\n" + text
    # Release the write lock while talking to Telegram; a crash before the mark below resends, as before.
    db.commit()
    try:
        send(text, row['photo'])
    except Exception as error:
        if not isinstance(error, DeliveryError):error = DeliveryError(reason=type(error).__name__)
        logging.warning('Telegram delivery failed: HTTP %s, retry in %ss, attempt %s%s', error.code, error.retry,
                        row['attempts']+1, ' ('+error.reason+')' if error.reason else '')
        db.execute('BEGIN IMMEDIATE')
        if permanent(error) and row['attempts']+1 >= FAILED_ATTEMPTS:
            # Keep the text for inspection, but stop it holding back later alerts for this rule.
            from watch_preferences import archive_message
            archive_message(db, row['id'])
        else:
            db.execute('UPDATE outbox SET attempts=attempts+1,next_try=? WHERE id=?', (now+max(30,error.retry),row['id']))
        delivered_id=None
    else:
        db.execute('BEGIN IMMEDIATE')
        # The alert may have been paused or deleted (message archived) while sending.
        marked = db.execute('UPDATE outbox SET delivered=? WHERE id=? AND delivered=0', (time.time(), row['id'])).rowcount
        delivered_id=(row['alert_id'] if 'alert_id' in row.keys() else '') if marked else None
    if commit:db.commit()
    return delivered_id
