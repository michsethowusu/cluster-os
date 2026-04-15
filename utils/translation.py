from deep_translator import GoogleTranslator

def translate_text(text, target_lang='fr', source_lang='auto'):
    if not text or len(text.strip()) == 0:
        return text
    
    try:
        translator = GoogleTranslator(source=source_lang, target=target_lang)
        
        # deep-translator handles chunking automatically, but has a 5000 char limit per request
        max_length = 4000
        if len(text) <= max_length:
            return translator.translate(text)
        
        # Manual chunking for long texts
        chunks = []
        current_chunk = ""
        
        for sentence in text.split('. '):
            if len(current_chunk) + len(sentence) < max_length:
                current_chunk += sentence + '. '
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                current_chunk = sentence + '. '
        
        if current_chunk:
            chunks.append(current_chunk.strip())
        
        translated_chunks = []
        for chunk in chunks:
            if chunk:
                translated = translator.translate(chunk)
                translated_chunks.append(translated)
        
        return ' '.join(translated_chunks)
        
    except Exception as e:
        print(f"Translation error: {e}")
        return text  # Return original if translation fails
