"""One-off: move all standards into the peer-review queue (unpublish + clear code).

Guarded by UNPUBLISH_STANDARDS=1. Run once when enabling review gating so the
existing directory is reviewed before anything is public again.
"""
import os

if os.environ.get('UNPUBLISH_STANDARDS', '') != '1':
    raise SystemExit(0)

from app import app, db, Standard, set_setting, invalidate_standards_cache

with app.app_context():
    n = Standard.query.filter_by(is_published=True).update(
        {Standard.is_published: False, Standard.code: None}, synchronize_session=False)
    db.session.commit()
    invalidate_standards_cache()
    set_setting('unpublish_standards_status', f'done: moved {n} standard(s) into review')
    print(f'[unpublish_standards] moved {n} standard(s) into review')
