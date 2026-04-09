#!/bin/sh
set -e

python -c "
from app import app, db
with app.app_context():
    db.create_all()
    # Add new columns to existing tables if they don't exist yet
    with db.engine.connect() as conn:
        # Update User table
        conn.execute(db.text('''
            ALTER TABLE \"user\"
            ADD COLUMN IF NOT EXISTS password_hash VARCHAR(256)
        '''))
        conn.execute(db.text('''
            ALTER TABLE \"user\"
            ADD COLUMN IF NOT EXISTS points INTEGER DEFAULT 0
        '''))
        conn.execute(db.text('''
            ALTER TABLE \"user\"
            ADD COLUMN IF NOT EXISTS is_subscribed BOOLEAN DEFAULT true
        '''))
        
        # Update Initiative table
        conn.execute(db.text('''
            ALTER TABLE initiative
            ADD COLUMN IF NOT EXISTS view_count INTEGER DEFAULT 0
        '''))
        conn.execute(db.text('''
            ALTER TABLE initiative
            ADD COLUMN IF NOT EXISTS quality_score INTEGER
        '''))
        
        # Update Project table
        conn.execute(db.text('''
            ALTER TABLE project
            ADD COLUMN IF NOT EXISTS is_published BOOLEAN DEFAULT FALSE
        '''))
        conn.execute(db.text('''
            ALTER TABLE project
            ADD COLUMN IF NOT EXISTS start_date TIMESTAMP
        '''))
        conn.execute(db.text('''
            ALTER TABLE project
            ADD COLUMN IF NOT EXISTS submitted_by INTEGER
        '''))

        # Update Event table
        conn.execute(db.text('''
            ALTER TABLE event
            ADD COLUMN IF NOT EXISTS is_published BOOLEAN DEFAULT FALSE
        '''))
        conn.execute(db.text('''
            ALTER TABLE event
            ADD COLUMN IF NOT EXISTS submitted_by INTEGER
        '''))
        conn.execute(db.text('''
            ALTER TABLE event
            ADD COLUMN IF NOT EXISTS zoom_webinar_id VARCHAR(100)
        '''))
        conn.execute(db.text('''
            ALTER TABLE event
            ADD COLUMN IF NOT EXISTS zoom_recording_url VARCHAR(500)
        '''))

        # Add blocked_email table (for unsubscribed non-members from import system)
        conn.execute(db.text('''
            CREATE TABLE IF NOT EXISTS blocked_email (
                id SERIAL PRIMARY KEY,
                email VARCHAR(120) UNIQUE NOT NULL,
                blocked_at TIMESTAMP DEFAULT NOW()
            )
        '''))

        conn.commit()
    print('DB ready.')
"

# Ensure the event attachments upload directory exists
python -c "
import os
from app import app
folder = os.path.join(app.config['UPLOAD_FOLDER'], 'event_attachments')
os.makedirs(folder, exist_ok=True)
print(f'Upload folder ready: {folder}')
"

exec gunicorn -w 4 -b 0.0.0.0:3000 app:app
