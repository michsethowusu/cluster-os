"""Env-gated background purge of quarantined spam — fast bulk SQL.

Permanently deletes initiatives with no AI quality score (the exploit signature)
and the throwaway accounts that authored them (non-admin, unapproved, left with
no other content). Uses set-based SQL so it completes in seconds even for
thousands of rows, instead of slow ORM per-row deletes.

Guarded by PURGE_UNVERIFIED=1. Progress/result in the 'purge_status' setting.
"""
import os

if os.environ.get('PURGE_UNVERIFIED', '') != '1':
    raise SystemExit(0)

from sqlalchemy import text
from app import app, db, set_setting

# Users we may delete: non-admin, unapproved, and with no remaining real content
# (their spam initiative is removed first, below).
_SPAM_USERS = (
    'SELECT id FROM "user" u WHERE u.is_admin = false AND u.is_approved = false '
    'AND NOT EXISTS (SELECT 1 FROM initiative i WHERE i.user_id = u.id) '
    'AND NOT EXISTS (SELECT 1 FROM question q WHERE q.user_id = u.id) '
    'AND NOT EXISTS (SELECT 1 FROM recommendation r WHERE r.user_id = u.id) '
    'AND NOT EXISTS (SELECT 1 FROM document_library d WHERE d.submitted_by = u.id)'
)

with app.app_context():
    try:
        db.session.execute(text("SET statement_timeout = '120s'"))

        # 1. Delete the unverified initiatives. The tag link-table FK has no
        #    ON DELETE CASCADE, so clear it first; comment/learn_more_request/
        #    initiative_send_queue cascade at the DB level.
        db.session.execute(text(
            "DELETE FROM initiative_tags WHERE initiative_id IN "
            "(SELECT id FROM initiative WHERE quality_score IS NULL)"))
        r1 = db.session.execute(text("DELETE FROM initiative WHERE quality_score IS NULL"))
        deleted_ini = r1.rowcount or 0
        db.session.commit()

        # 2. Delete throwaway spam accounts (child rows without cascade first).
        for tbl in ('member_project', 'project_participation', 'event_registration', 'vote'):
            db.session.execute(text(f'DELETE FROM {tbl} WHERE user_id IN ({_SPAM_USERS})'))
        r2 = db.session.execute(text(f'DELETE FROM "user" WHERE id IN ({_SPAM_USERS})'))
        deleted_acc = r2.rowcount or 0
        db.session.commit()

        msg = f'done: deleted {deleted_ini} initiative(s) and {deleted_acc} account(s)'
    except Exception as e:
        db.session.rollback()
        msg = f'error: {e}'
    set_setting('purge_status', msg)
    print(f'[purge_unverified] {msg}')
