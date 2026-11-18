"""Regression coverage for request pacing, stale responses, and prompt delivery."""
from unittest.mock import patch
import json
import sqlite3

import pytest

import api_source
import api_watch
import http_source
import telegram
import watch_preferences as prefs
from store import connect, emit, meta
from tests.test_api_watch import DAY, EVENT, response


@pytest.fixture
def db(tmp_path):
    connection=connect(str(tmp_path/'audit.sqlite3'))
    yield connection
    connection.close()


def test_every_listing_lane_obeys_thirty_seconds_and_hourly_cap(db):
    url=api_source.show_url(EVENT,DAY)
    for i in range(120):
        http_source.reserve(db,url,1000+i*30)
    with pytest.raises(http_source.SourceWait) as wait:
        http_source.reserve(db,url,4599,manual=True)
    assert wait.value.until==4600
    http_source.reserve(db,url,4600,metadata=True)
    with pytest.raises(http_source.SourceWait) as wait:
        http_source.reserve(db,url,4629)
    assert wait.value.until==4630


def test_403_retry_after_persists_across_connections(db):
    key=http_source.reserve(db,api_source.show_url(EVENT,DAY),1000)
    with patch('http_source.time.time',return_value=1010):
        http_source.finish(db,key,403,{'Retry-After':'600'})
    assert db.execute('SELECT not_before FROM source_hosts').fetchone()[0]==1610
    with pytest.raises(http_source.SourceWait):
        http_source.reserve(db,api_source.show_url(EVENT,DAY),1609)


def test_slow_telegram_failure_waits_from_response_time(db):
    emit(db,'slow','message')
    db.commit()
    now=[1000]
    def refuse(*args):
        now[0]=1040
        raise telegram.DeliveryError(429,60)
    with patch('telegram.time.time',side_effect=lambda:now[0]),patch('telegram.send',side_effect=refuse):
        telegram.drain(db)
    assert float(meta(db,'telegram_blocked_until'))==1100
    assert db.execute('SELECT next_try FROM outbox').fetchone()[0]==1100


def test_flush_sends_all_due_messages_and_completes_once_alert(db):
    alert=prefs.save(db,{**prefs.defaults(),'movies':['Movie'],'enabled':True,'once':True})
    for i in range(3):emit(db,str(i),f'message {i}',alert_id=alert)
    db.commit()
    with patch('telegram.send') as send:
        telegram.flush(db)
    assert send.call_count==3
    assert not prefs.read(db,alert)['enabled']
    assert db.execute('SELECT count(*) FROM outbox WHERE delivered=0').fetchone()[0]==0


def test_wrong_date_is_discarded_before_snapshot_and_outbox(db):
    prefs.save(db,{**prefs.defaults(),'movies':['Movie'],'enabled':True})
    api_watch.schedule(db,EVENT,DAY,'Movie')
    db.commit()
    job=db.execute('SELECT * FROM jobs').fetchone()
    invalid=response()
    invalid['data']['additionalData']['dateCode']='20000101'
    monitor=api_watch.Monitor('unused')
    with patch('api_watch.get_json',return_value=invalid):
        monitor.check(db)
    assert db.execute('SELECT count(*) FROM snapshots').fetchone()[0]==0
    assert db.execute('SELECT count(*) FROM outbox').fetchone()[0]==0
    assert db.execute('SELECT checked FROM jobs').fetchone()[0]==0
    assert 'mismatch' in db.execute('SELECT error FROM jobs').fetchone()[0]


def test_bad_json_shape_is_controlled_failure():
    for value in ([],None,{'data':None},{'data':{'additionalData':[]}}):
        with pytest.raises(api_source.ReadError):api_source.parse(value,EVENT,DAY)


def test_request_budget_wait_uses_full_deadline_without_one_second_polling(db):
    from store import setmeta
    setmeta(db,'listing_budget_until',1030)
    db.commit()
    monitor=api_watch.Monitor('unused')
    with patch('api_watch.time.time',return_value=1000),patch.object(monitor.stop,'wait') as wait:
        monitor.check(db)
    wait.assert_called_once_with(30)


def test_slow_response_does_not_add_latency_to_next_due_and_wakes_sender(db):
    prefs.save(db,{**prefs.defaults(),'movies':['Movie'],'enabled':True})
    api_watch.schedule(db,EVENT,DAY,'Movie')
    db.commit()
    now=[1000.0]
    def receive(*args):
        now[0]+=3
        return response()
    monitor=api_watch.Monitor('unused')
    with patch('api_watch.time.time',side_effect=lambda:now[0]),patch('api_watch.get_json',side_effect=receive),patch.object(monitor.stop,'wait') as wait:
        monitor.check(db)
    assert db.execute('SELECT due FROM jobs WHERE checked>0').fetchone()[0]==1030.0
    assert monitor.delivery_ready.is_set()
    wait.assert_not_called()


def test_optional_photo_429_blocks_next_message_without_resending_text(db):
    emit(db,'first','x'*1200,photo=b'png')
    emit(db,'second','next message')
    db.commit()
    def request(method,*args):
        if method=='sendPhoto':raise telegram.DeliveryError(429,90)
        return {}
    with patch('telegram.credential',return_value='test'),patch('telegram.request',side_effect=request) as send:
        telegram.flush(db)
    assert [call.args[0] for call in send.call_args_list]==['sendMessage','sendPhoto']
    assert db.execute('SELECT count(*) FROM outbox WHERE delivered>0').fetchone()[0]==1
    assert float(meta(db,'telegram_blocked_until'))>0


def test_alert_health_ignores_success_on_unselected_date(db):
    prefs.save(db,{**prefs.defaults(),'movies':['Movie'],'dates':[DAY],'enabled':True})
    api_watch.schedule(db,EVENT,DAY,'Movie')
    api_watch.schedule(db,EVENT,'20991231','Movie')
    for row in db.execute('SELECT id,payload FROM jobs').fetchall():
        selected=json.loads(row['payload'])['date']==DAY
        db.execute('UPDATE jobs SET checked=?,error=? WHERE id=?',(0 if selected else 1000,'date mismatch' if selected else '',row['id']))
    rule=prefs.rules(db)[0]
    assert rule['health']['last_success']==0
    assert rule['health']['error']=='date mismatch'


@pytest.mark.parametrize('module,table',[(http_source,'source_requests'),(__import__('row_watch'),'row_layouts')])
def test_simultaneous_schema_migration_tolerates_only_duplicate_columns(db,module,table):
    module.schema(db)
    class ConcurrentMigration:
        def execute(self,sql,*args):
            if sql==f'PRAGMA table_info({table})':return []
            if sql.startswith(f'ALTER TABLE {table}'):
                raise sqlite3.OperationalError('duplicate column name: manual')
            return db.execute(sql,*args)
        def __getattr__(self,name):return getattr(db,name)
    module.schema(ConcurrentMigration())
