"""One-time backfill: regenerate every initiative's short_description with AI.

Runs in the background from entrypoint.sh and is guarded by the
'summaries_backfilled' setting, so it does real work only on the first deploy
after the AI-summary feature ships and is a no-op on every deploy afterwards.
Progress is written to the 'summaries_backfill_status' setting so it can be
shown in the admin dashboard.
"""
import os
import time

from app import app, db, Initiative, get_setting, set_setting
from utils.ai_services import generate_summary

# Set FORCE_SUMMARY_BACKFILL=1 (per-app env var) to re-run even if already done.
force = os.environ.get('FORCE_SUMMARY_BACKFILL', '') == '1'

with app.app_context():
    if not force and get_setting('summaries_backfilled', 'false') == 'true':
        print('[backfill_summaries] already completed; skipping.')
    else:
        ids = [row.id for row in Initiative.query.with_entities(Initiative.id).all()]
        total = len(ids)
        print(f'[backfill_summaries] regenerating summaries for {total} initiative(s)...')
        set_setting('summaries_backfill_status', f'running 0/{total}')
        done = 0
        for n, iid in enumerate(ids, 1):
            try:
                ini = Initiative.query.get(iid)
                if not ini:
                    continue
                summary = generate_summary(ini.title, ini.content or '')
                if summary:
                    ini.short_description = summary
                    db.session.commit()
                    done += 1
            except Exception as e:
                db.session.rollback()
                print(f'[backfill_summaries] error on initiative {iid}: {e}')
            if n % 5 == 0 or n == total:
                set_setting('summaries_backfill_status', f'running {n}/{total}')
            time.sleep(0.3)  # be gentle on the AI API
        set_setting('summaries_backfill_status', f'done {done}/{total}')
        set_setting('summaries_backfilled', 'true')
        print(f'[backfill_summaries] done: {done}/{total} summaries regenerated.')
