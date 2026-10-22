"""Read seat statuses through public accessibility controls, without selecting seats."""
import re
from source import SourceError, native


def options(locator):
    return locator.locator('option').evaluate_all(
        '(es)=>es.filter(e=>e.value && !e.disabled).map(e=>({value:e.value,label:e.textContent}))')


def parse_cells(rows, category, row):
    available={}
    if not rows:
        raise SourceError('Empty seat status table')
    for cells in rows:
        if len(cells)<2 or not re.fullmatch(r'[A-Za-z0-9-]+',cells[0]):
            raise SourceError('Unknown seat number')
        number,status=cells[:2]
        if status not in ('Available','Available Best Seat','Booked','Booked Best Seat','Unavailable'):
            raise SourceError('Unknown seat status')
        if status.startswith('Available'):
            available[f'{category}/{row}/{number}']={'row':row,'seat':number}
    return available


def read_seats(browser,show):
    url=native(show['seat_url'])
    expected=f'/seat-layout/{show["event"]}/{show["venue_code"]}/{show["session"]}/{show["date"]}'
    if not url.endswith(expected):raise SourceError('Seat identity mismatch')
    page=browser.page
    response=page.goto(url,wait_until='domcontentloaded',timeout=45000)
    if not response or response.status!=200:
        raise SourceError('Seat source unavailable',response.status if response else 0)
    if page.url.split('?')[0]!=url:raise SourceError('Seat page redirected')
    page.get_by_role('button',name='Select Seats',exact=True).click(timeout=12000)
    page.locator('canvas').first.wait_for(state='visible',timeout=12000)
    page.wait_for_timeout(300)
    photo=page.screenshot(type='png')
    page.get_by_role('button',name='Open accessibility seat selection',exact=True).click()
    page.get_by_role('combobox',name='Select number of tickets, required',exact=True).select_option(label='1 Ticket')
    category=page.get_by_role('combobox',name='Select seat category',exact=True)
    row_select=page.get_by_role('combobox',name='Select row',exact=True)
    categories=options(category)
    if not categories:raise SourceError('No readable seat categories')
    available={}
    for cat in categories:
        category.select_option(cat['value'])
        rows=options(row_select)
        if not rows:raise SourceError('No readable seat rows')
        for row in rows:
            row_select.select_option(row['value'])
            grid=page.get_by_role('grid',name='Seats for Row '+row['value'],exact=True)
            grid.wait_for(state='visible',timeout=8000)
            cells=grid.locator('tbody tr').evaluate_all(
                '(es)=>es.map(e=>Array.from(e.querySelectorAll("td")).map(c=>c.textContent.trim()))')
            available.update(parse_cells(cells,cat['value'],row['label']))
    return available,photo
