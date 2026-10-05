"""Env-gated one-off: update the single document/policy email templates in the DB
to the richer built-in default (full block with description + working button),
keeping them confirmed so sends use them immediately.

Guard: SYNC_SINGLE_TEMPLATES=1. Status in 'sync_single_templates_status'.
"""
import os

if os.environ.get('SYNC_SINGLE_TEMPLATES', '') != '1':
    raise SystemExit(0)

from app import app, db, EmailTemplate, set_setting
from utils.email_sender import EMAIL_TEMPLATES

KEYS = ['document_single', 'policy_single']

with app.app_context():
    updated = []
    for key in KEYS:
        default = next((t for t in EMAIL_TEMPLATES if t['key'] == key), None)
        if not default:
            continue
        tmpl = EmailTemplate.query.filter_by(key=key).first()
        if tmpl:
            tmpl.subject = default['subject']
            tmpl.title = default['title']
            tmpl.body_html = default['body_html']
            tmpl.is_confirmed = True
        else:
            db.session.add(EmailTemplate(
                key=key, subject=default['subject'], title=default['title'],
                body_html=default['body_html'], is_confirmed=True))
        updated.append(key)
    db.session.commit()
    msg = 'done: ' + ', '.join(updated)
    set_setting('sync_single_templates_status', msg)
    print(f'[sync_single_templates] {msg}')
