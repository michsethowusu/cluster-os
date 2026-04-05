#!/bin/sh
set -e
flask init-db 2>/dev/null || python -c "
from app import app, db
with app.app_context():
    db.create_all()
    print('DB tables created.')
"
exec gunicorn -w 4 -b 0.0.0.0:3000 app:app
