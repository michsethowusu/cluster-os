"""Env-gated one-off: add a DocumentSendQueue entry for every published document
that doesn't already have one (bulk-imported docs were published directly and so
skipped the admin Approve step, which is what normally enqueues them).

Guarded by BACKFILL_DOC_QUEUE=1. Result in 'backfill_doc_queue_status'.
"""
import os

if os.environ.get('BACKFILL_DOC_QUEUE', '') != '1':
    raise SystemExit(0)

from app import app, db, DocumentLibrary, DocumentSendQueue, set_setting

with app.app_context():
    queued_ids = {q.document_id for q in DocumentSendQueue.query.all()}
    pubs = DocumentLibrary.query.filter_by(is_published=True).all()
    added = 0
    for doc in pubs:
        if doc.id in queued_ids:
            continue
        db.session.add(DocumentSendQueue(document_id=doc.id))
        added += 1
        if added % 100 == 0:
            db.session.commit()
    db.session.commit()
    msg = (f"done: added {added} to send queue "
           f"(published={len(pubs)}, already queued={len(queued_ids)})")
    set_setting('backfill_doc_queue_status', msg)
    print(f"[backfill_document_queue] {msg}")
