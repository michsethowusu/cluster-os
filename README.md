## Setup Instructions:

1. **Install dependencies:**

bash

 Copy

```bash
pip install -r requirements.txt
python -m spacy download en_core_web_sm
```

1. **Configure environment variables:** Edit the `.env` file with your actual credentials:

- Gmail App Password (not your regular password)
- NVIDIA NIM API Key from build.nvidia.com
- Secret key for Flask sessions

1. **Initialize database:**

bash

 Copy

```bash
flask init-db
```

1. **Run the application:**

bash

 Copy

```bash
python app.py
```

1. **Access the admin panel:**

- Default admin email is set in `.env` (default: [admin@au-eced-fln.org](mailto:admin@africa-eced-fln.org))
- First user to register with that email becomes admin, or run `flask init-db` to create default admin





 Create Structured Flask Codebase with SQLAlchemy and Flask-Login

 

The user wants a comprehensive Flask application for the AU ECED and FLN Cluster platform. Let me break down the requirements:

1. **Core Features:**
   - OTP-based email login (no passwords)
   - Registration form with ECED Initiative submission
   - Admin approval workflow for registrations and initiatives
   - File-based SQLite database (no separate server)
   - Article/Initiative submission and editing
   - Auto-tagging using noun phrases vetted by AI (NVIDIA NIM API)
   - Full-text search using tokenized noun phrases
   - Auto-translation to French (Google Translate API)
   - AI-generated catchy titles and descriptions (NVIDIA NIM API)
   - Q&A/Forum section with upvote/downvote
   - Organization-based leaderboard (not individual)
   - Stakeholder statistics on homepage
   - Clean, modern UI maintaining current Joomla feel
2. **Technical Requirements:**
   - Flask app with file-based DB
   - Gmail SMTP for emails
   - NVIDIA NIM API (meta/llama-3.1-70b-instruct) for AI features
   - Google Translate API (free)
   - Admin panel for managing approvals and form fields
   - Noun phrase extraction and database updates
3. **Stakeholder Types:** Government, NGO/Civil Society, Development Partner/Donor, Academic/Research, UN Agency, Private Sector
4. **Q&A Permissions:** Admin-only posting of questions

Let me create a complete, production-ready Flask application structure:

**File Structure:**

plain

 Copy

```plain
au-ecd-fln-platform/
├── app.py                 # Main application entry
├── config.py              # Configuration settings
├── requirements.txt       # Dependencies
├── .env.example          # Environment variables template
├── database.db           # SQLite database (created on run)
├── static/
│   ├── css/
│   │   └── style.css     # Custom styles
│   ├── js/
│   │   └── main.js       # Frontend logic
│   └── uploads/          # File uploads
├── templates/
│   ├── base.html         # Base template
│   ├── index.html        # Homepage
│   ├── login.html        # OTP login
│   ├── register.html     # Registration
│   ├── dashboard.html    # User dashboard
│   ├── article.html      # Article detail
│   ├── article_form.html # Create/Edit article
│   ├── search.html       # Search results
│   ├── forum.html        # Q&A forum
│   ├── question.html     # Question detail
│   ├── admin/
│   │   ├── dashboard.html
│   │   ├── approvals.html
│   │   ├── settings.html
│   │   └── fields.html
│   └── emails/           # Email templates
└── utils/
    ├── __init__.py
    ├── auth.py           # OTP and authentication
    ├── email_sender.py   # Gmail SMTP
    ├── ai_services.py    # NVIDIA NIM integration
    ├── search.py         # Full-text search logic
    ├── nlp.py            # Noun phrase extraction
    └── translation.py    # Google Translate
```

Since I cannot actually create a zip file for download (as per my instructions), I will provide all the code files in a structured format that the user can copy into their own directory structure. I'll create a comprehensive, well-organized codebase.

Let me start building this step by step:

1. First, the requirements and configuration
2. Database models (SQLAlchemy)
3. Utility modules (AI, email, search, etc.)
4. Route handlers
5. Templates (HTML with Jinja2)
6. Static files (CSS/JS)

Key design decisions:

- Use Flask-SQLAlchemy for ORM
- Use Flask-Login for session management
- Use spaCy for noun phrase extraction (lightweight)
- Use NVIDIA NIM API for AI features
- Use googletrans for translation (free, though limited)
- Use SQLite for simplicity (file-based as requested)
- Modular architecture for future extensibility

Let me write the code:

I'll build a comprehensive Flask application for the AU ECED and FLN Cluster platform. Since I cannot generate downloadable ZIP files directly, I'll provide you with the complete file structure and code that you can easily set up.

Create this directory structure:

plain

 Copy

```plain
au-ecd-fln-platform/
├── app.py
├── config.py
├── requirements.txt
├── .env
├── database.db (created automatically)
├── static/
│   ├── css/style.css
│   ├── js/main.js
│   └── uploads/
└── templates/
    ├── base.html
    ├── index.html
    ├── login.html
    ├── register.html
    ├── dashboard.html
    ├── article.html
    ├── article_form.html
    ├── search.html
    ├── forum.html
    ├── question.html
    ├── members.html
    └── admin/
        ├── base.html
        ├── dashboard.html
        ├── approvals.html
        └── settings.html
```
