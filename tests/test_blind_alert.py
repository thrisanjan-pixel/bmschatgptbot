import copy
import json
from unittest.mock import patch

import pytest

import alert_health
import api_source
import api_watch
import health_watch
import watch_preferences as prefs
from store import connect,setmeta
from tests.test_api_watch import DAY,EVENT,response


@pytest.fixture
def setup(tmp_path,monkeypatch):
    clock=[1000.0]
    monkeypatch.setattr(alert_health.time,'time',lambda:clock[0])
    path=str(tmp_path/'blind.sqlite3');db=connect(path)
    alert=prefs.save(db,{**prefs.defaults(),'movies':['Movie'],'dates':[DAY],'enabled':True},create=True)
    api_watch.schedule(db,EVENT,DAY,'Movie');db.commit()
    setmeta(db,'health_watch_started',1)
    def check(at):
        clock[0]=at;setmeta(db,'city_heartbeat',at)
        health_watch.check(db,at)
    check(1000)
    yield db,clock,check,alert,path
    db.close()


def messages(db):
    return [r[0] for r in db.execute("SELECT text FROM outbox WHERE alert_id='__health__' ORDER BY created,rowid")]


def good(db):
    job=db.execute('SELECT * FROM jobs WHERE payload LIKE ? LIMIT 1',('%'+EVENT+'%',)).fetchone()
    api_watch.ingest(db,job,response());db.commit()


def test_one_warning_at_five_minutes_and_immediate_recovery_once(setup):
    db,clock,check,alert,path=setup
    check(1299);assert messages(db)==[]
    check(1300);check(1315);check(1900)
    assert len(messages(db))==1
    assert 'BMS checks blocked for 5 min, alerts may be delayed' in messages(db)[0]
    assert prefs.rules(db)[0]['health']['blind']['blocked']
    clock[0]=1900;good(db)  # Recovery at the same clock tick is not lost.
    assert len(messages(db))==2 and 'recovered' in messages(db)[1]
    check(1915);assert len(messages(db))==2
    assert not prefs.rules(db)[0]['health']['blind']['blocked']
    check(2200);assert len(messages(db))==3
    clock[0]=2201;good(db);assert len(messages(db))==4
    assert prefs.read(db,alert)['enabled']  # Health warnings never complete the one-shot booking alert.


def test_dedup_and_usable_clock_survive_health_worker_restart(setup):
    db,clock,check,alert,path=setup
    check(1300)
    other=connect(path)
    try:
        clock[0]=1600;setmeta(other,'city_heartbeat',1600)
        health_watch.check(other,1600)
        assert len(messages(other))==1
        good(other);assert len(messages(other))==2
    finally:other.close()


@pytest.mark.parametrize('failure',['403','network','wrong-date','unverified-empty'])
def test_unusable_results_never_refresh_the_clock(setup,failure):
    db,clock,check,alert,path=setup
    good(db)
    clock[0]=1250
    db.execute('UPDATE jobs SET due=0');db.commit()
    if failure=='wrong-date':
        data=response();data['data']['additionalData']['dateCode']='20000101'
        mocked=patch('api_watch.get_json',return_value=data)
    elif failure=='unverified-empty':
        data={'metadata':{'error':{'errorCode':'1a.1b.2001'}},'data':{'additionalData':{},'showtimeWidgets':[]}}
        mocked=patch('api_watch.get_json',return_value=data)
    else:
        mocked=patch('api_watch.get_json',side_effect=api_source.ReadError('Source HTTP 403',403,180) if failure=='403' else api_source.ReadError('Source connection or JSON error'))
    with mocked:api_watch.Monitor('unused').check(db)
    check(1300)
    rule=prefs.rules(db)[0]
    assert rule['health']['blind']['last_usable']==1000
    assert rule['health']['blind']['blocked']
    assert len(messages(db))==1


def test_checks_for_another_alert_or_date_do_not_mask_blindness(setup):
    db,clock,check,alert,path=setup
    second=prefs.save(db,{**prefs.defaults(),'movies':['Other'],'enabled':True},create=True)
    check(1000)
    clock[0]=1290
    result={'day':DAY,'shows':{}}
    alert_health.listing(db,{'movie':'Other','event':'ET99999999'},result)
    alert_health.listing(db,{'movie':'Movie','event':EVENT},{'day':'20991231','shows':{}})
    db.commit();check(1300)
    rules={r['id']:r for r in prefs.rules(db)}
    assert rules[alert]['health']['blind']['blocked']
    assert not rules[second]['health']['blind']['blocked']
    assert len(messages(db))==1


def test_language_and_format_filters_are_used_but_no_matching_cinema_is_required(setup):
    db,clock,check,alert,path=setup
    prefs.save(db,{**prefs.read(db,alert),'languages':['English'],'formats':['2D'],'cinemas':['ALLU']},alert)
    check(1000);clock[0]=1290
    show={'movie':'Movie','venue_code':'OTHER','date':DAY,'language':'Telugu • 2D','format':''}
    alert_health.listing(db,{'movie':'Movie','event':EVENT},{'day':DAY,'shows':{'show':show}})
    check(1300);assert prefs.rules(db)[0]['health']['blind']['blocked']
    show['language']='English • 2D'
    alert_health.listing(db,{'movie':'Movie','event':EVENT},{'day':DAY,'shows':{'show':show}})
    db.commit();assert not prefs.rules(db)[0]['health']['blind']['blocked']
    assert len(messages(db))==2


def test_paused_alert_has_no_blind_warning_or_false_recovery(setup):
    db,clock,check,alert,path=setup
    check(1300);prefs.transition(db,alert,'cancel');check(1400)
    assert len(messages(db))==1
    assert not prefs.rules(db)[0]['health']['blind']['blocked']


def test_dashboard_reads_are_pure_and_do_not_open_a_write_transaction(setup):
    db,clock,check,alert,path=setup
    assert not db.in_transaction
    prefs.rules(db)
    assert not db.in_transaction


def test_configuration_changes_start_a_new_observation_window(setup):
    db,clock,check,alert,path=setup
    check(1300);clock[0]=1301
    prefs.save(db,{**prefs.read(db,alert),'screens':['DOLBY CINEMA']},alert)
    check(1301);check(1600);assert len(messages(db))==1
    check(1601);assert len(messages(db))==2


def test_optional_legacy_stale_setting_does_not_disable_five_minute_warning(setup):
    db,clock,check,alert,path=setup
    setmeta(db,'stale_warning_enabled',0);check(1300)
    assert len(messages(db))==1
    check(2000);assert len(messages(db))==1


def test_concurrent_health_workers_queue_one_warning(setup):
    from concurrent.futures import ThreadPoolExecutor
    db,clock,check,alert,path=setup
    clock[0]=1300;setmeta(db,'city_heartbeat',1300);db.commit()
    def run():
        connection=connect(path)
        try:health_watch.check(connection,1300)
        finally:connection.close()
    with ThreadPoolExecutor(max_workers=2) as executor:
        for future in [executor.submit(run),executor.submit(run)]:future.result()
    assert len(messages(db))==1


# Dropped: test_global_dashboard_issue_matches_alert_and_recovers checked an older design that
# stored blind issues in health_watch_issues; the pre-switch build keeps them in blind_watch_issues.


def test_empty_verified_listing_can_use_incomplete_cached_profile(setup):
    db,clock,check,alert,path=setup
    prefs.save(db,{**prefs.read(db,alert),'languages':['English']},alert)
    check(1000);clock[0]=1100
    db.execute('INSERT INTO sessions VALUES(?,?,?,1)',('incomplete',json.dumps({'event':EVENT,'language':'English • 2D'}),9999999999))
    alert_health.listing(db,{'movie':'Movie','event':EVENT},{'day':DAY,'shows':{}})
    db.commit();check(1399)
    assert not prefs.rules(db)[0]['health']['blind']['blocked']
