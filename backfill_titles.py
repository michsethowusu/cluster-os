"""One-time backfill: tidy the capitalisation/punctuation of every initiative
title with AI (without rewording it — clean_title guarantees that).

Runs in the background from entrypoint.sh, guarded by the 'titles_backfilled'
setting so it does real work only once. Progress is written to the
'titles_backfill_status' setting for the admin dashboard.
"""
import time

from app import app, db, Initiative, get_setting, set_setting
from utils.ai_services import clean_title

with app.app_context():
    if get_setting('titles_backfilled', 'false') == 'true':
        print('[backfill_titles] already completed; skipping.')
    else:
        ids = [row.id for row in Initiative.query.with_entities(Initiative.id).all()]
        total = len(ids)
        print(f'[backfill_titles] cleaning {total} initiative title(s)...')
        set_setting('titles_backfill_status', f'running 0/{total}')
        changed = 0
        for n, iid in enumerate(ids, 1):
            try:
                ini = Initiative.query.get(iid)
                if not ini:
                    continue
                cleaned = clean_title(ini.title)
                if cleaned and cleaned != ini.title:
                    ini.title = cleaned
                    db.session.commit()
                    changed += 1
            except Exception as e:
                db.session.rollback()
                print(f'[backfill_titles] error on initiative {iid}: {e}')
            if n % 5 == 0 or n == total:
                set_setting('titles_backfill_status', f'running {n}/{total}')
            time.sleep(0.3)  # be gentle on the AI API
        set_setting('titles_backfill_status', f'done {changed} changed / {total} checked')
        set_setting('titles_backfilled', 'true')
        print(f'[backfill_titles] done: {changed}/{total} titles cleaned.')
