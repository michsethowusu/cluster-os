#!/bin/sh
set -e

python migrate.py

python -c "
import os
from app import app, db, User, StakeholderType, Label, DEFAULT_STAKEHOLDER_TYPES, LABEL_DEFAULTS
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
                is_member_state=(name == 'Member State'),
                is_active=True,
                order=i,
            ))
        print('Default stakeholder types seeded.')

    # Seed default labels (idempotent)
    if not Label.query.first():
        for key, _ in LABEL_DEFAULTS.items():
            db.session.add(Label(key=key, value='', category=key.split('_')[0]))
        print('Default labels seeded.')

    # Create admin user from environment (idempotent)
    admin_email = os.environ.get('ADMIN_EMAIL', '').strip()
    admin_password = os.environ.get('ADMIN_PASSWORD', '').strip()
    if admin_email:
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
        elif admin_password and not admin.password_hash:
            admin.password_hash = generate_password_hash(admin_password)
            print(f'Admin password set ({admin_email}).')

    db.session.commit()
"

exec gunicorn -w 4 -b 0.0.0.0:3000 app:app
