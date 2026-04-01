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
        # Try to parse JSON
        result = json.loads(response)
        return result
    except Exception as e:
        print(f"AI title/description generation error: {e}")
        return {"title": "", "description": ""}

def vet_tags_nvidia(phrases):
    """Filter noun phrases using AI to keep only relevant tags."""
    if not phrases:
        return []
    
    # Limit to 30 phrases to avoid token limits
    phrases = phrases[:30]
    phrases_text = ", ".join(phrases)
    prompt = f"""
    You are an AI assistant for an educational platform. From the following list of noun phrases extracted from an initiative, select up to 10 that are most relevant as tags.
    Return only the selected phrases as a comma-separated list.
    
    Phrases: {phrases_text}
    """
    try:
        response = call_nvidia_api(prompt, max_tokens=150)
        # Split and clean
        selected = [p.strip().lower() for p in response.split(',') if p.strip()]
        # Return unique, limited to 10
        return list(dict.fromkeys(selected))[:10]
    except Exception as e:
        print(f"Tag vetting error: {e}")
        return phrases[:10]  # fallback

def rank_members_by_query(query, user_data):
    """Rank members by relevance to query."""
    if not user_data:
        return []
    
    # Prepare a compact representation
    members_text = []
    for u in user_data:
        projects_text = '; '.join(u['projects'][:5])  # limit to 5 projects
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
                # Extract ID from format like "123 (0.95)"
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
