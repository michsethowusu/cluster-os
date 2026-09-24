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
                 GlossaryTermInitiative, Initiative, set_setting, invalidate_glossary_cache)

SEED = os.path.join(os.path.dirname(__file__), 'data', 'glossary_seed.json')

with app.app_context():
    if MODE == 'clear':
        GlossaryTermInitiative.query.delete()
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

    # valid initiative ids (links are FK-constrained; skip any that no longer exist)
    valid_iids = {r[0] for r in db.session.query(Initiative.id).all()}

    try:
        with open(SEED, encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        set_setting('glossary_import_status', f'error: cannot read seed: {e}')
        raise SystemExit(0)

    total = len(data)
    added = existed = links_added = 0
    for i, row in enumerate(data):
        slug = row['slug']
        try:
            term = GlossaryTerm.query.filter_by(slug=slug).first()
            if not term:
                term = GlossaryTerm(
                    term=row['term'], slug=slug, definition=row.get('definition') or '',
                    occurrences=int(row.get('occurrences') or 0),
                    countries=json.dumps(row.get('countries') or [], ensure_ascii=False),
                    is_published=True,
                )
                db.session.add(term)
                db.session.flush()  # get term.id

                seen = set()
                for a in [row['term']] + list(row.get('aliases') or []):
                    a = (a or '').strip()
                    if not a or a.lower() in seen:
                        continue
                    seen.add(a.lower())
                    db.session.add(GlossaryAlias(term_id=term.id, alias=a[:200]))

                db.session.add(GlossaryRevision(term_id=term.id, definition=term.definition,
                                                source='import', note='Initial import'))
                added += 1
            else:
                existed += 1

            # initiative links (idempotent — attaches to new AND existing terms)
            have = {r[0] for r in db.session.query(GlossaryTermInitiative.initiative_id)
                    .filter(GlossaryTermInitiative.term_id == term.id).all()}
            for iid in row.get('initiative_ids') or []:
                if iid in valid_iids and iid not in have:
                    db.session.add(GlossaryTermInitiative(term_id=term.id, initiative_id=iid))
                    links_added += 1
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f'[import_glossary] error on {slug}: {e}')
        if (i + 1) % 100 == 0 or (i + 1) == total:
            set_setting('glossary_import_status',
                        f'importing {i+1}/{total} (added {added}, existing {existed}, links +{links_added})')

    invalidate_glossary_cache()
    msg = f'done: added {added}, existing {existed}, links added {links_added} of {total} terms'
    set_setting('glossary_import_status', msg)
    print(f'[import_glossary] {msg}')
