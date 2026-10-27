"""Send the weekly standards-review emails.

One email per reviewer per week, at most. For each accepted reviewer whose last
review email was >= 7 days ago, pick their earliest-accepted glossary term that
still has standards they haven't rated, and email them the next 5. When a term is
fully rated, their participation for it is marked completed and the next term is
used the following week.

Run by the weekly scheduled task, or on demand from the admin 'Send due emails' button.
"""
import os
from datetime import datetime, timedelta

from app import (app, db, User, GlossaryTerm, Standard, StandardTerm,
                 StandardRating, StandardReviewParticipant, get_setting)
from utils.email_sender import send_review_weekly


def _abs(path):
    base = (os.environ.get('APP_URL') or get_setting('site_url', '') or '').rstrip('/')
    return f"{base}{path}" if base else path


def _test_target():
    email = (os.environ.get('REVIEW_TEST_EMAIL') or '').strip() or app.config.get('ADMIN_OTP_EMAIL')
    if email:
        return email
    a = User.query.filter_by(is_admin=True).first()
    return a.email if a else None


def run():
    with app.app_context():
        now = datetime.utcnow()
        test_mode = get_setting('review_test_mode', 'false') == 'true'
        test_target = _test_target() if test_mode else None
        uids = [r[0] for r in db.session.query(StandardReviewParticipant.user_id)
                .filter_by(status='accepted').distinct().all()]
        sent = 0
        for uid in uids:
            u = User.query.get(uid)
            if not u:
                continue
            if u.last_review_email_at and (now - u.last_review_email_at) < timedelta(days=7):
                continue
            parts = (StandardReviewParticipant.query
                     .filter_by(user_id=uid, status='accepted')
                     .order_by(StandardReviewParticipant.accepted_at.asc()).all())
            rated = {r.standard_id for r in StandardRating.query.filter_by(user_id=uid).all()}
            for p in parts:
                stds = (Standard.query
                        .join(StandardTerm, StandardTerm.standard_id == Standard.id)
                        .filter(StandardTerm.term_id == p.term_id)
                        .order_by(Standard.id.asc()).all())
                unrated = [s for s in stds if s.id not in rated]
                if not unrated:
                    p.status = 'completed'
                    db.session.commit()
                    continue
                batch = unrated[:5]
                try:
                    send_review_weekly(u, p.term.term, _abs('/review/' + p.token), batch,
                                       to_email=(test_target if test_mode else None))
                    u.last_review_email_at = now
                    db.session.commit()
                    sent += 1
                except Exception as e:
                    db.session.rollback()
                    app.logger.error(f"send_review_weekly error (user {uid}): {e}")
                break   # one email per reviewer per run/week
        print(f"[send_review_emails] sent {sent} email(s)")


if __name__ == '__main__':
    run()
