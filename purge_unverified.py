"""Env-gated background purge of quarantined spam.

Permanently deletes initiatives that have no AI quality score (the exploit's
signature) and the throwaway accounts that authored them (non-admin accounts
left with no other real content). DB-only, batched, resumable via re-run.

Guarded by PURGE_UNVERIFIED=1 so it only runs when explicitly enabled on the
affected app. Progress is written to 'purge_status' (see /backfill-status).
"""
import os

if os.environ.get('PURGE_UNVERIFIED', '') != '1':
    raise SystemExit(0)

from app import (app, db, Initiative, User, Question, Recommendation, DocumentLibrary,
                 Certificate, Comment, LearnMoreRequest, MemberProject,
                 ProjectParticipation, EventRegistration, Vote, set_setting)

with app.app_context():
    ids = [r.id for r in Initiative.query.filter(Initiative.quality_score.is_(None))
           .with_entities(Initiative.id).all()]
    total = len(ids)
    print(f'[purge_unverified] deleting {total} unverified initiative(s)...')
    set_setting('purge_status', f'running 0/{total} initiatives')

    author_ids = set()
    deleted_ini = 0
    for n, iid in enumerate(ids, 1):
        ini = Initiative.query.get(iid)
        if ini:
            author_ids.add(ini.user_id)
            db.session.delete(ini)   # ORM clears tag links; DB cascades child rows
            deleted_ini += 1
        if n % 50 == 0:
            db.session.commit()
            set_setting('purge_status', f'running {n}/{total} initiatives')
    db.session.commit()

    deleted_accounts = 0
    for uid in author_ids:
        u = User.query.get(uid)
        if not u or u.is_admin:
            continue
        remaining = (Initiative.query.filter_by(user_id=uid).count()
                     + Question.query.filter_by(user_id=uid).count()
                     + Recommendation.query.filter_by(user_id=uid).count()
                     + DocumentLibrary.query.filter_by(submitted_by=uid).count())
        if remaining:
            continue  # keep accounts that still have real content
        Certificate.query.filter_by(user_id=uid).delete()
        Comment.query.filter_by(user_id=uid).delete()
        LearnMoreRequest.query.filter_by(requester_id=uid).delete()
        MemberProject.query.filter_by(user_id=uid).delete()
        ProjectParticipation.query.filter_by(user_id=uid).delete()
        EventRegistration.query.filter_by(user_id=uid).delete()
        Vote.query.filter_by(user_id=uid).delete()
        db.session.delete(u)
        deleted_accounts += 1
        if deleted_accounts % 50 == 0:
            db.session.commit()
            set_setting('purge_status', f'deleted {deleted_ini} initiatives, {deleted_accounts} accounts...')
    db.session.commit()

    msg = f'done: deleted {deleted_ini} initiative(s) and {deleted_accounts} account(s)'
    set_setting('purge_status', msg)
    print(f'[purge_unverified] {msg}.')
