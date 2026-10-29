"""Env-gated one-off: send a single org-invitation email for testing.

Guarded by SEND_TEST_INVITE=1. Recipient/org from TEST_INVITE_EMAIL /
TEST_INVITE_ORG. Sends via the same send_invitation_email() the bulk invite uses
(no name -> greets the organisation). Result in 'send_test_invite_status'.
"""
import os

if os.environ.get('SEND_TEST_INVITE', '') != '1':
    raise SystemExit(0)

from app import app, set_setting
from utils.email_sender import send_invitation_email

email = (os.environ.get('TEST_INVITE_EMAIL') or '').strip()
org = (os.environ.get('TEST_INVITE_ORG') or 'Test Organisation').strip()

with app.app_context():
    if not email:
        set_setting('send_test_invite_status', 'error: TEST_INVITE_EMAIL not set')
        raise SystemExit(0)
    try:
        ok = send_invitation_email(email, '', organization=org)
        msg = (f"sent to {email} (org={org!r})" if ok is not False
               else f"NOT sent to {email}: template not confirmed")
    except Exception as e:
        msg = f"error sending to {email}: {str(e)[:200]}"
    set_setting('send_test_invite_status', msg)
    print(f"[send_test_invite] {msg}")
