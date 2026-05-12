#!/bin/sh
set -e

python migrate.py

python -c "
import os
from app import app
folder = os.path.join(app.config['UPLOAD_FOLDER'], 'event_attachments')
os.makedirs(folder, exist_ok=True)
print(f'Upload folder ready: {folder}')
"

exec gunicorn -w 4 -b 0.0.0.0:3000 app:app
