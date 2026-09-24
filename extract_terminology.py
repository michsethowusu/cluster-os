"""Env-gated, resumable extraction of ECED-FLN terminology from the highest-quality
initiatives (AI score 4-5, published). Runs on the server because the prod DB is
only reachable inside the container. Gemini does the extraction; results are kept
grounded by matching every extracted term back to real sentences in the source text.

Guards / env:
  EXTRACT_TERMINOLOGY=1        run (resumable; safe to re-run)
  EXTRACT_TERMINOLOGY=clear    wipe the result settings (cleanup after retrieval)
  GEMINI_API_KEY=<key>         Google Generative Language API key

Outputs (read back via the /backfill-status endpoint):
  terminology_status        'processed X / Y initiatives; N unique terms'
  terminology_list_json     [{term, count, countries:[...]}]  (unique terminologies)
  terminology_rows_json     [{term, sentence, country, initiative_id, initiative_title}]
"""
import os
import re
import json
import time

MODE = os.environ.get('EXTRACT_TERMINOLOGY', '')
if MODE not in ('1', 'clear'):
    raise SystemExit(0)

from app import app, db, Initiative, get_setting, set_setting  # noqa: E402

MODEL = 'gemini-flash-latest'


def _clear():
    with app.app_context():
        for k in ('terminology_status', 'terminology_list_json',
                  'terminology_rows_json', 'terminology_done_ids'):
            set_setting(k, '')
        print('[extract_terminology] result settings cleared')


if MODE == 'clear':
    _clear()
    raise SystemExit(0)

import requests  # noqa: E402

API_KEY = os.environ.get('GEMINI_API_KEY', '').strip()
if not API_KEY:
    with app.app_context():
        set_setting('terminology_status', 'error: GEMINI_API_KEY not set')
    raise SystemExit(0)

URL = f'https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={API_KEY}'

PROMPT = """You are a terminologist building a glossary for the Early Childhood Education \
and Development (ECED) and Foundational Learning and Numeracy (FLN) sector in Africa.

From the initiative text below, extract the domain-specific TERMINOLOGY actually present \
in the text: technical terms, concepts, programmes, pedagogical approaches, and sector \
jargon relevant to early childhood education, foundational literacy and numeracy, and \
teaching/learning. Examples of the KIND of thing to extract (do not invent these if absent): \
"foundational literacy", "school readiness", "play-based learning", "mother-tongue instruction", \
"structured pedagogy", "learning outcomes", "numeracy", "teacher professional development", \
"early stimulation", "inclusive education", "continuous assessment".

Rules:
- Only return terms that genuinely appear (verbatim or as a close morphological variant) in the text.
- Return each term in its natural, canonical form (lowercase unless it is an acronym or proper name).
- Do NOT return generic filler words, ordinary verbs, place names, person names, or organisation names.
- Return STRICT JSON only: a JSON array of strings. No prose, no markdown, no code fences.

Initiative text:
---
{TEXT}
---"""


def strip_markup(s):
    if not s:
        return ''
    s = re.sub(r'<(br|/p|/li|/h[1-6]|/div|/tr)\s*/?>', '. ', s, flags=re.I)
    s = re.sub(r'<[^>]+>', ' ', s)                 # remaining HTML tags
    s = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', s)  # markdown links -> text
    s = re.sub(r'[`*_#>]+', ' ', s)                # markdown emphasis/heading marks
    s = s.replace('&amp;', '&').replace('&nbsp;', ' ').replace('&#39;', "'")
    s = s.replace('&quot;', '"').replace('&lt;', '<').replace('&gt;', '>')
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def split_sentences(text):
    parts = re.split(r'(?<=[.!?])\s+|\n+', text)
    out = []
    for p in parts:
        p = p.strip()
        if len(p) >= 15:  # drop fragments
            out.append(p)
    return out


def call_gemini(text):
    payload = {
        'contents': [{'parts': [{'text': PROMPT.replace('{TEXT}', text[:16000])}]}],
        'generationConfig': {'temperature': 0.0, 'maxOutputTokens': 2048},
    }
    last = None
    for attempt in range(4):
        try:
            r = requests.post(URL, json=payload, timeout=90)
            if r.status_code in (429, 500, 502, 503, 504):
                last = f'HTTP {r.status_code}'
                time.sleep(min(2 ** attempt, 8))
                continue
            r.raise_for_status()
            d = r.json()
            raw = d['candidates'][0]['content']['parts'][0]['text']
            raw = raw.strip()
            raw = re.sub(r'^```(?:json)?|```$', '', raw, flags=re.I).strip()
            m = re.search(r'\[.*\]', raw, flags=re.S)
            if m:
                raw = m.group(0)
            terms = json.loads(raw)
            return [str(t).strip() for t in terms if str(t).strip()]
        except Exception as e:
            last = str(e)
            time.sleep(min(2 ** attempt, 8))
    print(f'[extract_terminology] gemini failed: {last}')
    return None


with app.app_context():
    rows = (
        Initiative.query
        .filter(Initiative.is_published == True)  # noqa: E712
        .filter(Initiative.quality_score != None)  # noqa: E711
        .filter(Initiative.quality_score >= 4)
        .order_by(Initiative.id.asc())
        .all()
    )
    total = len(rows)

    try:
        done_ids = set(json.loads(get_setting('terminology_done_ids', '[]') or '[]'))
    except Exception:
        done_ids = set()
    try:
        dataset = json.loads(get_setting('terminology_rows_json', '[]') or '[]')
    except Exception:
        dataset = []
    # rebuild the unique index from whatever is already saved
    uniq = {}  # lower -> {term, count, countries:set}
    for row in dataset:
        key = row['term'].lower()
        u = uniq.setdefault(key, {'term': row['term'], 'count': 0, 'countries': set()})
        u['count'] += 1
        if row.get('country'):
            u['countries'].add(row['country'])

    processed = len(done_ids)
    set_setting('terminology_status', f'starting: 0 / {total} initiatives')

    for i in rows:
        if i.id in done_ids:
            continue
        text = strip_markup(f"{i.title}. {i.short_description or ''}. {i.content or ''}")
        sentences = split_sentences(text)
        terms = call_gemini(text)
        if terms is None:
            # transient failure: stop and leave progress for a later re-run
            set_setting('terminology_status',
                        f'paused (AI error) at {processed} / {total}; re-run to continue')
            break

        seen_here = set()
        for term in terms:
            tl = term.lower()
            if len(tl) < 3 or tl in seen_here:
                continue
            seen_here.add(tl)
            # match the term back to real sentences (grounding)
            matched = [s for s in sentences if tl in s.lower()]
            u = uniq.setdefault(tl, {'term': term, 'count': 0, 'countries': set()})
            if i.country:
                u['countries'].add(i.country)
            for s in matched[:5]:  # cap sentences per term per initiative
                dataset.append({
                    'term': term,
                    'sentence': s,
                    'country': i.country or '',
                    'initiative_id': i.id,
                    'initiative_title': i.title,
                })
                u['count'] += 1
            if not matched:
                # keep the term in the glossary even without an exact sentence hit
                u['count'] += 0

        done_ids.add(i.id)
        processed += 1

        # persist incrementally every few initiatives (resumable + visible progress)
        if processed % 3 == 0 or processed == total:
            uniq_list = sorted(
                ({'term': v['term'], 'count': v['count'],
                  'countries': sorted(v['countries'])} for v in uniq.values()),
                key=lambda x: (-x['count'], x['term'].lower()),
            )
            set_setting('terminology_list_json', json.dumps(uniq_list, ensure_ascii=False))
            set_setting('terminology_rows_json', json.dumps(dataset, ensure_ascii=False))
            set_setting('terminology_done_ids', json.dumps(sorted(done_ids)))
            set_setting('terminology_status',
                        f'processed {processed} / {total} initiatives; '
                        f'{len(uniq)} unique terms; {len(dataset)} sentence rows')
        time.sleep(1.0)  # pace the API

    # final flush
    uniq_list = sorted(
        ({'term': v['term'], 'count': v['count'],
          'countries': sorted(v['countries'])} for v in uniq.values()),
        key=lambda x: (-x['count'], x['term'].lower()),
    )
    set_setting('terminology_list_json', json.dumps(uniq_list, ensure_ascii=False))
    set_setting('terminology_rows_json', json.dumps(dataset, ensure_ascii=False))
    set_setting('terminology_done_ids', json.dumps(sorted(done_ids)))
    if len(done_ids) >= total:
        set_setting('terminology_status',
                    f'done: {total} initiatives; {len(uniq)} unique terms; '
                    f'{len(dataset)} sentence rows')
    print(f'[extract_terminology] {get_setting("terminology_status")}')
