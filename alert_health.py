"""Per-alert verified-source health, independent of delivery throttles."""
import hashlib
import json
import time

from store import meta,setmeta,emit

BLIND_SECONDS=300
# Owner's choice in Activity; 0 turns the blocked-check warning off.
BLIND_MINUTES=(0,5,10,15,30,60)


def blind_minutes(db):
    try:minutes=int(meta(db,'blind_warning_minutes','5'))
    except ValueError:minutes=5
    return minutes if minutes in BLIND_MINUTES else 5


def blind_text(minutes):
    return f'BMS checks blocked for {minutes} min, alerts may be delayed'


def state(db,rule,now=None):
    now=time.time() if now is None else now
    signature=hashlib.sha256(json.dumps([rule['preferences'],rule['updated']],sort_keys=True).encode()).hexdigest()
    key='alert_usable:'+rule['id']
    raw=json.loads(meta(db,key,'{}'))
    if raw.get('signature')!=signature:
        raw={'signature':signature,'since':now,'last_usable':0,'notified':False,'episode':0}
    return raw


def view(db,rule,now=None):
    now=time.time() if now is None else now
    raw=state(db,rule,now)
    threshold=blind_minutes(db)*60
    return {'blocked':bool(threshold) and rule['status']=='active' and now-max(raw['since'],raw['last_usable'])>=threshold,
            'last_usable':raw['last_usable'],'since':raw['since'],'threshold_seconds':threshold}


def mark(db,rule,now=None):
    raw=state(db,rule,now)
    raw['last_usable']=time.time() if now is None else now
    if raw['notified']:
        recover(db,rule,raw)
    setmeta(db,'alert_usable:'+rule['id'],json.dumps(raw))


def recover(db,rule,raw):
    emit(db,f"blind-recovered:{rule['id']}:{raw['signature']}:{raw['episode']}",
         '✅ BMS checks recovered; alerts are watching again.\n\n'+', '.join(rule['preferences']['movies']),alert_id='__health__')
    raw['notified']=False


def listing(db,args,result):
    import watch_preferences as prefs
    profiles=list(result['shows'].values())
    if not profiles:
        profiles=[json.loads(r[0]) for r in db.execute("SELECT payload FROM sessions WHERE json_valid(payload) AND json_extract(payload,'$.event')=?",(args['event'],))]
    profiles=[{'movie':'','venue_code':'','date':'','language':str(s.get('language') or ''),
               'format':str(s.get('format') or '')} for s in profiles]
    for rule in prefs.active(db):
        p=rule['preferences']
        if p['row_show'] not in ('',prefs.ANY_SHOW):continue
        if prefs.movie_key(args["movie"]) not in {prefs.movie_key(m) for m in p["movies"]}:continue
        # An undated "no shows in this city" answer covers every date.
        if p['dates'] and result['day'] not in p['dates'] and not result.get('all_dates'):continue
        probe={**prefs.defaults(),'languages':p['languages'],'formats':p['formats']}
        if profiles and not any(prefs.matches(show,probe) for show in profiles):continue
        # A verified empty listing is useful: it says bookings are not present.
        # Cinema/screen availability is the match condition, not source health.
        mark(db,rule)


def rows(db,show):
    import watch_preferences as prefs
    for rule in prefs.active(db):
        if show['id'] in prefs.row_targets(db,rule['preferences']):mark(db,rule)


def check(db,rules,now):
    for rule in rules:
        raw=state(db,rule,now);blind=view(db,rule,now)['blocked']
        title=', '.join(rule['preferences']['movies'])
        if blind and not raw['notified']:
            raw['episode']+=1;raw['notified']=True;raw['warning_at']=now
            emit(db,f"blind:{rule['id']}:{raw['signature']}:{raw['episode']}",
                 '⚠️ '+blind_text(blind_minutes(db))+'\n\n'+title,alert_id='__health__')
        setmeta(db,'alert_usable:'+rule['id'],json.dumps(raw))
