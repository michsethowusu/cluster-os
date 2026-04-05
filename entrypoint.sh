#!/bin/sh
set -e

python -c "
from app import app, db
with app.app_context():
    db.create_all()
    # Add new columns to existing tables if they don't exist yet
    with db.engine.connect() as conn:
        conn.execute(db.text('''
            ALTER TABLE \"user\"
            ADD COLUMN IF NOT EXISTS password_hash VARCHAR(256)
        '''))
        conn.execute(db.text('''
            ALTER TABLE \"user\"
            ADD COLUMN IF NOT EXISTS points INTEGER DEFAULT 0
        '''))
        conn.commit()
    print('DB ready.')
"

exec gunicorn -w 4 -b 0.0.0.0:3000 app:app
