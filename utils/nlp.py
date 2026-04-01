import spacy
from flask import current_app

# Load spaCy model
try:
    nlp = spacy.load("en_core_web_sm")
except OSError:
    import subprocess
    subprocess.run(["python", "-m", "spacy", "download", "en_core_web_sm"])
    nlp = spacy.load("en_core_web_sm")

def extract_noun_phrases(text):
    """Extract noun phrases from text"""
    if not text:
        return []
    
    doc = nlp(text[:50000])  # Limit text length
    phrases = []
    
    for chunk in doc.noun_chunks:
        phrase = chunk.text.lower().strip()
        # Clean phrase
        phrase = ' '.join(phrase.split())  # Normalize whitespace
        if len(phrase) > 2 and len(phrase) < 50:
            phrases.append(phrase)
    
    # Remove duplicates while preserving order
    seen = set()
    unique_phrases = []
    for p in phrases:
        if p not in seen and not p.isdigit():
            seen.add(p)
            unique_phrases.append(p)
    
    return unique_phrases[:20]  # Limit to top 20

def update_noun_phrase_db(initiative_id, phrases):
    """Update noun phrase database"""
    from app import db, NounPhrase, Tag, Initiative
    
    # Clear existing phrases for this initiative
    NounPhrase.query.filter_by(initiative_id=initiative_id).delete()
    
    # Add new phrases
    for phrase in phrases:
        # Check if phrase matches any vetted tag
        tag = Tag.query.filter(Tag.name.ilike(phrase)).first()
        
        np = NounPhrase(
            phrase=phrase,
            initiative_id=initiative_id,
            tag_id=tag.id if tag else None
        )
        db.session.add(np)
    
    db.session.commit()
