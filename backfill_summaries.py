"""Backfill: (re)generate initiative short_description with AI — robustly.

Design goals:
  * Resumable — each initiative that gets a summary is recorded in the
    'summaries_done_ids' setting, so re-runs SKIP anything already processed and
    only work on what is still pending.
  * Gentle on the API — a fixed 2-second delay between requests, plus retry with
    backoff, so we don't trip NVIDIA rate limits (which return empty summaries).
  * Guarded — normally runs only once (the 'summaries_backfilled' flag). Set
    FORCE_SUMMARY_BACKFILL=1 to run even when the flag is set. If a run can't
    finish everything, it clears the flag so the next deploy resumes the rest.

Progress is written to 'summaries_backfill_status' for the admin dashboard.
"""
import os
import json
import time

from app import app, db, Initiative, get_setting, set_setting
from utils.ai_services import generate_summary

FORCE = os.environ.get('FORCE_SUMMARY_BACKFILL', '') == '1'
DELAY = 2.0          # seconds between requests
RETRIES = 3          # attempts per initiative before giving up this run


def _summary_with_retry(title, content):
    for attempt in range(RETRIES):
        summary = generate_summary(title, content)
        if summary:
            return summary
        time.sleep(DELAY * (attempt + 1))  # backoff on empty/failed
    return ''


with app.app_context():
    if not FORCE and get_setting('summaries_backfilled', 'false') == 'true':
        print('[backfill_summaries] already completed; skipping.')
    else:
        try:
            done_ids = set(json.loads(get_setting('summaries_done_ids', '[]') or '[]'))
        except Exception:
            done_ids = set()

        all_ids = [row.id for row in Initiative.query.with_entities(Initiative.id).all()]
        total = len(all_ids)
        todo = [i for i in all_ids if i not in done_ids]
        print(f'[backfill_summaries] {len(done_ids)}/{total} already done; '
              f'processing {len(todo)} pending...')
        set_setting('summaries_backfill_status', f'running {len(done_ids)}/{total}')

        new_done = 0
        failed = 0
        for n, iid in enumerate(todo, 1):
            try:
                ini = Initiative.query.get(iid)
                if not ini:
                    done_ids.add(iid)  # gone; don't retry it forever
                    continue
                summary = _summary_with_retry(ini.title, ini.content or '')
                if summary:
                    ini.short_description = summary
                    db.session.commit()
                    done_ids.add(iid)
                    new_done += 1
                else:
                    failed += 1
            except Exception as e:
                db.session.rollback()
                failed += 1
                print(f'[backfill_summaries] error on initiative {iid}: {e}')
            if n % 5 == 0 or n == len(todo):
                set_setting('summaries_done_ids', json.dumps(sorted(done_ids)))
                set_setting('summaries_backfill_status',
                            f'running {len(done_ids)}/{total} ({failed} failed this run)')
            time.sleep(DELAY)

        set_setting('summaries_done_ids', json.dumps(sorted(done_ids)))
        remaining = total - len(done_ids)
        if remaining <= 0:
            set_setting('summaries_backfilled', 'true')
            set_setting('summaries_backfill_status', f'done {total}/{total}')
        else:
            # Not finished — clear the flag so the next deploy resumes the rest.
            set_setting('summaries_backfilled', 'false')
            set_setting('summaries_backfill_status',
                        f'partial {len(done_ids)}/{total} — {remaining} pending')
        print(f'[backfill_summaries] this run: +{new_done} new, {failed} failed. '
              f'total {len(done_ids)}/{total}.')
