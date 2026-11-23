"""Hot-standby role of this instance.

BOT_ROLE (primary or standby) is what the instance was deployed as. The runtime role
(active or standby) decides whether the Telegram sender runs. It is kept in the
database so a restart keeps a promotion. Polling runs in both roles.
"""
import logging
import math
import os

from store import meta, setmeta

ACTIVE = 'active'
STANDBY = 'standby'
ROLES = (ACTIVE, STANDBY)


def configured_role():
    """'primary' or 'standby' from BOT_ROLE. Anything else counts as primary."""
    value = (os.environ.get('BOT_ROLE') or 'primary').strip().lower()
    return value if value in ('primary', 'standby') else 'primary'


def start_role():
    return ACTIVE if configured_role() == 'primary' else STANDBY


def runtime_role(db):
    """'active' or 'standby'. Read only: never takes a write lock (the sender calls it every loop)."""
    value = meta(db, 'runtime_role', '')
    return value if value in ROLES else start_role()


def initialize(db):
    """Store the start role once per database, so later restarts keep any promotion. Returns the role."""
    value = meta(db, 'runtime_role', '')
    if value not in ROLES:
        value = start_role()
        setmeta(db, 'runtime_role', value)
    db.commit()
    return value


def set_role(db, value, reason=''):
    """Store a new runtime role. Returns True when it changed."""
    if value not in ROLES:
        raise ValueError('Unknown role')
    before = runtime_role(db)
    if before == value:
        setmeta(db, 'runtime_role', value)
        return False
    setmeta(db, 'runtime_role', value)
    import time
    setmeta(db, 'role_changed_at', time.time())
    setmeta(db, 'role_change_reason', reason[:200])
    logging.warning('Runtime role changed: %s -> %s (%s)', before, value, reason or 'no reason given')
    return True


def sender_active(db):
    """True when this instance may send Telegram messages."""
    return runtime_role(db) == ACTIVE


def standby_scale(db):
    """Share of the normal BookMyShow request rate this instance may use.

    1 while active. While standby, STANDBY_RATE_FACTOR (default 0.5), so a hot standby
    adds at most half the primary's traffic. Clamped to 0.1..1."""
    if runtime_role(db) == ACTIVE:
        return 1.0
    try:
        value = float(os.environ.get('STANDBY_RATE_FACTOR', '0.5'))
    except ValueError:
        value = 0.5
    if not math.isfinite(value):
        value = 0.5
    return min(1.0, max(0.1, value))


def describe(db):
    return {'configured_role': configured_role(), 'role': runtime_role(db),
            'role_changed_at': float(meta(db, 'role_changed_at', '0') or 0),
            'role_change_reason': meta(db, 'role_change_reason', '')}
