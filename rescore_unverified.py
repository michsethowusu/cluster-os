"""Env-gated re-scoring of initiatives that were auto-published without a real AI
score during an outage (quality_score IS NULL while is_published = true).

Modes (env RESCORE_UNVERIFIED):
  report : re-score each unverified published initiative with the now-working AI,
           RECORD the real score, and report how many now score below 3. Does NOT
           unpublish anything — a dry run to see the impact.
  apply  : unpublish any published initiative scoring 1-2 (sends it to the admin
           approval queue). Run after a 'report' pass.

Paced (2s) + uses the scorer's built-in 429 retry. Progress in 'rescore_status'.
"""
import os
import time

MODE = os.environ.get('RESCORE_UNVERIFIED', '')
if MODE not in ('report', 'apply'):
    raise SystemExit(0)

from sqlalchemy import text
from app import app, db, Initiative, get_setting, set_setting, record_ai_scoring_result
from utils.ai_services import score_initiative_quality

with app.app_context():
    if MODE == 'apply':
        r = db.session.execute(text(
            "UPDATE initiative SET is_published = false "
            "WHERE is_published = true AND quality_score IS NOT NULL AND quality_score < 3"))
        db.session.commit()
        msg = f'apply done: unpublished {r.rowcount or 0} weak (1-2) initiative(s) to the admin queue'
        set_setting('rescore_status', msg)
        print(f'[rescore_unverified] {msg}')
    else:  # report
        ids = [row.id for row in Initiative.query.filter(
            Initiative.is_published == True,          # noqa: E712
            Initiative.quality_score.is_(None)).with_entities(Initiative.id).all()]
        total = len(ids)
        print(f'[rescore_unverified] report: re-scoring {total} unverified published initiative(s)...')
        set_setting('rescore_status', f'report running 0/{total}')
        scored = weak = failed = 0
        for n, iid in enumerate(ids, 1):
            ini = Initiative.query.get(iid)
            if not ini:
                continue
            s = score_initiative_quality(ini.title, ini.content or '', ini.short_description or '')
            record_ai_scoring_result(s is not None)
            if s is None:
                failed += 1
            else:
                ini.quality_score = s      # record the real score (does NOT unpublish)
                db.session.commit()
                scored += 1
                if s < 3:
                    weak += 1
            if n % 5 == 0 or n == total:
                set_setting('rescore_status',
                            f'report {n}/{total}: weak(<3)={weak}, unscorable={failed}')
            time.sleep(2)
        msg = (f'report done: re-scored {scored}/{total} — {weak} now score 1-2 '
               f'(would be pulled on apply), {failed} still unscorable')
        set_setting('rescore_status', msg)
        print(f'[rescore_unverified] {msg}')
