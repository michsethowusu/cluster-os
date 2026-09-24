"""Env-gated import of the ECED-FLN glossary into the database.

Reads data/glossary_seed.json (canonical term, slug, definition, aliases,
countries) and creates GlossaryTerm + GlossaryAlias rows, plus an initial
'import' revision per term. Idempotent: skips terms whose slug already exists.

Guards:
  IMPORT_GLOSSARY=1       import (skips existing)
  IMPORT_GLOSSARY=clear   delete all glossary rows (for a clean re-import)

Progress in the 'glossary_import_status' setting (see /backfill-status).
"""
import os
import json

MODE = os.environ.get('IMPORT_GLOSSARY', '')
if MODE not in ('1', 'clear'):
    raise SystemExit(0)

from app import (app, db, GlossaryTerm, GlossaryAlias, GlossaryRevision,
                 set_setting, invalidate_glossary_cache)

SEED = os.path.join(os.path.dirname(__file__), 'data', 'glossary_seed.json')

with app.app_context():
    if MODE == 'clear':
        GlossaryRevision.query.delete()
        GlossaryAlias.query.delete()
        from app import GlossaryComment
        GlossaryComment.query.delete()
        GlossaryTerm.query.delete()
        db.session.commit()
        invalidate_glossary_cache()
        set_setting('glossary_import_status', 'cleared all glossary rows')
        print('[import_glossary] cleared')
        raise SystemExit(0)

    try:
        with open(SEED, encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        set_setting('glossary_import_status', f'error: cannot read seed: {e}')
        raise SystemExit(0)

    total = len(data)
    added = skipped = 0
    for i, row in enumerate(data):
        slug = row['slug']
        try:
            if GlossaryTerm.query.filter_by(slug=slug).first():
                skipped += 1
                continue
            term = GlossaryTerm(
                term=row['term'], slug=slug, definition=row.get('definition') or '',
                occurrences=int(row.get('occurrences') or 0),
                countries=json.dumps(row.get('countries') or [], ensure_ascii=False),
                is_published=True,
            )
            db.session.add(term)
            db.session.flush()  # get term.id

            # aliases: the canonical term itself + all recorded aliases (deduped, case-insensitive)
            seen = set()
            for a in [row['term']] + list(row.get('aliases') or []):
                a = (a or '').strip()
                if not a or a.lower() in seen:
                    continue
                seen.add(a.lower())
                db.session.add(GlossaryAlias(term_id=term.id, alias=a[:200]))

            db.session.add(GlossaryRevision(term_id=term.id, definition=term.definition,
                                            source='import', note='Initial import'))
            db.session.commit()
            added += 1
        except Exception as e:
            db.session.rollback()
            print(f'[import_glossary] error on {slug}: {e}')
        if (i + 1) % 100 == 0 or (i + 1) == total:
            set_setting('glossary_import_status',
                        f'importing {i+1}/{total} (added {added}, skipped {skipped})')

    invalidate_glossary_cache()
    msg = f'done: added {added}, skipped(existing) {skipped} of {total}'
    set_setting('glossary_import_status', msg)
    print(f'[import_glossary] {msg}')
