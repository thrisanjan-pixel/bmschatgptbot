"""Open on PC helper: opens the BookMyShow link of an alert on this PC the moment its message is queued.
It only asks your Screenwatch dashboard what is new, then opens the link in your default browser like a click.
It never requests BookMyShow itself and never clicks or fills in anything on a page."""
import argparse
import http.cookiejar
import json
import os
import threading
import time
import urllib.error
import urllib.request
import webbrowser
try:import winsound
except ImportError:winsound=None
try:import tkinter
except ImportError:tkinter=None

DEFAULT_URL='https://bmschatgptbot-production.up.railway.app'
FAST,SLOW=3,30  # seconds between polls while an Open on PC alert is on / when none is
TEST_LINK='https://in.bookmyshow.com/explore/home/hyderabad'
POPUP_SECONDS=120
ALARM_SECONDS=8

def log(*parts):print(time.strftime('%H:%M:%S'),*parts,flush=True)

def alarm(stop):
    """Loud repeating beeps for about 8 seconds; the pop-up's Close button stops it early."""
    end=time.time()+ALARM_SECONDS
    while time.time()<end and not stop.is_set():
        for freq in (1400,1000,1400,1000):
            if stop.is_set():break
            if winsound:winsound.Beep(freq,180)
            else:time.sleep(.18)
        if winsound and not stop.is_set():winsound.MessageBeep(winsound.MB_ICONHAND);stop.wait(.25)

def popup(summary,url,stop):
    """Topmost window with the alert text; closes itself after 2 minutes."""
    if not tkinter:log('tkinter missing: no pop-up');return
    root=tkinter.Tk();root.title('Screenwatch · Open on PC');root.attributes('-topmost',True);root.configure(padx=22,pady=18)
    tkinter.Label(root,text='Seats alert',font=('Segoe UI',16,'bold')).pack(anchor='w')
    tkinter.Label(root,text=summary or 'New alert',font=('Segoe UI',12),wraplength=420,justify='left').pack(anchor='w',pady=(8,14))
    bar=tkinter.Frame(root);bar.pack(anchor='e')
    def close():stop.set();root.destroy()
    tkinter.Button(bar,text='Open again',font=('Segoe UI',11),width=12,command=lambda:webbrowser.open(url)).pack(side='left',padx=6)
    tkinter.Button(bar,text='Close',font=('Segoe UI',11),width=10,command=close).pack(side='left')
    root.protocol('WM_DELETE_WINDOW',close);root.after(POPUP_SECONDS*1000,close)
    try:root.focus_force()
    except tkinter.TclError:pass
    root.mainloop();stop.set()

def announce(summary,url):
    """Open the link, start the alarm and show the pop-up, all without blocking the polling loop."""
    webbrowser.open(url)
    stop=threading.Event()
    threading.Thread(target=alarm,args=(stop,),daemon=True).start()
    threading.Thread(target=popup,args=(summary,url,stop),daemon=True).start()

class Dashboard:
    def __init__(self,base):
        self.base=base.rstrip('/');self.csrf=''
        self.http=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def get(self,path):
        with self.http.open(self.base+path,timeout=20) as r:return json.loads(r.read())
    def alerts(self,since):return self.get('/api/pc-alerts?client=helper&since='+repr(float(since)))
    def opened(self,alert):
        """Tell the dashboard to turn Open on PC off for this alert (the token comes from the same page the browser uses)."""
        for attempt in (1,2):
            if not self.csrf:self.csrf=self.get('/api/watch?new=1')['csrf']
            request=urllib.request.Request(self.base+'/api/watch',data=json.dumps({'action':'pc_opened','alert':alert}).encode(),
                headers={'Content-Type':'application/json','X-CSRF-Token':self.csrf})
            try:
                with self.http.open(request,timeout=20) as r:return bool(json.loads(r.read()).get('ok'))
            except urllib.error.HTTPError as error:
                error.close()
                if error.code==403 and attempt==1:self.csrf='';continue  # the dashboard restarted and issued a new token
                raise

def new_events(data,seen,handled=None):
    """Events to open now. While the Chrome extension is connected it opens the page itself, so the helper skips them (one log line)."""
    if data.get('extension'):
        if data['events']:log('the Chrome extension is connected; leaving',len(data['events']),'event(s) to it')
        return []
    result=[];handled=set() if handled is None else handled
    for event in data['events']:
        if event['id'] in seen:continue
        seen.add(event['id'])
        if event['alert'] in handled:continue  # one opening per alert; it turns itself off
        if not str(event.get('url','')).startswith('https://in.bookmyshow.com/'):continue  # open BookMyShow links only, whatever the server sends
        handled.add(event['alert']);result.append(event)
    return result

def run(base):
    dash=Dashboard(base);since=None;seen=set();to_close=set();wait=0
    log('Open on PC helper watching',dash.base)
    while True:
        try:
            for alert in list(to_close):
                if dash.opened(alert):to_close.discard(alert);log('turned off Open on PC for alert',alert)
            # The first call only learns the server clock, so old messages are never replayed.
            data=dash.alerts(since if since is not None else 9e12)
            for event in new_events(data,seen):
                log('opening',event['url'],'-',event['summary'])
                announce(event['summary'],event['url']);to_close.add(event['alert'])
            for alert in list(to_close):
                if dash.opened(alert):to_close.discard(alert);log('turned off Open on PC for alert',alert)
            since=max(since or 0,data['now']-5)
            wait=FAST if data['armed'] or to_close else SLOW
        except (urllib.error.URLError,OSError,ValueError,KeyError) as error:
            wait=SLOW;log('cannot reach the dashboard, retrying in',SLOW,'s:',error)
        time.sleep(wait)

def main():
    p=argparse.ArgumentParser(description='Open BookMyShow alert links on this PC.')
    p.add_argument('--url',default=os.environ.get('SCREENWATCH_URL',DEFAULT_URL),help='Screenwatch dashboard address')
    p.add_argument('--test',action='store_true',help='show the pop-up and sound and open the Hyderabad page once, without contacting the server')
    a=p.parse_args()
    if a.test:
        log('test: opening',TEST_LINK);webbrowser.open(TEST_LINK)
        stop=threading.Event();threading.Thread(target=alarm,args=(stop,),daemon=True).start()
        popup('Test alert · this is how an Open on PC alert looks',TEST_LINK,stop);return
    run(a.url)

if __name__=='__main__':main()
