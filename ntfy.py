"""Operator meta-alerts through ntfy (NTFY_TOPIC_URL). Never raises; never logs the URL."""
import logging
import os
import urllib.request


def configured():
    return bool((os.environ.get('NTFY_TOPIC_URL') or '').strip())


def notify(message, title='Screenwatch', priority='high', tags='warning'):
    """Send one push. Returns True when ntfy accepted it."""
    url = (os.environ.get('NTFY_TOPIC_URL') or '').strip()
    if not url.startswith('https://'):
        return False
    headers = {'Title': title[:200], 'Priority': priority, 'Tags': tags}
    token = (os.environ.get('NTFY_TOKEN') or '').strip()
    if token:
        headers['Authorization'] = 'Bearer ' + token
    try:
        request = urllib.request.Request(url, data=message[:3000].encode(), headers=headers, method='POST')
        with urllib.request.urlopen(request, timeout=10) as response:
            return 200 <= response.status < 300
    except Exception as error:
        logging.warning('ntfy alert failed: %s', type(error).__name__)
        return False
