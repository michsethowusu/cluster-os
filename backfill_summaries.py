"""One-time backfill: regenerate every initiative's short_description with AI.

Runs in the background from entrypoint.sh and is guarded by the
'summaries_backfilled' setting, so it does real work only on the first deploy
after the AI-summary feature ships and is a no-op on every deploy afterwards.
"""
import time

from app import app, db, Initiative, get_setting, set_setting
from utils.ai_services import generate_summary

with app.app_context():
    if get_setting('summaries_backfilled', 'false') == 'true':
        print('[backfill_summaries] already completed; skipping.')
    else:
        ids = [row.id for row in Initiative.query.with_entities(Initiative.id).all()]
        print(f'[backfill_summaries] regenerating summaries for {len(ids)} initiative(s)...')
        done = 0
        for iid in ids:
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
            time.sleep(0.3)  # be gentle on the AI API
        set_setting('summaries_backfilled', 'true')
        print(f'[backfill_summaries] done: {done}/{len(ids)} summaries regenerated.')
