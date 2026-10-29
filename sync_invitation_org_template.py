"""Env-gated one-off: update the confirmed 'invitation_org' email template in the
DB to the current code default (uses {{ brand_full }} and the 'learn more' link).

The live org-invitation email is rendered from the confirmed DB template, so code
changes to EMAIL_TEMPLATES don't take effect until the DB row is updated. Guarded
by SYNC_INVITE_ORG=1. Records result (incl. resolved brand_full/contact_email) in
the 'sync_invite_org_status' setting.
"""
import os

if os.environ.get('SYNC_INVITE_ORG', '') != '1':
    raise SystemExit(0)

from app import app, db, EmailTemplate, set_setting
from utils.email_sender import EMAIL_TEMPLATES, _brand_full, _site_contact_email

with app.app_context():
    default = next(t for t in EMAIL_TEMPLATES if t['key'] == 'invitation_org')
    tmpl = EmailTemplate.query.filter_by(key='invitation_org').first()
    if not tmpl:
        tmpl = EmailTemplate(key='invitation_org')
        db.session.add(tmpl)
    tmpl.subject = default['subject']
    tmpl.title = default['title']
    tmpl.body_html = default['body_html']
    tmpl.is_confirmed = True
    db.session.commit()
    msg = (f"done: invitation_org updated & confirmed | "
           f"brand_full={_brand_full()!r} contact_email={_site_contact_email()!r}")
    set_setting('sync_invite_org_status', msg)
    print(f"[sync_invitation_org_template] {msg}")
