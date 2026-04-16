import os
import requests
import json

def call_nvidia_api(prompt, max_tokens=300, temperature=0.7):
    """Call NVIDIA NIM API with the given prompt."""
    api_key = os.environ.get('NVIDIA_API_KEY')
    if not api_key:
        raise Exception("NVIDIA_API_KEY not set")
    
    url = "https://integrate.api.nvidia.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "meta/llama-3.1-70b-instruct",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature
    }
    
    response = requests.post(url, headers=headers, json=payload)
    response.raise_for_status()
    data = response.json()
    return data['choices'][0]['message']['content'].strip()


def score_initiative_quality(title, content, short_description=""):
    """
    Score an initiative on two dimensions, each 1–5, then return the average
    rounded to the nearest integer (1–5 overall).

    Dimension A — Writing quality & clarity
        How well-written, structured, and detailed the submission is.

    Dimension B — Implementation alignment
        Whether this describes an ECED-FLN initiative that is ACTUALLY BEING
        IMPLEMENTED (or has been), NOT a proposal, plan, or aspiration.
        Strong evidence: named locations, beneficiary counts, partner orgs,
        results, timelines that have already passed, lessons learned, etc.

    Only initiatives scoring 4 or 5 overall are sent as digest notifications.
    Returns an int between 1 and 5, or None on failure.
    """
    combined = (
        f"Title: {title}\n\n"
        f"Short description: {short_description}\n\n"
        f"Content:\n{content[:3000]}"
    )
    prompt = f"""
You are a senior reviewer for the African Union ECED-FLN (Early Childhood Education and
Development & Foundational Learning) Cluster Platform. Your job is to evaluate member-submitted
initiative descriptions on two independent dimensions.

=== DIMENSION A: Writing Quality & Clarity (1–5) ===
Score how well-written, structured, and informative the text is.

5 – Excellent: Well-structured, specific, rich detail, clear purpose, evidence or data cited.
4 – Good: Clear and informative, minor gaps in detail or structure.
3 – Average: Readable but vague, lacks specifics or supporting context.
2 – Weak: Poorly organised, thin content, hard to understand the initiative.
1 – Very poor: Placeholder text, incoherent, or almost no substance.

=== DIMENSION B: Implementation Evidence (1–5) ===
Score how clearly this describes an initiative that is ACTUALLY BEING IMPLEMENTED or
HAS BEEN IMPLEMENTED — NOT a proposal, concept note, or future plan.

Strong positive signals (push score UP):
  • Named geography / country / region / district where it runs
  • Specific beneficiary counts (children, teachers, schools reached)
  • Named partner organisations or funders
  • Activities described in past or present tense ("we trained", "the programme reaches")
  • Measurable outcomes, results, or lessons learned
  • Dates or timeframes that have already occurred

Strong negative signals (push score DOWN):
  • Language like "we propose", "we plan to", "this initiative will", "our goal is to"
  • No named location, no beneficiaries, no partners
  • Entirely future-tense or aspirational
  • Generic description with no operational detail

5 – Clear, concrete evidence of active/completed implementation with specifics.
4 – Mostly implemented, a few specifics present, minor aspirational language.
3 – Mixed — some implementation evidence but also significant proposal language.
2 – Mostly a proposal or plan, little evidence of actual activity.
1 – Purely aspirational / conceptual, no implementation at all.

=== OUTPUT FORMAT ===
Respond ONLY with a valid JSON object — no markdown, no explanation outside the JSON:
{{
  "quality_score": <int 1-5>,
  "implementation_score": <int 1-5>,
  "quality_reason": "<one sentence>",
  "implementation_reason": "<one sentence>"
}}

=== INITIATIVE TO EVALUATE ===
{combined}
"""
    try:
        response = call_nvidia_api(prompt, max_tokens=200, temperature=0.1)
        clean = response.strip().replace('```json', '').replace('```', '').strip()
        result = json.loads(clean)
        q = max(1, min(5, int(result.get("quality_score", 3))))
        i = max(1, min(5, int(result.get("implementation_score", 3))))
        overall = round((q + i) / 2)
        # Store the sub-scores in the result for optional logging
        print(
            f"[score_initiative_quality] quality={q} ({result.get('quality_reason','')}) | "
            f"implementation={i} ({result.get('implementation_reason','')}) | overall={overall}"
        )
        return overall
    except Exception as e:
        print(f"Initiative quality scoring error: {e}")
        return None


def generate_title_description(content):
    """Generate catchy title and short description from initiative content."""
    prompt = f"""
    You are an AI assistant for an educational platform. Given the following initiative content, generate:
    1. A catchy, engaging title (max 80 characters)
    2. A concise, compelling short description (max 300 characters)
    
    Return your answer in JSON format:
    {{"title": "your title", "description": "your description"}}
    
    Content:
    {content[:3000]}
    """
    try:
        response = call_nvidia_api(prompt, max_tokens=200)
        result = json.loads(response)
        return result
    except Exception as e:
        print(f"AI title/description generation error: {e}")
        return {"title": "", "description": ""}


def vet_tags_nvidia(phrases):
    """Filter noun phrases using AI to keep only relevant tags."""
    if not phrases:
        return []
    
    phrases = phrases[:30]
    phrases_text = ", ".join(phrases)
    prompt = f"""
    You are an AI assistant for an educational platform. From the following list of noun phrases extracted from an initiative, select up to 10 that are most relevant as tags.
    Return only the selected phrases as a comma-separated list.
    
    Phrases: {phrases_text}
    """
    try:
        response = call_nvidia_api(prompt, max_tokens=150)
        selected = [p.strip().lower() for p in response.split(',') if p.strip()]
        return list(dict.fromkeys(selected))[:10]
    except Exception as e:
        print(f"Tag vetting error: {e}")
        return phrases[:10]


def rank_members_by_query(query, user_data):
    """Rank members by relevance to query."""
    if not user_data:
        return []
    
    members_text = []
    for u in user_data:
        projects_text = '; '.join(u['projects'][:5])
        members_text.append(f"ID {u['id']}: {projects_text}")
    members_str = "\n".join(members_text)
    
    prompt = f"""
    You are an AI assistant for a knowledge platform. Given the following list of members, each with an ID and a list of projects they are involved in, rank them by relevance to the query: "{query}".
    Return only the member IDs in order of most relevant to least relevant, one per line, with a score from 0 to 1 in parentheses after the ID, like:
    123 (0.95)
    456 (0.78)
    ...
    If no relevant members, return "None".
    
    Member data:
    {members_str}
    """
    try:
        response = call_nvidia_api(prompt, max_tokens=200)
        lines = response.strip().split('\n')
        ids = []
        for line in lines:
            if line.strip() and not line.startswith('None'):
                parts = line.split('(')
                if parts:
                    try:
                        ids.append(int(parts[0].strip()))
                    except ValueError:
                        continue
        return ids
    except Exception as e:
        print(f"AI ranking error: {e}")
        return []


def detect_language(title, content):
    """
    Detect the language of an initiative using the NVIDIA AI model.
    Returns an ISO 639-1 language code compatible with deep_translator's
    GoogleTranslator source parameter (e.g. 'en', 'fr', 'pt', 'ar', 'sw').
    Returns None on failure.
    """
    sample = f"{title}\n\n{content[:1500]}"
    prompt = f"""You are a language detection tool. Identify the primary language of the following text.
Respond ONLY with a valid ISO 639-1 two-letter language code (e.g. en, fr, pt, ar, sw, es, am).
Do not include any explanation, punctuation, or extra text — just the two-letter code.

Text:
{sample}

Language code:"""
    try:
        response = call_nvidia_api(prompt, max_tokens=10, temperature=0.0)
        code = response.strip().lower().split()[0][:5]
        # Validate it looks like a language code (2-3 letters)
        if code.isalpha() and 2 <= len(code) <= 3:
            return code
        return None
    except Exception as e:
        print(f"Language detection error: {e}")
        return None


def clean_tags_for_polls(poll_title):
    """Extract tags from poll title using AI."""
    prompt = f"""
    Extract up to 5 key noun phrases (tags) from the following poll title. Return them as a comma-separated list.
    Title: {poll_title}
    Tags:
    """
    try:
        response = call_nvidia_api(prompt, max_tokens=100)
        tags = [tag.strip().lower() for tag in response.split(',') if tag.strip()]
        return tags[:5]
    except Exception as e:
        print(f"Poll tag extraction error: {e}")
        return []
