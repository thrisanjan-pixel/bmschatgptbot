import io
import json
import logging
import subprocess
import sys
import urllib.error
from unittest.mock import patch
import pytest
import api_source,api_watch,http_source
import watch_preferences as prefs
from store import connect,meta,setmeta
from tests.test_api_watch import DAY,EVENT,response

@pytest.fixture
def db(tmp_path):
    state=connect(str(tmp_path/'state.sqlite3'))
    prefs.save(state,{**prefs.defaults(),'movies':['Movie'],'enabled':True})
    api_watch.schedule(state,EVENT,DAY,'Movie');state.commit()
    yield state
    state.close()

# Frozen from deployment be49a1ba, not generated from the implementation.
LEGACY_URL='https://in.bookmyshow.com/api/movies-data/v4/showtimes-by-event/primary-dynamic?eventCode=ET00439706&dateCode=20261217&isDesktop=true&regionCode=HYD&xLocationShared=false&memberId=&lsId=&subCode=&lat=17.385&lon=78.487'
LEGACY_HEADERS=[('User-agent','BMSLocalNotifier/0.1'),('Accept','application/json'),('X-app-code','WEB'),('X-region-code','HYD'),('X-region-slug','hyderabad'),('X-platform-code','DESKTOP-WEB'),('X-location-selection','manual')]

@pytest.mark.parametrize('budget',[False,True])
def test_outgoing_request_pins_legacy_client_url_params_headers_order_and_timeout(db,monkeypatch,budget):
    monkeypatch.setenv('BMS_REQUEST_BUDGET','1' if budget else '')
    monkeypatch.setenv('BMS_DB',db.execute('PRAGMA database_list').fetchone()[2])
    class Response(io.BytesIO):
        status=200
        headers={}
    with patch('http_source.urllib.request.urlopen',return_value=Response(b'{}')) as native:
        api_source.get_json(api_source.show_url('ET00439706','20261217'))
    native.assert_called_once()
    request=native.call_args.args[0]
    assert request.full_url==LEGACY_URL
    assert request.header_items()==LEGACY_HEADERS
    assert request.get_method()=='GET' and request.data is None
    assert native.call_args.kwargs=={'timeout':25}
    assert not request.has_header('Cookie') and not request.has_header('Connection')


def test_403_ends_cycle_without_extra_request_or_session_retry(db,caplog):
    monitor=api_watch.Monitor('unused')
    denied=api_source.ReadError('Source HTTP 403',403,180)
    with patch('api_watch.time.time',return_value=1000),patch('api_watch.get_json',side_effect=denied) as get:
        with caplog.at_level(logging.INFO):monitor.check(db)
    get.assert_called_once()
    job=db.execute('SELECT * FROM jobs').fetchone()
    assert job['due']==1180
    assert meta(db,'listing_403_retry:'+job['id'])==''
    assert meta(db,'listing_blocked_count')=='1'
    assert db.execute('SELECT count(*) FROM outbox').fetchone()[0]==0
    assert 'BMS blocked one check count=1' in caplog.text
    assert all(r.levelno<logging.ERROR for r in caplog.records)


def test_repeated_403_backs_off_and_stale_retry_flags_do_not_change_priority(db):
    job=db.execute('SELECT * FROM jobs').fetchone()
    setmeta(db,'listing_403_retry:'+job['id'],'1')
    error=api_source.ReadError('Source HTTP 403',403,180)
    api_watch.record_listing_failure(db,job,error,1000,1000)
    failed=db.execute('SELECT * FROM jobs').fetchone()
    api_watch.record_listing_failure(db,failed,error,1180,1180)
    assert db.execute('SELECT due FROM jobs').fetchone()[0]==1480
    assert not meta(db,'listing_403_retry:'+job['id'])

def test_wrong_date_is_info_only_and_never_queued(db,caplog):
    bad=response();bad['data']['additionalData']['dateCode']='20200101'
    with patch('api_watch.get_json',return_value=bad),caplog.at_level(logging.INFO):
        api_watch.Monitor('unused').check(db)
    discarded=[item for item in caplog.records if 'Discarded wrong-date/event' in item.getMessage()]
    assert len(discarded)==1 and discarded[0].levelno==logging.INFO
    assert not [item for item in caplog.records if item.levelno>=logging.WARNING]
    assert db.execute('SELECT count(*) FROM outbox').fetchone()[0]==0
    assert db.execute('SELECT count(*) FROM snapshots').fetchone()[0]==0
    assert prefs.rules(db)[0]['health']['label']=='Waiting for requested date'


def test_dashboard_reports_block_count_and_warning_label(db):
    import dashboard
    job=db.execute('SELECT * FROM jobs').fetchone()
    api_watch.record_listing_failure(db,job,api_source.ReadError('Source HTTP 403',403,180),1000,1000)
    db.commit()
    assert prefs.rules(db)[0]['health']['label']=='BMS blocked one check'
    result=dashboard.status(db.execute('PRAGMA database_list').fetchone()[2])
    assert result['reliability']['blocked_checks']=={'count':1,'last_at':1000}


def test_structured_logs_use_stdout_and_preserve_real_severity():
    result=subprocess.run([sys.executable,'-c',
        'import log_config,logging;log_config.configure();logging.info("discarded date");logging.warning("BMS blocked one check count=1")'],
        capture_output=True,text=True,check=True)
    assert not result.stderr
    records=[json.loads(line) for line in result.stdout.splitlines()]
    assert [item['level'] for item in records]==['info','warn']
    failure=subprocess.run([sys.executable,'-c',
        'import log_config,logging;log_config.configure();logging.error("real failure")'],
        capture_output=True,text=True,check=True)
    assert not failure.stdout
    assert json.loads(failure.stderr)['level']=='error'
    from log_config import JsonFormatter
    actual=logging.LogRecord('app',logging.ERROR,'',0,'genuine failure',(),None)
    assert json.loads(JsonFormatter().format(actual))['level']=='error'
