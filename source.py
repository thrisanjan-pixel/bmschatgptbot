"""Read normal public pages. No reservation, challenge bypass or TLS overrides."""
import json
import os
import re
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

BASE = 'https://in.bookmyshow.com'
CATALOG = BASE + '/explore/movies-hyderabad'


class SourceError(RuntimeError):
    def __init__(self, message, status=0, retry=300):
        super().__init__(message)
        self.status, self.retry = status, retry


def state(document):
    match = re.search(r'window\.__INITIAL_STATE__\s*=\s*', document)
    if not match:
        raise SourceError('Missing public page data')
    return json.JSONDecoder().raw_decode(document[match.end():])[0]


def nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


def native(url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.netloc != 'in.bookmyshow.com':
        raise SourceError('Invalid source URL')
    return url


class Browser:
    def __init__(self):
        self.runtime = sync_playwright().start()
        proxy = os.environ.get('HTTPS_PROXY')
        self.browser = self.runtime.chromium.launch(
            executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'),
            headless=os.environ.get('BMS_HEADLESS','1')!='0', args=['--no-sandbox'],
            proxy={'server': proxy} if proxy else None)
        self.page = self.browser.new_page(viewport={'width': 1280, 'height': 1000})
        self.last = 0

    def read(self, url, catalog=False):
        native(url)
        time.sleep(max(0, 1 - (time.monotonic() - self.last)))
        self.last = time.monotonic()
        response = self.page.goto(url, wait_until='domcontentloaded', timeout=45000)
        if not response or response.status != 200:
            status = response.status if response else 0
            retry = 300
            if response:
                try:
                    retry = max(300, int(response.headers.get('retry-after', '300')))
                except ValueError:
                    try:
                        retry = max(300, parsedate_to_datetime(response.headers['retry-after']).timestamp()-time.time())
                    except (ValueError, TypeError, KeyError):
                        pass
            raise SourceError(f'Source HTTP {status}', status, retry)
        self.page.wait_for_timeout(1500)
        if catalog:
            previous, stable = -1, 0
            for _ in range(100):
                count = self.page.locator('main a[href*="/movies/"]').count()
                stable = stable + 1 if count == previous else 0
                if stable >= 3:
                    break
                previous = count
                self.page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                self.page.wait_for_timeout(1000)
        document = self.page.content()
        state(document)  # Challenge/error pages never become successful observations.
        return document

    def close(self):
        self.browser.close()
        self.runtime.stop()
