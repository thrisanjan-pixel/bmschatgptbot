"""Versioned, credential-free logical recovery with non-destructive imports."""
import json,time,re,shutil
from pathlib import Path
from urllib.parse import urlsplit
import watch_preferences as prefs
from store import setmeta,meta
TABLES=('alerts','row_alert_state','alert_initial_state','row_layouts','sessions','check_stats','check_intervals')
MAX_BYTES=32*1024*1024

def export(db):
 prefs.ensure(db)
 tables={}
 db.execute('SAVEPOINT export_snapshot')
 try:
  for table in TABLES:
   if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
    tables[table]=[dict(r) for r in db.execute('SELECT * FROM '+table)]
 finally:db.execute('RELEASE export_snapshot')
 return {'format':'screenwatch-backup','version':1,'created':time.time(),'tables':tables}

def decode(raw):
 if len(raw)>MAX_BYTES:raise ValueError('Backup exceeds 32 MB')
 try:data=json.loads(raw)
 except (ValueError,UnicodeError):raise ValueError('Not a valid JSON backup') from None
 if not isinstance(data,dict) or data.get('format')!='screenwatch-backup' or data.get('version')!=1 or not isinstance(data.get('tables'),dict):raise ValueError('Unsupported Screenwatch backup')
 if set(data['tables'])-set(TABLES):raise ValueError('Unexpected backup tables')
 if any(not isinstance(rows,list) or len(rows)>250000 or any(not isinstance(r,dict) for r in rows) for rows in data['tables'].values()):raise ValueError('Invalid backup rows')
 for row in data['tables'].get('alerts',[]):
  if not isinstance(row.get('id'),str) or not 0<len(row['id'])<=150:raise ValueError('Invalid alert identity')
  try:p=json.loads(row['payload'])
  except (KeyError,TypeError,ValueError):raise ValueError('Invalid alert preferences') from None
  prefs.validate({**p,'enabled':False})
 for row in data['tables'].get('sessions',[]):
  try:show=json.loads(row['payload']);url=urlsplit(show['url'])
  except (KeyError,TypeError,ValueError):raise ValueError('Invalid source session') from None
  if url.scheme!='https' or url.hostname not in ('in.bookmyshow.com','www.bookmyshow.com'):raise ValueError('Unexpected source URL')
 return data

def restore(db,raw):
 data=decode(raw)
 import row_watch
 row_watch.schema(db);prefs.ensure(db)
 db.execute('CREATE TABLE IF NOT EXISTS alert_initial_state(alert_id TEXT PRIMARY KEY,signature TEXT NOT NULL)')
 db.commit()
 # Validate the entire document before any writes. No SQL comes from the file.
 for table,rows in data['tables'].items():
  columns={r[1] for r in db.execute('PRAGMA table_info('+table+')')}
  for row in rows:
   if set(row)!=columns:raise ValueError('Backup schema does not match '+table)
 existing={r[0] for r in db.execute('SELECT id FROM alerts')};added=[]
 restore_intervals=not db.execute('SELECT 1 FROM check_intervals LIMIT 1').fetchone()
 try:
  db.execute('BEGIN IMMEDIATE')
  for row in data['tables'].get('alerts',[]):
   if row['id'] in existing:continue
   p=json.loads(row['payload']);p['enabled']=False
   values={**row,'payload':json.dumps(p)}
   db.execute('INSERT INTO alerts('+','.join(values)+') VALUES('+','.join('?' for _ in values)+')',list(values.values()));added.append(row['id'])
  for table in TABLES[1:]:
   for row in data['tables'].get(table,[]):
    if 'alert_id' in row and row['alert_id'] not in added:continue
    # Existing source state always wins. Interval history is restored only into an empty history.
    if table=='check_intervals' and not restore_intervals:continue
    columns=list(row)
    db.execute('INSERT OR IGNORE INTO '+table+'('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',list(row.values()))
  setmeta(db,'alerts_migration_complete','1');setmeta(db,'last_restore',time.time());db.commit()
 except Exception:db.rollback();raise
 return {'restored':len(added),'skipped':len(data['tables'].get('alerts',[]))-len(added),'paused':True}

BACKUP_COUNT=3
BACKUP_BUDGET=64*1024*1024


def prune_backups(folder, count=BACKUP_COUNT, budget=BACKUP_BUDGET):
 files=sorted((p for p in folder.iterdir() if not p.is_symlink() and p.is_file()
               and re.fullmatch(r'screenwatch-[0-9]+[.]json',p.name)),
              key=lambda p:int(p.stem.split('-')[-1]),reverse=True)
 kept=0;total=0;removed=0
 for p in files:
  size=p.stat().st_size
  if kept and (kept>=count or total+size>budget):
   p.unlink();removed+=size
  else:kept+=1;total+=size
 return {'count':kept,'bytes':total,'removed_bytes':removed}


EXPIRED_DAYS=2


def prune_expired(db,now):
 # Past showtimes otherwise accumulate forever (about 8 MB a week), slowing every
 # options read and eventually pushing the backup over its 32 MB limit.
 # Showtimes a saved row alert points at are kept for its history.
 import datetime as dt
 from api_source import IST
 prefs.ensure(db);limit=now-EXPIRED_DAYS*86400
 kept="SELECT json_extract(payload,'$.row_show') FROM alerts WHERE coalesce(json_extract(payload,'$.row_show'),'')<>''"
 db.execute('DELETE FROM sessions WHERE cutoff<? AND id NOT IN ('+kept+')',(limit,))
 if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='row_layouts'").fetchone():
  db.execute('DELETE FROM row_layouts WHERE show_id NOT IN (SELECT id FROM sessions) AND show_id NOT IN ('+kept+')')
 for (job,) in db.execute('SELECT id FROM jobs WHERE expires>0 AND expires<?',(limit,)).fetchall():
  db.execute('DELETE FROM meta WHERE key IN (?,?)',('waiting_show:'+job,'listing_success_streak:'+job))
 db.execute('DELETE FROM jobs WHERE expires>0 AND expires<?',(limit,))
 day=dt.datetime.fromtimestamp(limit,IST).strftime('%Y%m%d')
 old="id GLOB 'api:*:[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]' AND substr(id,-8)<?"
 db.execute("DELETE FROM meta WHERE key IN (SELECT 'revision:'||id FROM snapshots WHERE "+old+')',(day,))
 db.execute('DELETE FROM snapshots WHERE '+old,(day,))


def cleanup_history(db,now):
 if now-float(meta(db,'last_history_cleanup','0'))<3600:return
 # Preserve undelivered messages and every saved rule/baseline.
 db.execute('UPDATE outbox SET photo=NULL WHERE delivered>0 AND delivered<? AND photo IS NOT NULL',(now-86400,))
 db.execute('DELETE FROM outbox WHERE delivered>0 AND delivered<?',(now-7*86400,))
 db.execute('DELETE FROM metric_history WHERE at<?',(now-7*86400,))
 db.execute('DELETE FROM check_intervals WHERE at<?',(now-7*86400,))
 db.execute('DELETE FROM check_stats WHERE last_attempt<?',(now-30*86400,))
 prune_expired(db,now)
 setmeta(db,'last_history_cleanup',now);db.commit()
 # Deleted SQLite pages are reused. Avoid VACUUM's full-size temporary copy.
 db.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchall()


def automatic(db,path):
 now=time.time()
 folder=Path(path).resolve().parent/'backups'
 if folder.is_symlink():raise ValueError('Backup directory cannot be a symlink')
 folder.mkdir(exist_ok=True)
 # Prune before the hourly check, freeing old copies immediately after rollout.
 previous=json.loads(meta(db,'storage_usage','{}'))
 summary=prune_backups(folder)
 removed=summary['removed_bytes']+previous.get('removed_backup_bytes',0)
 cleanup_history(db,now)
 if now-float(meta(db,'last_backup','0'))>=3600:
  payload=json.dumps(export(db),ensure_ascii=False).encode()
  if len(payload)>MAX_BYTES:raise ValueError('Backup exceeds 32 MB; download a database snapshot with operator tools')
  spare=prune_backups(folder,count=BACKUP_COUNT-1,budget=BACKUP_BUDGET-len(payload))
  removed+=spare['removed_bytes']
  if shutil.disk_usage(folder).free<len(payload)+8*1024*1024:
   raise ValueError('Not enough free storage for a safe backup')
  target=folder/('screenwatch-'+str(int(now))+'.json');temporary=target.with_suffix('.tmp')
  try:temporary.write_bytes(payload);temporary.replace(target)
  finally:
   if temporary.exists():temporary.unlink()
  setmeta(db,'last_backup',now)
  summary=prune_backups(folder);removed+=summary['removed_bytes']
 base=Path(path)
 files=[base,Path(str(base)+'-wal'),Path(str(base)+'-shm')]
 setmeta(db,'storage_usage',json.dumps({'at':now,'backup_count':summary['count'],
  'backup_bytes':summary['bytes'],'backup_budget_bytes':BACKUP_BUDGET,
  'removed_backup_bytes':removed,'database_bytes':sum(p.stat().st_size for p in files if p.exists()),
  'filesystem_free_bytes':shutil.disk_usage(folder).free,
  'history_days':7,'photo_days':1}))
 db.commit()
