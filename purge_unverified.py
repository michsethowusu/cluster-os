"""Env-gated background purge of quarantined spam — fast bulk SQL.

Permanently deletes initiatives with no AI quality score (the exploit signature)
and the throwaway accounts that authored them (non-admin, unapproved, left with
no other content). Uses set-based SQL so it completes in seconds even for
thousands of rows, instead of slow ORM per-row deletes.

Guarded by PURGE_UNVERIFIED=1. Progress/result in the 'purge_status' setting.
"""
import os
import json

MODE = os.environ.get('PURGE_UNVERIFIED', '')
if MODE not in ('1', 'report'):
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

def _scalar(sql):
    return db.session.execute(text(sql)).scalar() or 0


if MODE == 'report':
    # Dry run: describe what a purge WOULD affect, delete nothing.
    with app.app_context():
        try:
            rep = {
                'total_initiatives': _scalar('SELECT COUNT(*) FROM initiative'),
                'published': _scalar('SELECT COUNT(*) FROM initiative WHERE is_published = true'),
                'null_score_total': _scalar('SELECT COUNT(*) FROM initiative WHERE quality_score IS NULL'),
                'null_score_published': _scalar('SELECT COUNT(*) FROM initiative WHERE quality_score IS NULL AND is_published = true'),
                'unpublished_score_1_2': _scalar('SELECT COUNT(*) FROM initiative WHERE is_published = false AND quality_score IN (1,2)'),
                'unpublished_score_3_5': _scalar('SELECT COUNT(*) FROM initiative WHERE is_published = false AND quality_score >= 3'),
                'null_score_last_3d': _scalar("SELECT COUNT(*) FROM initiative WHERE quality_score IS NULL AND created_at > now() - interval '3 days'"),
                'deletable_accounts_est': _scalar(
                    'SELECT COUNT(*) FROM "user" u WHERE u.is_admin = false AND u.is_approved = false '
                    'AND NOT EXISTS (SELECT 1 FROM initiative i WHERE i.user_id = u.id AND (i.quality_score IS NOT NULL OR i.is_published = true)) '
                    'AND NOT EXISTS (SELECT 1 FROM question q WHERE q.user_id = u.id) '
                    'AND NOT EXISTS (SELECT 1 FROM recommendation r WHERE r.user_id = u.id) '
                    'AND NOT EXISTS (SELECT 1 FROM document_library d WHERE d.submitted_by = u.id)'),
                'new_unapproved_users_last_3d': _scalar("SELECT COUNT(*) FROM \"user\" WHERE is_approved = false AND is_admin = false AND created_at > now() - interval '3 days'"),
            }
            rows = db.session.execute(text(
                "SELECT title FROM initiative WHERE quality_score IS NULL ORDER BY created_at DESC LIMIT 8")).fetchall()
            rep['sample_null_titles'] = [r[0] for r in rows]
            msg = 'report: ' + json.dumps(rep, ensure_ascii=False)
        except Exception as e:
            db.session.rollback()
            msg = f'report error: {e}'
        set_setting('purge_status', msg)
        print(f'[purge_unverified] {msg}')
    raise SystemExit(0)


with app.app_context():
    try:
        db.session.execute(text("SET statement_timeout = '120s'"))

        # Held junk = unpublished initiatives that are either unscored or AI-scored
        # 1-2. Published initiatives and unpublished score>=3 are always kept.
        where = "is_published = false AND (quality_score IS NULL OR quality_score IN (1,2))"
        # 1. Clear every child table that references the target initiatives (some
        #    FKs lack ON DELETE CASCADE), then delete the initiatives themselves.
        target = f"(SELECT id FROM initiative WHERE {where})"
        for child, col in [
            ('noun_phrase', 'initiative_id'),
            ('recommendation', 'initiative_id'),
            ('comment', 'initiative_id'),
            ('learn_more_request', 'initiative_id'),
            ('initiative_send_queue', 'initiative_id'),
            ('initiative_tags', 'initiative_id'),
        ]:
            db.session.execute(text(f"DELETE FROM {child} WHERE {col} IN {target}"))
        r1 = db.session.execute(text(f"DELETE FROM initiative WHERE {where}"))
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
