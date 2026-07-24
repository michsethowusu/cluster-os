"""Env-gated one-shot cleanup for the auto-approve exploit.

When AI scoring was unavailable (expired API key), submissions were published
without a real quality score — signature: is_published=True AND quality_score IS
NULL. This quarantines exactly those: unpublishes them (off the public site) and
un-approves their non-admin authors (locks the accounts). It is REVERSIBLE — an
admin can approve/publish any false positives, or permanently delete the spam
from Admin -> Unverified Submissions.

Guarded by QUARANTINE_UNVERIFIED=1 so it only runs when explicitly enabled
(set it on the affected app only).
"""
import os

if os.environ.get('QUARANTINE_UNVERIFIED', '') != '1':
    raise SystemExit(0)

from app import app, db, Initiative, User, set_setting

with app.app_context():
    unverified = Initiative.query.filter(
        Initiative.is_published == True,          # noqa: E712
        Initiative.quality_score.is_(None),
    ).all()
    author_ids = set()
    for ini in unverified:
        ini.is_published = False
        author_ids.add(ini.user_id)
    db.session.commit()

    locked = 0
    for uid in author_ids:
        u = User.query.get(uid)
        if u and not u.is_admin and u.is_approved:
            u.is_approved = False
            locked += 1
    db.session.commit()

    msg = f'unpublished {len(unverified)} unverified initiative(s), locked {locked} account(s)'
    set_setting('quarantine_status', msg)
    print(f'[quarantine_unverified] {msg}.')
