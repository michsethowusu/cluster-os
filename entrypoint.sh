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

        # ── Zoom integration columns ──────────────────────────────────────
        # zoom_webinar_id: stores the Zoom Meeting ID (column kept for backwards DB compat) created via API
        conn.execute(db.text('''
            ALTER TABLE event
            ADD COLUMN IF NOT EXISTS zoom_webinar_id VARCHAR(100)
        '''))
        # zoom_recording_url: fetched after the event ends
        conn.execute(db.text('''
            ALTER TABLE event
            ADD COLUMN IF NOT EXISTS zoom_recording_url VARCHAR(500)
        '''))
        # meeting_link kept for backwards compatibility but no longer used (Zoom Meetings API now used)
        # (existing data preserved, new events use Zoom)

        # ── EventAttachment table (created by db.create_all above,
        #    but we ensure the folder exists via the app) ──────────────────
        # No manual ALTER needed — db.create_all() handles the new model.

        # Update Initiative table
        conn.execute(db.text('''
            ALTER TABLE initiative
            ADD COLUMN IF NOT EXISTS view_count INTEGER DEFAULT 0
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
