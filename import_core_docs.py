"""Env-gated bulk import of the scraped ECED/FLN core-document set into the
document library (runs on the server so it can re-download the PDFs).

For each manifest entry it re-downloads the PDF from its source URL, extracts the
text, then asks the AI to (a) decide whether the document is genuinely
ECED/FLN-focused and (b) generate its title / year / description / tags. Only
documents that pass the screen are stored; the rest are discarded. Imported
documents are published immediately.

Guarded by IMPORT_CORE_DOCS=1. Idempotent: skips any manifest entry whose file
is already present on disk. Progress/result in the 'core_docs_import_status'
setting (exposed on /backfill-status as 'core_docs_import').

Manifest: core_docs_manifest.json (title, term, url, sha256, ...), committed.
"""
import os
import time
import uuid
import json
import gc

if os.environ.get('IMPORT_CORE_DOCS', '') != '1':
    raise SystemExit(0)

import requests
import PyPDF2
from datetime import datetime

from app import (app, db, DocumentLibrary, User, Tag,
                 set_setting, _extract_document_text)
from utils.ai_services import call_nvidia_api

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, 'core_docs_manifest.json')
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; ClusterOS-DocImporter/1.0)'}
MAX_BYTES = 25 * 1024 * 1024       # skip anything larger than 25 MB (memory safety)
MAX_PAGES = 25                     # extract at most this many pages (bounds memory/CPU)
MIN_TEXT = 400                     # too little text to screen reliably
PACE_SECONDS = 1.2                 # gap between AI calls (rate-limit friendly)


def _extract_pdf_text(path, max_pages=MAX_PAGES):
    """Bounded PDF text extraction: read at most max_pages so a single huge PDF
    cannot blow up memory/CPU (the whole import runs beside the web workers)."""
    try:
        rd = PyPDF2.PdfReader(path)
        out = []
        for i, page in enumerate(rd.pages):
            if i >= max_pages:
                break
            out.append(page.extract_text() or '')
        return "\n".join(out), None
    except Exception as e:
        return "", str(e)[:200]

SCREEN_PROMPT = """You are a document librarian for the African Union ECED-FLN platform. \
ECED = Early Childhood Education and Development; FLN = Foundational Learning and Numeracy.

Decide whether the document below belongs in a professional library on early childhood \
education/development, foundational learning, numeracy, or closely related education-sector \
topics (teacher development, child protection, inclusion, education policy for young children, \
measurement of learning, etc.). African context is a plus but strong global ECED/FLN research \
also qualifies. Reject documents that are off-topic (general country/atlas material, unrelated \
economics/energy/tourism, marketing, or anything with no substantive educational content).

Return ONLY a JSON object (no markdown fences, no preamble):
{
  "relevant": true or false,
  "title": "<formal, concise title; max 200 chars>",
  "year_published": <4-digit year if found, else null>,
  "description": "<2-3 sentence plain-English summary; max 300 chars>",
  "tags": ["<3-8 specific ECED/FLN/education keywords>"]
}

If relevant is false you may leave the other fields minimal.

DOCUMENT TITLE (as scraped): __SCRAPED_TITLE__
GLOSSARY TERM IT WAS FOUND UNDER: __TERM__

DOCUMENT TEXT:
__TEXT__"""


def _status(**kw):
    set_setting('core_docs_import_status', json.dumps(kw))
    print(f'[import_core_docs] {kw}')


def _parse_json(raw):
    clean = (raw or '').strip()
    if clean.startswith('```'):
        clean = clean.split('```', 2)[1] if '```' in clean[3:] else clean
        clean = clean.replace('json', '', 1).strip('` \n')
    clean = clean.replace('```json', '').replace('```', '').strip()
    start, end = clean.find('{'), clean.rfind('}')
    if start != -1 and end != -1:
        clean = clean[start:end + 1]
    return json.loads(clean)


def run():
    # Let the web app finish booting and pass its healthcheck before we add the
    # download/extract load — a failing healthcheck is what triggered redeploys
    # that stacked multiple importers and OOM'd the box.
    time.sleep(int(os.environ.get('IMPORT_CORE_DOCS_DELAY', '75')))
    with app.app_context():
        try:
            entries = json.load(open(MANIFEST, encoding='utf-8'))
        except Exception as e:
            _status(state='error', message=f'cannot read manifest: {e}')
            return

        admin = User.query.filter_by(is_admin=True).first() or User.query.first()
        if not admin:
            _status(state='error', message='no user to attribute uploads to')
            return

        folder = os.path.join(app.config['UPLOAD_FOLDER'], 'documents')
        os.makedirs(folder, exist_ok=True)

        total = len(entries)
        added = skipped = screened_out = failed = 0
        _status(state='running', total=total, processed=0, added=0,
                screened_out=0, skipped=0, failed=0, updated_at=str(datetime.utcnow()))

        for idx, e in enumerate(entries, 1):
            fname = (e.get('filename') or '').strip()
            url = (e.get('url') or '').strip()
            if not fname or not url.startswith('http'):
                failed += 1
                continue

            # Idempotent: already imported and file present → skip.
            existing = DocumentLibrary.query.filter_by(filename=fname).first()
            if existing and existing.stored_name and \
                    os.path.exists(os.path.join(folder, existing.stored_name)):
                skipped += 1
            else:
                filepath = None
                try:
                    size = e.get('file_size_bytes') or 0
                    if size and size > MAX_BYTES:
                        failed += 1
                    else:
                        resp = requests.get(url, headers=HEADERS, allow_redirects=True,
                                            timeout=120, stream=True)
                        resp.raise_for_status()
                        content = resp.content
                        if len(content) > MAX_BYTES or not content[:5].startswith(b'%PDF'):
                            failed += 1
                        else:
                            stored = f"{uuid.uuid4().hex}.pdf"
                            filepath = os.path.join(folder, stored)
                            with open(filepath, 'wb') as f:
                                f.write(content)

                            text, _err = _extract_pdf_text(filepath)
                            text = (text or '').replace('\x00', '')  # Postgres rejects NUL
                            if not text or len(text.strip()) < MIN_TEXT:
                                os.remove(filepath); filepath = None
                                screened_out += 1
                            else:
                                prompt = (SCREEN_PROMPT
                                          .replace('__SCRAPED_TITLE__', (e.get('title') or '')[:200])
                                          .replace('__TERM__', (e.get('term') or '')[:120])
                                          .replace('__TEXT__', text[:8000]))
                                data = _parse_json(call_nvidia_api(prompt, max_tokens=700,
                                                                   temperature=0.1))
                                time.sleep(PACE_SECONDS)

                                if not data.get('relevant'):
                                    os.remove(filepath); filepath = None
                                    screened_out += 1
                                else:
                                    doc = DocumentLibrary(
                                        title=(data.get('title') or e.get('title') or fname)[:300],
                                        description=(data.get('description') or '')[:500],
                                        filename=fname, stored_name=stored,
                                        file_size=len(content), file_type='pdf',
                                        extracted_text=text or None,
                                        submitted_by=admin.id, is_published=True,
                                        processing_status='ready',
                                    )
                                    ry = data.get('year_published')
                                    try:
                                        ry = int(ry)
                                        if 1900 <= ry <= 2030:
                                            doc.year_published = ry
                                    except (TypeError, ValueError):
                                        pass
                                    db.session.add(doc)
                                    db.session.flush()
                                    for tname in (data.get('tags') or [])[:8]:
                                        tname = str(tname).strip()[:100]
                                        if not tname:
                                            continue
                                        tag = Tag.query.filter_by(name=tname).first()
                                        if not tag:
                                            tag = Tag(name=tname, is_vetted=True)
                                            db.session.add(tag); db.session.flush()
                                        if tag not in doc.tags:
                                            doc.tags.append(tag)
                                            tag.usage_count = (tag.usage_count or 0) + 1
                                    db.session.commit()
                                    added += 1
                except Exception as ex:
                    db.session.rollback()
                    if filepath and os.path.exists(filepath):
                        try: os.remove(filepath)
                        except OSError: pass
                    failed += 1
                    print(f'[import_core_docs] error on {fname}: {str(ex)[:200]}')
                finally:
                    # Release the downloaded bytes/text each iteration so memory
                    # stays flat across the full run (it shares the box with gunicorn).
                    try: del content
                    except NameError: pass
                    try: del text
                    except NameError: pass
                    try: del data
                    except NameError: pass
                    gc.collect()

            if idx % 10 == 0 or idx == total:
                _status(state='running', total=total, processed=idx, added=added,
                        screened_out=screened_out, skipped=skipped, failed=failed,
                        updated_at=str(datetime.utcnow()))

        _status(state='done', total=total, processed=total, added=added,
                screened_out=screened_out, skipped=skipped, failed=failed,
                updated_at=str(datetime.utcnow()))


if __name__ == '__main__':
    run()
