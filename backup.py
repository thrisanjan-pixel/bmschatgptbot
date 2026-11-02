"""Versioned, credential-free logical recovery with non-destructive imports."""
import json,time
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

def automatic(db,path):
 now=time.time()
 if now-float(meta(db,'last_backup','0'))<3600:return
 folder=Path(path).resolve().parent/'backups';folder.mkdir(exist_ok=True)
 payload=json.dumps(export(db),ensure_ascii=False).encode()
 if len(payload)>MAX_BYTES:raise ValueError('Backup exceeds 32 MB; download a database snapshot with operator tools')
 target=folder/('screenwatch-'+str(int(now))+'.json');temporary=target.with_suffix('.tmp');temporary.write_bytes(payload);temporary.replace(target)
 for old in sorted(folder.glob('screenwatch-*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[24:]:old.unlink()
 setmeta(db,'last_backup',now);db.commit()
