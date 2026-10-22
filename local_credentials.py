"""Opt-in local credentials stored outside the repository, readable only by owner."""
import json
import os
from pathlib import Path

KEYS = ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID')


def location():
    return Path.home()/'.config'/'bms-watch'/'telegram.json'


def load():
    if not all(os.environ.get(k) for k in KEYS) and location().exists():
        try:
            values=json.loads(location().read_text())
            if not all(isinstance(values.get(k),str) and values[k] for k in KEYS):return False
        except (OSError,ValueError,AttributeError):return False
        for key in KEYS:
            if not os.environ.get(key):os.environ[key]=values[key]
    return all(os.environ.get(k) for k in KEYS)


def save(values):
    path=location()
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temporary=path.with_suffix('.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    try:
        with os.fdopen(fd,'w') as stream:json.dump({k:values[k] for k in KEYS},stream)
        os.chmod(temporary,0o600)
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)
