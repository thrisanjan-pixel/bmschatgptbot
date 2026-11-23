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


# A transport failure after the request went out: Telegram may have delivered it.
AMBIGUOUS_ERRORS = ('TimeoutError', 'timeout', 'IncompleteRead', 'RemoteDisconnected', 'ConnectionResetError')


def max_attempts():
    """Failed sends (not counting 429 flood waits) before a message is dead-lettered."""
    try:return max(1,int(os.environ.get('OUTBOX_MAX_ATTEMPTS','20')))
    except ValueError:return 20


def ambiguous_policy():
    """'assume_sent' (default): a message whose send timed out after it went out is marked
    delivered, so it can never arrive twice. 'retry': send it again (may duplicate)."""
    return 'retry' if os.environ.get('OUTBOX_AMBIGUOUS_POLICY','assume_sent').strip()=='retry' else 'assume_sent'


class DeliveryError(RuntimeError):
    def __init__(self, code=0, retry=30, reason='', ambiguous=False):
        super().__init__(f'Telegram delivery failed (HTTP {code})')
        self.ambiguous=bool(ambiguous)
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
        # A connect failure arrives as URLError (never sent); a read timeout means it may have arrived.
        name=type(error).__name__
        raise DeliveryError(reason=name, ambiguous=name in AMBIGUOUS_ERRORS) from None


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


def claim(db, row, now):
    """Hide a due row from other processes for 120 s; False if someone else already took it."""
    return bool(db.execute('UPDATE outbox SET next_try=? WHERE id=? AND delivered=0 AND next_try<=?', (now+120, row['id'], now)).rowcount)


def mark_delivered(db, row, now):
    """Mark a claimed row delivered. Returns the alert id it completes, or None."""
    marked = db.execute('UPDATE outbox SET delivered=? WHERE id=? AND delivered=0', (now, row['id'])).rowcount
    delivered_id=(row['alert_id'] if 'alert_id' in row.keys() else '') if marked else None
    # A seat-photo follow-up is not the alert's own message, so it never completes a once alert.
    if delivered_id and str(row['text'] or '').startswith('📸'):delivered_id=None
    return delivered_id


def dead_letter(db, row, error):
    """Move a message that failed max_attempts() times out of the queue and tell the operator."""
    from watch_preferences import archive_message
    from store import meta,setmeta
    archive_message(db, row['id'])
    setmeta(db,'dead_letter_count',int(meta(db,'dead_letter_count','0') or 0)+1)
    setmeta(db,'last_dead_letter_at',time.time())


def drain(db, commit=True, system_only=False):
    from store import meta,setmeta,duplicate_of
    import role
    # Only the active instance sends. A standby keeps detecting and queueing.
    if not role.sender_active(db):return None
    if meta(db,'telegram_enabled','1')!='1':return None
    now = time.time()
    # A 429 is a flood wait for the whole bot, not for one row.
    if float(meta(db,'telegram_blocked_until','0') or 0)>now:return None
    row = db.execute("SELECT * FROM outbox WHERE delivered=0 AND next_try<=?"+(" AND alert_id='__health__'" if system_only else "")+" ORDER BY CASE WHEN alert_id='__health__' THEN 0 ELSE 1 END,created LIMIT 1", (now,)).fetchone()
    if not row:
        return None
    keys=row.keys()
    if 'dedupe' in keys and row['dedupe'] and duplicate_of(db,row['alert_id'],row['dedupe'],row['release_ts'],exclude_id=row['id'],delivered_only=True):
        # The same release already went out (or may have, after an ambiguous timeout): never send it twice.
        if not db.in_transaction:db.execute('BEGIN IMMEDIATE')
        db.execute('UPDATE outbox SET delivered=? WHERE id=? AND delivered=0',(now,row['id']))
        setmeta(db,'dedupe_skipped',int(meta(db,'dedupe_skipped','0') or 0)+1)
        logging.info('Outbox %s skipped: same release already delivered',row['id'][:12])
        if commit:db.commit()
        return None
    text = row['text']
    if now - row['created'] > 600:
        # Queued while alerts were switched off or Telegram was down; say so instead of looking current.
        ist = dt.datetime.fromtimestamp(row['created'], dt.timezone(dt.timedelta(hours=5, minutes=30)))
        text = f"Delayed alert from {ist:%H:%M} IST (may be out of date)\n\n" + text
    # Claim the row before releasing the write lock: api_watch and health_watch both drain and must not both send it.
    if not db.in_transaction:db.execute('BEGIN IMMEDIATE')
    if not claim(db, row, now):
        db.commit();return None
    # Release the write lock while talking to Telegram; a crash before the mark below resends after the claim expires.
    db.commit()
    try:
        send(text, row['photo'])
    except Exception as error:
        if not isinstance(error, DeliveryError):error = DeliveryError(reason=type(error).__name__)
        logging.warning('Telegram delivery failed: HTTP %s, retry in %ss, attempt %s%s', error.code, error.retry,
                        row['attempts']+1, ' ('+error.reason+')' if error.reason else '')
        assume_sent = error.ambiguous and ambiguous_policy()=='assume_sent'
        dead = not assume_sent and error.code!=429 and row['attempts']+1>=max_attempts() and not permanent(error)
        if dead:
            import ntfy
            ntfy.notify(f'Screenwatch dead-lettered a Telegram message after {row["attempts"]+1} failed sends '
                        f'(last: HTTP {error.code} {error.reason}). Check Telegram and the dashboard.', title='Screenwatch delivery failing')
        db.execute('BEGIN IMMEDIATE')
        setmeta(db,'last_failed_send_at',time.time())
        if error.code==429:setmeta(db,'telegram_blocked_until',str(now+error.retry))
        if assume_sent:
            # Telegram may have shown it already; the dedupe key stops any copy of it going out too.
            logging.warning('Outbox %s: send timed out after it went out; counting it as delivered',row['id'][:12])
            setmeta(db,'ambiguous_sends',int(meta(db,'ambiguous_sends','0') or 0)+1)
            delivered_id=mark_delivered(db,row,time.time())
        elif permanent(error) and row['attempts']+1 >= FAILED_ATTEMPTS:
            # Keep the text for inspection, but stop it holding back later alerts for this rule.
            from watch_preferences import archive_message
            archive_message(db, row['id'])
            delivered_id=None
        elif dead:
            dead_letter(db,row,error)
            delivered_id=None
        else:
            db.execute('UPDATE outbox SET attempts=attempts+1,next_try=? WHERE id=?', (now+max(30,error.retry),row['id']))
            delivered_id=None
    else:
        db.execute('BEGIN IMMEDIATE')
        setmeta(db,'last_telegram_success_at',time.time())
        # The alert may have been paused or deleted (message archived) while sending.
        delivered_id=mark_delivered(db,row,time.time())
    if commit:db.commit()
    return delivered_id
