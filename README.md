# AU ECED-FLN Cluster Platform

A Flask-based web platform for the African Union Early Childhood Education and Development & Foundational Learning (ECED-FLN) Cluster. It connects member organisations across Africa to share initiatives, participate in forums, register for events, and collaborate on projects.

---

## Tech Stack

- **Backend:** Python 3.11, Flask, Flask-SQLAlchemy, Flask-Login
- **Database:** PostgreSQL (via SQLAlchemy)
- **Email:** Brevo API (transactional email)
- **AI Features:** NVIDIA NIM API (meta/llama-3.1-70b-instruct)
- **NLP:** spaCy (noun phrase extraction for auto-tagging)
- **Markdown:** mistune + bleach
- **Deployment:** Docker + Coolify

---

## Project Structure

```
project/
├── app.py                  # Main application — models, routes, CLI commands
├── config.py               # Configuration (reads from environment variables)
├── requirements.txt        # Python dependencies
├── Dockerfile              # Docker build
├── entrypoint.sh           # Startup script (DB init + gunicorn)
├── static/
│   ├── css/style.css
│   ├── js/main.js
│   └── uploads/
├── templates/
│   ├── base.html
│   ├── index.html
│   ├── login.html
│   ├── register.html
│   ├── verify_otp.html
│   ├── dashboard.html
│   ├── article.html
│   ├── article_form.html
│   ├── search.html
│   ├── forum.html
│   ├── question.html
│   ├── members.html
│   ├── leaderboard.html
│   ├── events.html
│   ├── event_detail.html
│   ├── event_register.html
│   ├── polls.html
│   ├── poll_detail.html
│   ├── projects.html
│   ├── project_detail.html
│   ├── profile_edit.html
│   ├── search_members.html
│   └── admin/
│       ├── dashboard.html
│       ├── approvals.html
│       ├── settings.html
│       ├── fields.html
│       ├── members.html
│       ├── initiatives.html
│       ├── events.html
│       ├── event_form.html
│       ├── projects.html
│       ├── project_form.html
│       ├── import_members.html
│       └── import_initiatives.html
└── utils/
    ├── __init__.py
    ├── email_sender.py     # Brevo API email functions
    ├── ai_services.py      # NVIDIA NIM API integration
    ├── nlp.py              # Noun phrase extraction (spaCy)
    └── translation.py      # Text translation
```

---

## Environment Variables

Set these in Coolify (or a `.env` file for local development):

| Variable | Description |
|---|---|
| `SECRET_KEY` | Flask session secret key |
| `POSTGRESQL_URL` | Full PostgreSQL connection URL |
| `BREVO_API_KEY` | Brevo transactional email API key |
| `MAIL_DEFAULT_SENDER` | Sender name and email, e.g. `Name <email@domain.com>` |
| `NVIDIA_API_KEY` | NVIDIA NIM API key for AI features |
| `ADMIN_EMAIL` | Email address for the admin account |
| `APP_URL` | Full public URL of the app, e.g. `https://yourdomain.com` |

> **Note:** The app will refuse to start if `POSTGRESQL_URL` (or `DATABASE_URL`) is not set.

---

## Deployment (Coolify + Docker)

The app is deployed via Docker on Coolify. On every container start, `entrypoint.sh` runs `db.create_all()` to ensure all database tables exist (safe to run repeatedly — it never drops existing data), then starts gunicorn.

### Dockerfile summary

- Base image: `python:3.11-slim`
- Installs dependencies from `requirements.txt`
- Runs on port `3000` via gunicorn with 4 workers
- Health check: `GET /health`

### entrypoint.sh

```sh
#!/bin/sh
set -e
python -c "
from app import app, db
with app.app_context():
    db.create_all()
    print('DB tables ready.')
"
exec gunicorn -w 4 -b 0.0.0.0:3000 app:app
```

---

## Initial Setup (First Deploy)

After the first successful deploy, set the admin password via the Coolify terminal:

```bash
flask set-admin-password yourchosenpassword
```

This only needs to be done once. The password is stored as a bcrypt hash in the database.

---

## Authentication

- **Regular members** log in with their email and receive a **6-digit OTP** via email (valid for 10 minutes).
- **Admin** logs in with email + **password** (no OTP). Password is set via the `flask set-admin-password` CLI command.

---

## Key Features

### Member-facing
- OTP-based passwordless login
- Registration with stakeholder profile and project descriptions
- Submit and edit ECED-FLN initiatives (Markdown supported)
- Forum Q&A with upvote/downvote on recommendations
- Event listing and registration with embedded polls
- Project participation
- AI-powered member search (finds members by project expertise)
- French translation toggle on content

### Admin panel (`/admin`)
- Approve/reject member registrations
- Publish/unpublish initiatives and forum questions
- Manage events and polls
- Manage projects and activities
- Import members and initiatives via CSV
- Configure registration form fields
- Toggle auto-approval for new members
- Trigger NLP re-processing on published initiatives

### AI & NLP (NVIDIA NIM)
- Auto-generates initiative tags from content using noun phrase extraction (spaCy) vetted by AI
- Generates catchy titles and short descriptions for initiatives
- Ranks members by relevance to a search query

---

## Local Development

```bash
# Install dependencies
pip install -r requirements.txt
python -m spacy download en_core_web_sm

# Create a .env file with the required variables (see above)

# Initialise the database and create admin user
flask init-db

# Set admin password
flask set-admin-password yourpassword

# Run
python app.py
```

The app runs on `http://localhost:5000` in development mode.

---

## Notes

- The sender domain (`cluster@eced-au.org`) must be a **verified sender** in your Brevo account under Settings → Senders & IPs.
- `APP_URL` must be set correctly for email links to work (e.g. login links in approval emails).
- File uploads are stored in `static/uploads/` — if using Docker, mount a persistent volume to this path to retain uploads across redeploys.
- SQLite is not supported — PostgreSQL only.
