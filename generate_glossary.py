"""Regenerate the glossary from the platform's own initiatives, end to end.

Pipeline (all in memory first, so the live glossary is untouched until the final
load step): extract terminology from published initiatives scored 4-5, cluster
into canonical terms with aliases, write grounded definitions, back up the
current glossary, then replace it. Progress is written to the
'glossary_gen_status' setting (JSON) and shown on the admin page.

Triggered as a detached subprocess by the admin "Regenerate" button:
  GLOSSARY_GENERATE=1        run the full pipeline
  GLOSSARY_RESTORE=<id>      restore a GlossaryBackup snapshot instead

AI provider: Gemini if GEMINI_API_KEY is set, otherwise the platform's NVIDIA NIM.
"""
import os
import re
import json
import time
from datetime import datetime

import requests
from app import (app, db, Initiative, GlossaryTerm, GlossaryAlias, GlossaryRevision,
                 GlossaryComment, GlossaryTermInitiative, GlossaryBackup,
                 set_setting, invalidate_glossary_cache)
from utils.ai_services import call_nvidia_api

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
    set_setting('glossary_gen_status', set_status.cache)


set_status.cache = ''


def ai(prompt, max_tokens=1024, temperature=0.0):
    """One completion via the configured provider, with light retry."""
    last = None
    for attempt in range(4):
        try:
            if PROVIDER == 'gemini':
                r = requests.post(GEMINI_URL, json={
                    'contents': [{'parts': [{'text': prompt}]}],
                    'generationConfig': {'temperature': temperature, 'maxOutputTokens': max_tokens},
                }, timeout=120)
                if r.status_code in (429, 500, 502, 503, 504):
                    last = f'HTTP {r.status_code}'; time.sleep(min(2 ** attempt, 8)); continue
                r.raise_for_status()
                return r.json()['candidates'][0]['content']['parts'][0]['text']
            return call_nvidia_api(prompt, max_tokens=max_tokens, temperature=temperature)
        except Exception as e:
            last = str(e); time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f'AI call failed: {last}')


def parse_jsonl(text):
    t = re.sub(r'```(?:json)?|```', '', text, flags=re.I)
    out = []
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
    s = re.sub(r'<(br|/p|/li|/h[1-6]|/div|/tr)\s*/?>', '. ', s, flags=re.I)
    s = re.sub(r'<[^>]+>', ' ', s)
    s = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', s)
    s = re.sub(r'[`*_#>]+', ' ', s)
    s = s.replace('&amp;', '&').replace('&nbsp;', ' ')
    return re.sub(r'\s+', ' ', s).strip()


def sentences_of(text):
    return [p.strip() for p in re.split(r'(?<=[.!?])\s+|\n+', text) if len(p.strip()) >= 15]


def slugify(s, taken):
    base = re.sub(r'[^\w]+', '-', s.lower()).strip('-')[:190] or 'term'
    slug, n = base, 1
    while slug in taken:
        slug = f'{base}-{n}'; n += 1
    taken.add(slug)
    return slug


# ---------------- restore ----------------
def do_restore(backup_id):
    with app.app_context():
        bk = GlossaryBackup.query.get(int(backup_id))
        if not bk:
            set_status(state='error', message=f'Backup {backup_id} not found', finished_at=now())
            return
        set_status(state='running', phase='restore', message='Backing up current before restore…',
                   started_at=now(), processed=0, total=0)
        _snapshot_current(label=f'auto-before-restore-{backup_id}')
        data = json.loads(bk.data)
        _replace_glossary(data['terms'], source='restore', restore_comments=data.get('comments'))
        set_status(state='done', phase='restore',
                   message=f"Restored backup #{backup_id} ({len(data['terms'])} terms)",
                   finished_at=now(), result={'terms': len(data['terms'])})


def _snapshot_current(label):
    terms = []
    for t in GlossaryTerm.query.all():
        terms.append({
            'term': t.term, 'slug': t.slug, 'definition': t.definition,
            'occurrences': t.occurrences, 'countries': t.country_list(),
            'is_published': t.is_published,
            'aliases': [a.alias for a in t.aliases],
            'initiative_ids': [l.initiative_id for l in
                               GlossaryTermInitiative.query.filter_by(term_id=t.id).all()],
            'revisions': [{'definition': r.definition, 'source': r.source,
                           'note': r.note, 'created_at': r.created_at.isoformat() if r.created_at else None}
                          for r in t.revisions.order_by(GlossaryRevision.created_at.asc()).all()],
        })
    comments = [{'term_slug': c.term.slug, 'user_id': c.user_id, 'content': c.content,
                 'status': c.status} for c in GlossaryComment.query.all()]
    bk = GlossaryBackup(label=label, term_count=len(terms),
                        data=json.dumps({'terms': terms, 'comments': comments}, ensure_ascii=False))
    db.session.add(bk)
    db.session.commit()
    return bk.id


def _replace_glossary(terms, source, restore_comments=None):
    """Clear the glossary and insert the given term dicts. Runs in one commit block."""
    valid_iids = {r[0] for r in db.session.query(Initiative.id).all()}
    GlossaryTermInitiative.query.delete()
    GlossaryRevision.query.delete()
    GlossaryComment.query.delete()
    GlossaryAlias.query.delete()
    GlossaryTerm.query.delete()
    db.session.commit()

    taken = set()
    slug_by = {}
    for td in terms:
        slug = td.get('slug') or slugify(td['term'], taken)
        if slug in taken:
            slug = slugify(td['term'], taken)
        taken.add(slug)
        term = GlossaryTerm(term=td['term'], slug=slug, definition=td.get('definition') or '',
                            occurrences=int(td.get('occurrences') or 0),
                            countries=json.dumps(td.get('countries') or [], ensure_ascii=False),
                            is_published=td.get('is_published', True))
        db.session.add(term)
        db.session.flush()
        slug_by[slug] = term.id
        seen = set()
        for a in [td['term']] + list(td.get('aliases') or []):
            a = (a or '').strip()
            if a and a.lower() not in seen:
                seen.add(a.lower())
                db.session.add(GlossaryAlias(term_id=term.id, alias=a[:200]))
        revs = td.get('revisions')
        if revs:
            for r in revs:
                db.session.add(GlossaryRevision(term_id=term.id, definition=r.get('definition'),
                                                source=r.get('source') or source, note=r.get('note')))
        else:
            db.session.add(GlossaryRevision(term_id=term.id, definition=term.definition,
                                            source=source, note='Generated'))
        for iid in td.get('initiative_ids') or []:
            if iid in valid_iids:
                db.session.add(GlossaryTermInitiative(term_id=term.id, initiative_id=iid))
        db.session.commit()

    if restore_comments:
        for c in restore_comments:
            tid = slug_by.get(c.get('term_slug'))
            if tid:
                db.session.add(GlossaryComment(term_id=tid, user_id=c['user_id'],
                                               content=c['content'], status=c.get('status', 'pending')))
        db.session.commit()
    invalidate_glossary_cache()


# ---------------- generate ----------------
EXTRACT_PROMPT = """Extract the Early Childhood Education & Development (ECED) and Foundational Learning \
& Numeracy (FLN) terminology that ACTUALLY appears in the text below: technical terms, concepts, \
programmes, pedagogical approaches and sector jargon. Ignore generic words, person/place/organisation \
names. Return each term on its OWN LINE in its natural canonical form, nothing else (no numbering, no bullets).

Text:
%s"""

CLUSTER_PROMPT = """You are building an ECED-FLN glossary. For EACH input term, output the canonical \
concept it belongs to. Merge spelling/case/hyphen/plural variants, abbreviation<->expansion, and clear \
synonyms into the SAME canonical string; keep distinct concepts separate. Canonical form: the full \
expanded phrase in Title Case with the abbreviation in parentheses when one exists (e.g. \
"Early Childhood Development (ECD)"), else a clean Title Case phrase.
Return JSONL, one object per line: {"term": <input term verbatim>, "canonical": <label>}. \
Include every input term. No code fences.

Input terms:
%s"""

MERGE_PROMPT = """Consolidate these ECED-FLN canonical labels: group any that mean the same concept \
(true synonyms / abbreviation pairs only, not merely related). For each group pick one final label in \
"Expanded Form (ABBR)" form when an abbreviation exists, else Title Case.
Return JSONL, one per line: {"final": <label>, "members": [<labels from input>]}. Every label appears once.

Labels:
%s"""

DEFINE_PROMPT = """You are writing an ECED-FLN glossary. For EACH term below, write ONE concise \
definition (about 15-40 words) explaining the concept as used in the example sentences. Write a general \
definition (do not reference "the sentences", a specific initiative/country, and do not start with the \
term's name). If no examples, give a concise standard definition.
Return JSONL, one per line: {"term": <verbatim>, "definition": <one sentence>}. No code fences.

Terms:
%s"""


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

        # ---- extract ----
        terms = {}   # lower -> {display, occ, countries:set, inits:set, sents:list}
        for idx, i in enumerate(inits):
            text = strip_markup(f"{i.title}. {i.short_description or ''}. {i.content or ''}")
            sents = sentences_of(text)
            try:
                raw = ai(EXTRACT_PROMPT % text[:14000], max_tokens=1024)
            except Exception as e:
                set_status(state='error', phase='extract',
                           message=f'AI extraction failed: {e}', finished_at=now())
                return
            found = [ln.strip(' -•\t').strip() for ln in raw.splitlines() if ln.strip()]
            seen_here = set()
            for term in found:
                tl = term.lower()
                if len(tl) < 3 or tl in seen_here or len(term) > 120:
                    continue
                seen_here.add(tl)
                rec = terms.setdefault(tl, {'display': term, 'occ': 0, 'countries': set(),
                                            'inits': set(), 'sents': []})
                if i.country:
                    rec['countries'].add(i.country.strip())
                rec['inits'].add(i.id)
                for s in sents:
                    if tl in s.lower():
                        rec['occ'] += 1
                        if len(rec['sents']) < 8:
                            rec['sents'].append(s)
            if (idx + 1) % 3 == 0 or idx + 1 == total:
                set_status(phase='extract', processed=idx + 1, total=total,
                           message=f'Extracting terminology… {idx+1}/{total} initiatives, {len(terms)} terms')
            time.sleep(0.3)

        term_list = sorted(terms.keys(), key=lambda k: terms[k]['display'].lower())

        # ---- cluster (stage 1: per-term canonical) ----
        term_to_c = {}
        BATCH = 120
        nb = (len(term_list) + BATCH - 1) // BATCH
        for b in range(nb):
            chunk = term_list[b * BATCH:(b + 1) * BATCH]
            names = [terms[k]['display'] for k in chunk]
            got = {}
            try:
                for o in parse_jsonl(ai(CLUSTER_PROMPT % json.dumps(names, ensure_ascii=False), max_tokens=4096)):
                    if o.get('term') and o.get('canonical'):
                        got[str(o['term'])] = str(o['canonical']).strip()
            except Exception as e:
                set_status(state='error', phase='cluster', message=f'AI clustering failed: {e}',
                           finished_at=now())
                return
            for k in chunk:
                term_to_c[k] = got.get(terms[k]['display'], terms[k]['display'])
            set_status(phase='cluster', processed=b + 1, total=nb,
                       message=f'Clustering terms… batch {b+1}/{nb}')
            time.sleep(0.2)

        # ---- cluster (stage 2: merge labels) ----
        distinct = sorted(set(term_to_c.values()))
        c_to_final = {}
        try:
            for g in parse_jsonl(ai(MERGE_PROMPT % json.dumps(distinct, ensure_ascii=False), max_tokens=8192)):
                final = str(g.get('final', '')).strip()
                for m in g.get('members', []):
                    if final:
                        c_to_final[str(m).strip()] = final
        except Exception:
            pass
        for c in distinct:
            c_to_final.setdefault(c, c)

        # ---- aggregate canonical ----
        canon = {}   # final -> {aliases:set, occ, countries:set, inits:set, sents:list}
        for k in term_list:
            fin = c_to_final.get(term_to_c.get(k, terms[k]['display']), terms[k]['display'])
            c = canon.setdefault(fin, {'aliases': set(), 'occ': 0, 'countries': set(),
                                       'inits': set(), 'sents': []})
            c['aliases'].add(terms[k]['display'])
            c['occ'] += terms[k]['occ']
            c['countries'].update(terms[k]['countries'])
            c['inits'].update(terms[k]['inits'])
            for s in terms[k]['sents']:
                if len(c['sents']) < 8:
                    c['sents'].append(s)

        # ---- define ----
        final_terms = sorted(canon.keys(), key=lambda x: x.lower())
        defs = {}
        DB2 = 10
        nb2 = (len(final_terms) + DB2 - 1) // DB2
        for b in range(nb2):
            chunk = final_terms[b * DB2:(b + 1) * DB2]
            blocks = []
            for t in chunk:
                sents = canon[t]['sents']
                usage = '\n'.join(f'- {s[:300]}' for s in sents) if sents else '(none)'
                blocks.append(f'TERM: {t}\nUSAGE:\n{usage}')
            got = {}
            try:
                for o in parse_jsonl(ai(DEFINE_PROMPT % '\n\n'.join(blocks), max_tokens=2048, temperature=0.2)):
                    if o.get('term') and o.get('definition'):
                        got[str(o['term']).strip()] = ' '.join(str(o['definition']).split())
            except Exception:
                pass
            for t in chunk:
                defs[t] = got.get(t, '')
            set_status(phase='define', processed=b + 1, total=nb2,
                       message=f'Writing definitions… batch {b+1}/{nb2}')
            time.sleep(0.2)

        # ---- backup + load ----
        set_status(phase='backup', message='Backing up current glossary…')
        backup_id = _snapshot_current(label=f'auto-before-regenerate {now()}')

        set_status(phase='load', message='Publishing the new glossary…')
        taken = set()
        payload = []
        for t in final_terms:
            c = canon[t]
            aliases = sorted(c['aliases'], key=lambda x: x.lower())
            payload.append({
                'term': t, 'slug': slugify(t, taken), 'definition': defs.get(t, ''),
                'occurrences': c['occ'], 'countries': sorted(c['countries']),
                'aliases': aliases, 'initiative_ids': sorted(c['inits']), 'is_published': True,
            })
        _replace_glossary(payload, source='generated')

        set_status(state='done', phase='load', finished_at=now(),
                   message=f'Done — {len(payload)} terms from {total} initiatives (backup #{backup_id}).',
                   result={'terms': len(payload), 'initiatives': total, 'backup_id': backup_id})


if __name__ == '__main__':
    GEN = os.environ.get('GLOSSARY_GENERATE', '')
    RESTORE = os.environ.get('GLOSSARY_RESTORE', '')
    if RESTORE:
        do_restore(RESTORE)
    elif GEN == '1':
        try:
            do_generate()
        except Exception as e:
            with app.app_context():
                set_status(state='error', message=f'Unexpected error: {e}', finished_at=now())
