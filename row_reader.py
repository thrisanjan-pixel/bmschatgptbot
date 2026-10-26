"""Read one public seat map on an isolated virtual display. Never select seats."""
import json,os,re,sys
from playwright.sync_api import sync_playwright

def read(show):
    with sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=False)
        try:
            page=browser.new_page()
            url=f"https://in.bookmyshow.com/movies/hyd/seat-layout/{show['event']}/{show['venue_code']}/{show['session']}/{show['date']}"
            response=page.goto(url,wait_until='domcontentloaded',timeout=45000)
            if not response or response.status!=200:raise ValueError('Seat page unavailable (HTTP '+str(response.status if response else 0)+')')
            if page.url.split('?')[0]!=url:raise ValueError('Seat page redirected')
            page.get_by_role('button',name='Select Seats',exact=True).click(timeout=15000)
            page.get_by_role('button',name='Open accessibility seat selection',exact=True).click()
            page.get_by_role('combobox',name='Select number of tickets, required',exact=True).select_option(label='1 Ticket')
            category=page.get_by_role('combobox',name='Select seat category',exact=True)
            row_select=page.get_by_role('combobox',name='Select row',exact=True)
            cats=category.locator('option').evaluate_all('(es)=>es.filter(e=>e.value&&!e.disabled).map(e=>({value:e.value,label:e.textContent}))')
            if not cats:raise ValueError('No readable categories; preserving the last row observation')
            rows=[];available=[]
            for cat in cats:
                name=re.split(r'\s+-\s+₹',cat['label'])[0].strip()
                if name not in {c['name'] for c in show['categories'].values()}:raise ValueError('Seat category identity mismatch')
                category.select_option(cat['value'])
                choices=row_select.locator('option').evaluate_all('(es)=>es.filter(e=>e.value&&!e.disabled).map(e=>({value:e.value,label:e.textContent}))')
                for row in choices:
                    row_select.select_option(row['value'])
                    grid=page.get_by_role('grid',name='Seats for Row '+row['value'],exact=True)
                    grid.wait_for(state='visible',timeout=8000)
                    cells=grid.locator('tbody tr').evaluate_all('(es)=>es.map(e=>Array.from(e.querySelectorAll("td")).map(c=>c.textContent.trim()))')
                    if not cells:raise ValueError('Empty row data')
                    label=row['label'].removeprefix(name+' Row ').strip()
                    identity=name+' / '+label
                    rows.append({'id':identity,'category':name,'row':label})
                    for cell in cells:
                        if len(cell)<2:raise ValueError('Invalid seat data')
                        if cell[1] not in ('Available','Available Best Seat','Booked','Booked Best Seat','Unavailable','Handicapped'):raise ValueError('Unknown seat status')
                        if cell[1].startswith('Available'):available.append({'row_id':identity,'seat':cell[0]})
            return {'rows':rows,'available':available,'url':url}
        finally:browser.close()
if __name__=='__main__':
    try:print(json.dumps({'data':read(json.load(sys.stdin))}))
    except Exception as e:print(json.dumps({'error':str(e).splitlines()[0][:200]}));sys.exit(1)
