"""Env-gated one-off import of the AU 'Teacher Frameworks' documents into the
policy document library (files live on the server's disk, so this must run on
the server).

Source: https://education-au.org/resources/category/61-teacher-frameworks
Guarded by IMPORT_TEACHER_DOCS=1. Idempotent: skips any document whose title is
already present. Progress/result in the 'teacher_docs_import_status' setting.
"""
import os
import uuid

if os.environ.get('IMPORT_TEACHER_DOCS', '') != '1':
    raise SystemExit(0)

import requests
from app import app, db, DocumentLibrary, User, set_setting, _extract_document_text

DOCS = [
    ("Continental Teacher Qualification Framework (English)",
     "https://education-au.org/resources/send/61-teacher-frameworks/83-continental-teacher-qualification-framework-en"),
    ("Continental Teacher Qualification Framework (French)",
     "https://education-au.org/resources/send/61-teacher-frameworks/84-continental-teacher-qualification-framework-fr"),
    ("Continental Framework of Standards and Competences for the Teaching Profession (English)",
     "https://education-au.org/resources/send/61-teacher-frameworks/81-continental-framework-of-standards-and-competences-en"),
    ("Continental Framework of Standards and Competences for the Teaching Profession (French)",
     "https://education-au.org/resources/send/61-teacher-frameworks/82-continental-framework-of-standards-and-competences-fr"),
    ("Continental Guidelines for the Teaching Profession in Africa (English)",
     "https://education-au.org/resources/send/61-teacher-frameworks/79-continental-guidelines-for-the-teaching-profession-en"),
    ("Continental Guidelines for the Teaching Profession in Africa (French)",
     "https://education-au.org/resources/send/61-teacher-frameworks/80-continental-guidelines-for-the-teaching-profession-fr"),
]
YEAR = 2020
DESCRIPTION = ("African Union continental teacher framework, published by the AU "
               "Continental Education Strategy for Africa (CESA). Source: education-au.org.")
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; ClusterOS-DocImporter/1.0)'}

with app.app_context():
    admin = User.query.filter_by(is_admin=True).first() or User.query.first()
    if not admin:
        set_setting('teacher_docs_import_status', 'error: no user to attribute uploads to')
        raise SystemExit(0)

    folder = os.path.join(app.config['UPLOAD_FOLDER'], 'documents')
    os.makedirs(folder, exist_ok=True)

    added = skipped = restored = failed = 0
    for title, url in DOCS:
        try:
            existing = DocumentLibrary.query.filter_by(title=title).first()
            # Skip only if the row exists AND its file is actually on disk; otherwise
            # (re)download — this restores files lost before a persistent volume existed.
            if existing and existing.stored_name and \
                    os.path.exists(os.path.join(folder, existing.stored_name)):
                skipped += 1
                continue
            resp = requests.get(url, headers=HEADERS, allow_redirects=True, timeout=60)
            resp.raise_for_status()
            content = resp.content
            if not content[:5].startswith(b'%PDF'):
                # not a PDF (e.g. an HTML error/interstitial) — skip rather than store junk
                failed += 1
                print(f'[import_teacher_docs] not a PDF, skipping: {title}')
                continue

            stored = f"{uuid.uuid4().hex}.pdf"
            filepath = os.path.join(folder, stored)
            with open(filepath, 'wb') as f:
                f.write(content)

            extracted, _err = _extract_document_text(filepath, 'pdf')
            filename = title.replace(' ', '_').replace('(', '').replace(')', '') + '.pdf'

            if existing:
                existing.stored_name = stored
                existing.filename = filename
                existing.file_size = len(content)
                existing.file_type = 'pdf'
                existing.extracted_text = extracted or None
                existing.is_published = True
                existing.processing_status = 'ready'
                db.session.commit()
                restored += 1
                print(f'[import_teacher_docs] restored file: {title} ({len(content)} bytes)')
            else:
                doc = DocumentLibrary(
                    title=title, description=DESCRIPTION, year_published=YEAR,
                    filename=filename, stored_name=stored, file_size=len(content),
                    file_type='pdf', extracted_text=extracted or None,
                    submitted_by=admin.id, is_published=True, processing_status='ready',
                )
                db.session.add(doc)
                db.session.commit()
                added += 1
                print(f'[import_teacher_docs] added: {title} ({len(content)} bytes)')
        except Exception as e:
            db.session.rollback()
            failed += 1
            print(f'[import_teacher_docs] error on {title}: {e}')

    msg = f'done: added {added}, restored {restored}, skipped(present) {skipped}, failed {failed} of {len(DOCS)}'
    set_setting('teacher_docs_import_status', msg)
    print(f'[import_teacher_docs] {msg}')
