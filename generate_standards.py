"""Build the ECED/FLN Standards directory from the platform's published initiatives.

Pipeline (all in memory first, so the live directory is untouched until load):
extract candidate standards per published 4-5 initiative, cluster titles, merge,
compose a clean statement/rationale/category per canonical standard, link the
glossary terms each references and the source initiatives, back up the current
directory, then replace it.

Triggered as a detached subprocess:
  STANDARDS_GENERATE=1       full rebuild
  STANDARDS_RESTORE=<id>     restore a StandardBackup snapshot

Provider: Gemini if GEMINI_API_KEY is set, else the platform default.
Progress JSON in the 'standards_gen_status' setting.
"""
import os
import re
import json
import time
from datetime import datetime

import requests
from app import (app, db, Initiative, GlossaryTerm, GlossaryAlias,
                 Standard, StandardSource, StandardTerm, StandardComment,
                 StandardRevision, StandardBackup, set_setting, invalidate_standards_cache)
from utils.ai_services import call_nvidia_api, STANDARD_CATEGORIES

GEMINI_KEY = os.environ.get('GEMINI_API_KEY', '').strip()
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-3.5-flash-lite')
GEMINI_URL = ('https://generativelanguage.googleapis.com/v1beta/models/'
              f'{GEMINI_MODEL}:generateContent?key=' + GEMINI_KEY)
PROVIDER = 'gemini' if GEMINI_KEY else 'nvidia'


def now():
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')


def set_status(**fields):
    try:
        cur = json.loads(set_status.cache or '{}')
    except Exception:
        cur = {}
    cur.update(fields)
    cur['updated_at'] = now()
    set_status.cache = json.dumps(cur)
    set_setting('standards_gen_status', set_status.cache)


set_status.cache = ''


def ai(prompt, max_tokens=1500, temperature=0.2):
    last = None
    for attempt in range(7):
        try:
            if PROVIDER == 'gemini':
                r = requests.post(GEMINI_URL, json={
                    'contents': [{'parts': [{'text': prompt}]}],
                    'generationConfig': {'temperature': temperature, 'maxOutputTokens': max_tokens},
                }, timeout=120)
                if r.status_code == 429:
                    # rate limited — wait for the per-minute window to reset
                    last = 'HTTP 429'; time.sleep(min(70, 15 * (attempt + 1))); continue
                if r.status_code in (500, 502, 503, 504):
                    last = f'HTTP {r.status_code}'; time.sleep(min(2 ** attempt, 10)); continue
                r.raise_for_status()
                return r.json()['candidates'][0]['content']['parts'][0]['text']
            return call_nvidia_api(prompt, max_tokens=max_tokens, temperature=temperature)
        except Exception as e:
            last = str(e); time.sleep(min(2 ** attempt, 10))
    raise RuntimeError(f'AI call failed: {last}')


def parse_jsonl(text):
    out = []
    t = re.sub(r'```(?:json)?|```', '', text or '', flags=re.I)
    for line in t.splitlines():
        line = line.strip().rstrip(',')
        if line.startswith('{'):
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def strip_markup(s):
    if not s:
        return ''
    s = re.sub(r'<[^>]+>', ' ', s)
    s = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', s)
    s = re.sub(r'[`*_#>]+', ' ', s)
    s = s.replace('&amp;', '&').replace('&nbsp;', ' ')
    return re.sub(r'\s+', ' ', s).strip()


def slugify(s, taken):
    base = re.sub(r'[^\w]+', '-', (s or '').lower()).strip('-')[:200] or 'standard'
    slug, n = base, 1
    while slug in taken:
        slug = f'{base}-{n}'; n += 1
    taken.add(slug)
    return slug


def _termmap():
    rows = db.session.query(GlossaryAlias.alias, GlossaryAlias.term_id).join(
        GlossaryTerm, GlossaryAlias.term_id == GlossaryTerm.id).filter(
        GlossaryTerm.is_published == True).all()  # noqa: E712
    pairs = {}
    for alias, tid in rows:
        a = (alias or '').strip().lower()
        if len(a) >= 3:
            pairs.setdefault(a, tid)
    return sorted(pairs.items(), key=lambda kv: len(kv[0]), reverse=True)


def _terms_in(text, termpairs):
    low = (text or '').lower()
    found = set()
    for alias, tid in termpairs:
        if re.search(r'(?<![\w-])' + re.escape(alias) + r'(?![\w-])', low):
            found.add(tid)
    return found


# ---------------- snapshot / replace ----------------
def _snapshot_current(label):
    stds = []
    for s in Standard.query.all():
        stds.append({
            'title': s.title, 'slug': s.slug, 'statement': s.statement,
            'rationale': s.rationale, 'category': s.category, 'is_published': s.is_published,
            'initiative_ids': [r.initiative_id for r in StandardSource.query.filter_by(standard_id=s.id).all()],
            'term_ids': [r.term_id for r in StandardTerm.query.filter_by(standard_id=s.id).all()],
            'revisions': [{'statement': r.statement, 'rationale': r.rationale, 'source': r.source,
                           'note': r.note} for r in
                          s.revisions.order_by(StandardRevision.created_at.asc()).all()],
        })
    comments = [{'slug': c.standard.slug, 'user_id': c.user_id, 'content': c.content, 'status': c.status}
                for c in StandardComment.query.all()]
    bk = StandardBackup(label=label, standard_count=len(stds),
                        data=json.dumps({'standards': stds, 'comments': comments}, ensure_ascii=False))
    db.session.add(bk); db.session.commit()
    return bk.id


def _replace_standards(stds, source, restore_comments=None):
    valid_iids = {r[0] for r in db.session.query(Initiative.id).all()}
    valid_tids = {r[0] for r in db.session.query(GlossaryTerm.id).all()}
    StandardSource.query.delete()
    StandardTerm.query.delete()
    StandardRevision.query.delete()
    StandardComment.query.delete()
    Standard.query.delete()
    db.session.commit()

    taken = set()
    slug_by = {}
    for sd in stds:
        slug = sd.get('slug') or slugify(sd['title'], taken)
        if slug in taken:
            slug = slugify(sd['title'], taken)
        taken.add(slug)
        s = Standard(title=sd['title'][:300], slug=slug, statement=sd.get('statement') or '',
                     rationale=sd.get('rationale') or '', category=sd.get('category') or None,
                     is_published=sd.get('is_published', True))
        db.session.add(s); db.session.flush()
        slug_by[slug] = s.id
        for iid in sd.get('initiative_ids') or []:
            if iid in valid_iids:
                db.session.add(StandardSource(standard_id=s.id, initiative_id=iid))
        for tid in sd.get('term_ids') or []:
            if tid in valid_tids:
                db.session.add(StandardTerm(standard_id=s.id, term_id=tid))
        revs = sd.get('revisions')
        if revs:
            for r in revs:
                db.session.add(StandardRevision(standard_id=s.id, statement=r.get('statement'),
                                                rationale=r.get('rationale'),
                                                source=r.get('source') or source, note=r.get('note')))
        else:
            db.session.add(StandardRevision(standard_id=s.id, statement=s.statement,
                                            rationale=s.rationale, source=source, note='Generated'))
        db.session.commit()

    if restore_comments:
        for c in restore_comments:
            sid = slug_by.get(c.get('slug'))
            if sid:
                db.session.add(StandardComment(standard_id=sid, user_id=c['user_id'],
                                               content=c['content'], status=c.get('status', 'pending')))
        db.session.commit()
    invalidate_standards_cache()


def do_restore(backup_id):
    with app.app_context():
        bk = StandardBackup.query.get(int(backup_id))
        if not bk:
            set_status(state='error', message=f'Backup {backup_id} not found', finished_at=now())
            return
        set_status(state='running', phase='restore', message='Backing up current before restore…',
                   started_at=now())
        _snapshot_current(label=f'auto-before-restore-{backup_id}')
        data = json.loads(bk.data)
        _replace_standards(data['standards'], source='restore', restore_comments=data.get('comments'))
        set_status(state='done', phase='restore', finished_at=now(),
                   message=f"Restored backup #{backup_id} ({len(data['standards'])} standards)",
                   result={'standards': len(data['standards'])})


# ---------------- prompts ----------------
CATS = ", ".join(STANDARD_CATEGORIES)

EXTRACT = """You are deriving STANDARDS for Early Childhood Education & Development (ECED) and \
Foundational Learning & Numeracy (FLN) in Africa, grounded in the initiative below. A standard is a \
concise NORMATIVE best-practice statement (what good provision should ensure), generalisable beyond \
this one initiative — do NOT name the organisation, project or country.
Suggest 1-6 standards this initiative demonstrates or implies. For each: title (<=12 words), statement \
(one 'should/must' sentence), rationale (one short sentence), category (exactly one of [%s]).
Return JSONL, one object per line, no code fences: {"title":"...","statement":"...","rationale":"...","category":"..."}

Initiative title: %s
Initiative text:
%s""" % ('%s', '%s', '%s')

CLUSTER = """These are candidate ECED/FLN standard titles. For EACH, output the canonical standard title \
it belongs to (merge ones that express the same norm; keep distinct norms separate). Use a clear \
normative Title Case label.
Return JSONL one per line: {"i": <index>, "canonical": "<title>"}. Include every index.

Titles (index: title):
%s"""

MERGE = """Consolidate these canonical ECED/FLN standard titles: group any expressing the SAME norm; \
keep distinct ones separate. Choose one clear final title per group.
Return JSONL one per line: {"final":"<title>","members":["<title>",...]}. Every title appears once.

Titles:
%s"""

COMPOSE = """You are finalising ECED/FLN standards for Africa. For EACH item below, using its member \
candidate statements, write ONE clean normative statement (should/must ...), a one-sentence rationale, \
and pick a category (exactly one of [%s]). General and reusable; no org/country names.
Return JSONL one per line: {"i": <index>, "statement":"...","rationale":"...","category":"..."}

Items:
%s""" % ('%s', '%s')


def do_generate():
    with app.app_context():
        set_status(state='running', phase='extract', message='Starting…', started_at=now(),
                   processed=0, total=0, provider=PROVIDER, result=None, finished_at=None)
        inits = (Initiative.query
                 .filter(Initiative.is_published == True)  # noqa: E712
                 .filter(Initiative.quality_score != None)  # noqa: E711
                 .filter(Initiative.quality_score >= 4)
                 .order_by(Initiative.id.asc()).all())
        total = len(inits)
        if not total:
            set_status(state='error', message='No published initiatives scored 4-5 to build from.',
                       finished_at=now())
            return

        # ---- extract candidates ----
        cands = []   # {title, statement, rationale, category, iid}
        valid_cats = set(STANDARD_CATEGORIES)
        failed = 0
        for idx, i in enumerate(inits):
            text = strip_markup(f"{i.short_description or ''}. {i.content or ''}")
            try:
                raw = ai(EXTRACT % (CATS, i.title, text[:12000]), max_tokens=1500, temperature=0.3)
            except Exception as e:
                # Skip this initiative rather than aborting the whole run; only give
                # up if a large share fail (likely a hard quota/key problem).
                failed += 1
                if failed > 8 and failed > (idx + 1) * 0.5:
                    set_status(state='error', phase='extract',
                               message=f'Too many AI failures ({failed}); last: {e}', finished_at=now())
                    return
                time.sleep(5)
                continue
            for o in parse_jsonl(raw):
                st = (o.get('statement') or '').strip()
                if not st:
                    continue
                cat = (o.get('category') or '').strip()
                cands.append({
                    'title': (o.get('title') or st[:80]).strip()[:300],
                    'statement': st, 'rationale': (o.get('rationale') or '').strip(),
                    'category': cat if cat in valid_cats else 'Other', 'iid': i.id,
                })
            if (idx + 1) % 3 == 0 or idx + 1 == total:
                set_status(phase='extract', processed=idx + 1, total=total,
                           message=f'Extracting standards… {idx+1}/{total} initiatives, '
                                   f'{len(cands)} candidates ({failed} skipped)')
            time.sleep(2.0)   # pace to stay under the API's per-minute rate limit

        if not cands:
            set_status(state='error', message='No candidate standards produced.', finished_at=now())
            return

        # ---- cluster candidate titles -> canonical ----
        cand_to_canon = {}
        BATCH = 80
        nb = (len(cands) + BATCH - 1) // BATCH
        for b in range(nb):
            chunk = cands[b * BATCH:(b + 1) * BATCH]
            listing = "\n".join(f"{j}: {c['title']}" for j, c in enumerate(chunk))
            got = {}
            try:
                for o in parse_jsonl(ai(CLUSTER % listing, max_tokens=4096)):
                    if 'i' in o and o.get('canonical'):
                        try:
                            got[int(o['i'])] = str(o['canonical']).strip()
                        except Exception:
                            pass
            except Exception as e:
                set_status(state='error', phase='cluster', message=f'AI clustering failed: {e}',
                           finished_at=now())
                return
            for j, c in enumerate(chunk):
                cand_to_canon[b * BATCH + j] = got.get(j, c['title'])
            set_status(phase='cluster', processed=b + 1, total=nb,
                       message=f'Clustering candidates… batch {b+1}/{nb}')
            time.sleep(1.5)

        # ---- merge canonical titles ----
        distinct = sorted(set(cand_to_canon.values()))
        canon_to_final = {}
        try:
            for g in parse_jsonl(ai(MERGE % json.dumps(distinct, ensure_ascii=False), max_tokens=8192)):
                final = str(g.get('final', '')).strip()
                for m in g.get('members', []):
                    if final:
                        canon_to_final[str(m).strip()] = final
        except Exception:
            pass
        for c in distinct:
            canon_to_final.setdefault(c, c)

        # ---- aggregate per final standard ----
        finals = {}   # final_title -> {statements:[], rationales:[], cats:{}, iids:set}
        for k, cand in enumerate(cands):
            fin = canon_to_final.get(cand_to_canon.get(k, cand['title']), cand['title'])
            f = finals.setdefault(fin, {'statements': [], 'rationales': [], 'cats': {}, 'iids': set()})
            f['statements'].append(cand['statement'])
            if cand['rationale']:
                f['rationales'].append(cand['rationale'])
            f['cats'][cand['category']] = f['cats'].get(cand['category'], 0) + 1
            f['iids'].add(cand['iid'])

        final_titles = sorted(finals.keys(), key=lambda x: x.lower())

        # ---- compose clean statement/rationale/category ----
        composed = {}
        CB = 8
        nb2 = (len(final_titles) + CB - 1) // CB
        for b in range(nb2):
            chunk = final_titles[b * CB:(b + 1) * CB]
            blocks = []
            for j, t in enumerate(chunk):
                f = finals[t]
                mem = " | ".join(f['statements'][:6])
                blocks.append(f"{j}: TITLE: {t}\n   MEMBERS: {mem[:900]}")
            got = {}
            try:
                for o in parse_jsonl(ai(COMPOSE % (CATS, "\n".join(blocks)), max_tokens=3072)):
                    if 'i' in o and o.get('statement'):
                        try:
                            got[int(o['i'])] = o
                        except Exception:
                            pass
            except Exception:
                pass
            for j, t in enumerate(chunk):
                composed[t] = got.get(j)
            set_status(phase='compose', processed=b + 1, total=nb2,
                       message=f'Composing standards… batch {b+1}/{nb2}')
            time.sleep(1.5)

        # ---- link glossary terms + build payload ----
        set_status(phase='link', message='Linking glossary terms…')
        termpairs = _termmap()
        taken = set()
        payload = []
        for t in final_titles:
            f = finals[t]
            comp = composed.get(t) or {}
            statement = (comp.get('statement') or (f['statements'][0] if f['statements'] else '')).strip()
            rationale = (comp.get('rationale') or (f['rationales'][0] if f['rationales'] else '')).strip()
            category = (comp.get('category') or '').strip()
            if category not in set(STANDARD_CATEGORIES):
                category = max(f['cats'].items(), key=lambda kv: kv[1])[0] if f['cats'] else 'Other'
            tids = _terms_in(f"{t}. {statement}. {rationale}", termpairs)
            payload.append({
                'title': t, 'slug': slugify(t, taken), 'statement': statement,
                'rationale': rationale, 'category': category,
                'is_published': False,   # pending peer review until approved
                'initiative_ids': sorted(f['iids']), 'term_ids': sorted(tids),
            })

        # ---- backup + load ----
        set_status(phase='backup', message='Backing up current directory…')
        backup_id = _snapshot_current(label=f'auto-before-regenerate {now()}')
        set_status(phase='load', message='Publishing the new directory…')
        _replace_standards(payload, source='generated')

        set_status(state='done', phase='load', finished_at=now(),
                   message=f'Done — {len(payload)} standards from {total} initiatives (backup #{backup_id}).',
                   result={'standards': len(payload), 'initiatives': total, 'backup_id': backup_id})


if __name__ == '__main__':
    GEN = os.environ.get('STANDARDS_GENERATE', '')
    RESTORE = os.environ.get('STANDARDS_RESTORE', '')
    if RESTORE:
        do_restore(RESTORE)
    elif GEN == '1':
        try:
            do_generate()
        except Exception as e:
            with app.app_context():
                set_status(state='error', message=f'Unexpected error: {e}', finished_at=now())
