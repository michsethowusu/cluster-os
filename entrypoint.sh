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

        # Update Event table (Fixes your Admin Panel crash)
        conn.execute(db.text('''
            ALTER TABLE event
            ADD COLUMN IF NOT EXISTS is_published BOOLEAN DEFAULT FALSE
        '''))
        conn.execute(db.text('''
            ALTER TABLE event
            ADD COLUMN IF NOT EXISTS submitted_by INTEGER
        '''))

        # Update Initiative table
        conn.execute(db.text('''
            ALTER TABLE initiative
            ADD COLUMN IF NOT EXISTS view_count INTEGER DEFAULT 0
        '''))

        conn.commit()
    print('DB ready.')
"

exec gunicorn -w 4 -b 0.0.0.0:3000 app:app
