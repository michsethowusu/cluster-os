#!/bin/sh
set -e

python migrate.py

python -c "
import os
from app import app, db, User, StakeholderType, Label, EmailTemplate, DEFAULT_STAKEHOLDER_TYPES, LABEL_DEFAULTS
from werkzeug.security import generate_password_hash

with app.app_context():
    # Create upload folder
    folder = os.path.join(app.config['UPLOAD_FOLDER'], 'event_attachments')
    os.makedirs(folder, exist_ok=True)
    print(f'Upload folder ready: {folder}')

    # Seed default stakeholder types (idempotent)
    if not StakeholderType.query.first():
        for i, name in enumerate(DEFAULT_STAKEHOLDER_TYPES):
            db.session.add(StakeholderType(
                name=name,
                is_member_state=False,
                is_active=True,
                order=i,
            ))
        print('Default stakeholder types seeded.')

    # Seed default labels (idempotent)
    if not Label.query.first():
        for key, _ in LABEL_DEFAULTS.items():
            db.session.add(Label(key=key, value='', category=key.split('_')[0]))
        print('Default labels seeded.')

    # Seed default email templates (idempotent)
    from utils.email_sender import EMAIL_TEMPLATES
    if not EmailTemplate.query.first():
        for et in EMAIL_TEMPLATES:
            db.session.add(EmailTemplate(
                key=et['key'],
                subject=et['subject'],
                title=et['title'],
                body_html=et['body_html'],
                is_confirmed=False,
            ))
        print(f'{len(EMAIL_TEMPLATES)} default email templates seeded.')

    # Create/update admin user from environment (idempotent)
    admin_email = os.environ.get('ADMIN_EMAIL', '').strip()
    admin_password = os.environ.get('ADMIN_PASSWORD', '').strip()
    if admin_email:
        # Demote any user who previously had admin but no longer matches the configured email
        for u in User.query.filter(User.email != admin_email, User.is_admin == True).all():
            u.is_admin = False
            print(f'Admin demoted: {u.email}')

        admin = User.query.filter_by(email=admin_email).first()
        if not admin:
            admin = User(
                email=admin_email,
                name=os.environ.get('ADMIN_NAME', 'Administrator'),
                organization=os.environ.get('ADMIN_ORG', 'AU ECED-FLN'),
                stakeholder_type='Government',
                country=os.environ.get('ADMIN_COUNTRY', 'Ethiopia'),
                is_approved=True,
                is_admin=True,
            )
            if admin_password:
                admin.password_hash = generate_password_hash(admin_password)
            db.session.add(admin)
            print(f'Admin user created ({admin_email}).')
        else:
            # Ensure admin flags are set on every boot (in case of earlier incomplete init)
            admin.is_admin = True
            admin.is_approved = True
            if admin_password:
                admin.password_hash = generate_password_hash(admin_password)
            print(f'Admin user updated ({admin_email}).')

    db.session.commit()
"

# One-time AI backfills (each guarded by its own DB flag, so they only do work
# once). Run in the background so they never block startup.
python -u backfill_summaries.py &
python -u backfill_titles.py &
# Env-gated incident cleanup (no-ops unless their env flags are set)
python -u quarantine_unverified.py &
python -u purge_unverified.py &
python -u rescore_unverified.py &

exec gunicorn -w 4 -b 0.0.0.0:3000 app:app
