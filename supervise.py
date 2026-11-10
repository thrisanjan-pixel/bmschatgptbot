"""Restart development services while this machine remains active.

This does not turn an ephemeral cloud workspace into a 24/7 host.
"""
import fcntl
import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time


def main():
    root=Path(__file__).resolve().parent
    parser=argparse.ArgumentParser()
    parser.add_argument('--db',default=os.environ.get('BMS_DB','/workspace/booking-tracker/state.sqlite3'))
    parser.add_argument('--allow-local-setup',action='store_true')
    parser.add_argument('--api',action='store_true',help='Run the browser-free HTTP monitor')
    args=parser.parse_args()
    db=Path(args.db)
    db.parent.mkdir(parents=True,exist_ok=True)
    lock=open(str(db)+'.supervisor.lock','a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise SystemExit('Development supervisor already running')
    if os.environ.get('BMS_SOURCE_NOT_BEFORE'):
        from store import connect
        from http_source import carry_over
        state=connect(str(db))
        try:print(f'Carried over source deadlines: {carry_over(state,os.environ["BMS_SOURCE_NOT_BEFORE"])}',flush=True)
        except ValueError as error:print(f'Ignoring malformed BMS_SOURCE_NOT_BEFORE: {error}',flush=True)  # a bad value must not crash-loop the service
        finally:state.close()
    stop=threading.Event()
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:stop.set())
    processes={}
    next_start={name:0 for name in ('api_watch.py' if args.api else 'city_watch.py','dashboard.py')}
    if args.api:next_start['health_watch.py']=0
    if args.api and os.environ.get('BMS_ROWS_ENABLED')=='1':next_start['row_watch.py']=0
    try:
        while not stop.is_set():
            for name in next_start:
                process=processes.get(name)
                if process and process.poll() is not None:
                    print(f'{name} exited ({process.returncode}); restarting in 10 seconds',flush=True)
                    processes.pop(name);next_start[name]=time.monotonic()+10
                if name not in processes and time.monotonic()>=next_start[name]:
                    extra=['--allow-local-setup'] if name=='city_watch.py' and args.allow_local_setup else []
                    processes[name]=subprocess.Popen([sys.executable,'-u',str(root/name),'--db',str(db),*extra],cwd=root)
                    print(f'Started {name}, PID {processes[name].pid}',flush=True)
            stop.wait(1)
    finally:
        for process in processes.values():
            if process.poll() is None:process.terminate()
        deadline=time.monotonic()+90
        for process in processes.values():
            try:process.wait(timeout=max(1,deadline-time.monotonic()))
            except subprocess.TimeoutExpired:process.kill();process.wait()


if __name__=='__main__':main()
