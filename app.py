from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, abort, session, Response, send_from_directory
from markupsafe import Markup
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
import os
import re
import random
import string
import bleach
import csv
import io
import json
import click
import threading
import uuid
import mistune
from config import Config

app = Flask(__name__)
app.config.from_object(Config)

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Import utilities after app initialization
from utils.email_sender import (
    send_otp_email,
    send_approval_email,
    send_event_notification,
    send_member_notification,
    send_import_welcome_email,
    send_invitation_email,
    send_initiative_approved_email,
    send_initiative_pending_email,
    send_project_notification,
    send_project_approved_email,
    send_event_approved_email,
    send_project_signup_confirmation,
    send_project_signup_admin_alert,
    send_bulk_initiatives_digest,
    send_single_initiative_notification,
    send_event_registration_confirmation,
    send_custom_bulk_email,
    send_single_policy_notification,
    send_bulk_policies_digest,
)
from utils.ai_services import generate_title_description, vet_tags_nvidia, rank_members_by_query, clean_tags_for_polls, score_initiative_quality, detect_language
from utils.nlp import extract_noun_phrases, update_noun_phrase_db
from utils.translation import translate_text
from utils.zoom_api import (
    create_zoom_webinar,
    register_user_for_webinar,
    fetch_recording_url,
    delete_zoom_webinar,
)

# ===================== MODELS =====================

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    organization = db.Column(db.String(200), nullable=False)
    stakeholder_type = db.Column(db.String(50), nullable=False)
    country = db.Column(db.String(100), nullable=False)
    is_approved = db.Column(db.Boolean, default=False)
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    otp = db.Column(db.String(6), nullable=True)
    otp_expiry = db.Column(db.DateTime, nullable=True)
    password_hash = db.Column(db.String(256), nullable=True)
    points = db.Column(db.Integer, default=0, nullable=False)
    is_subscribed = db.Column(db.Boolean, default=True, nullable=False)

    # Relationships
    initiatives = db.relationship('Initiative', backref='author', lazy=True)
    recommendations = db.relationship('Recommendation', backref='author', lazy=True)
    votes = db.relationship('Vote', backref='user', lazy=True)
    comments = db.relationship('Comment', backref='author', lazy=True)
    
    # New relationships – defined here, child models only contain foreign keys
    member_projects = db.relationship('MemberProject', backref='user', lazy='dynamic', cascade='all, delete-orphan')
    event_registrations = db.relationship('EventRegistration', backref='user', lazy='dynamic', cascade='all, delete-orphan')
    project_participations = db.relationship('ProjectParticipation', backref='user', lazy='dynamic', cascade='all, delete-orphan')
    
    @property
    def is_active(self):
        return self.is_approved
    
    @property
    def is_authenticated(self):
        return True
    
    @property
    def is_anonymous(self):
        return False
    
    def get_id(self):
        return str(self.id)


class MemberProject(db.Model):
    """Short project descriptions from members (max 300 chars)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    description = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Setting(db.Model):
    """Key-value store for application settings."""
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.String(500))


class Initiative(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(200), unique=True, nullable=False)
    content = db.Column(db.Text, nullable=False)
    short_description = db.Column(db.String(300))
    stakeholder_type = db.Column(db.String(50))
    country = db.Column(db.String(100))
    is_published = db.Column(db.Boolean, default=False)
    view_count = db.Column(db.Integer, default=0, nullable=False)
    quality_score = db.Column(db.Integer, nullable=True)  # 1-5, admin-only, scored by AI
    detected_lang = db.Column(db.String(10), nullable=True)  # ISO 639-1 code, e.g. 'fr', 'en', 'ar'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    
    tags = db.relationship('Tag', secondary='initiative_tags', backref='initiatives')
    comments = db.relationship('Comment', backref='initiative', lazy='dynamic',
                               cascade='all, delete-orphan')


class Comment(db.Model):
    """Member comments on initiatives, require admin approval before going live."""
    id = db.Column(db.Integer, primary_key=True)
    initiative_id = db.Column(db.Integer, db.ForeignKey('initiative.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    content = db.Column(db.Text, nullable=False)
    is_approved = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Tag(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    is_vetted = db.Column(db.Boolean, default=False)
    usage_count = db.Column(db.Integer, default=0)


initiative_tags = db.Table('initiative_tags',
    db.Column('initiative_id', db.Integer, db.ForeignKey('initiative.id'), primary_key=True),
    db.Column('tag_id', db.Integer, db.ForeignKey('tag.id'), primary_key=True)
)


class NounPhrase(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    phrase = db.Column(db.String(200), nullable=False)
    initiative_id = db.Column(db.Integer, db.ForeignKey('initiative.id'))
    tag_id = db.Column(db.Integer, db.ForeignKey('tag.id'))


class RegistrationField(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    field_name = db.Column(db.String(100), nullable=False)
    field_label = db.Column(db.String(200), nullable=False)
    field_type = db.Column(db.String(50), default='text')
    is_required = db.Column(db.Boolean, default=True)
    options = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    order = db.Column(db.Integer, default=0)


class Question(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(300), nullable=False)
    content = db.Column(db.Text, nullable=False)
    is_published = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    
    user = db.relationship('User', backref='questions')
    
    recommendations = db.relationship('Recommendation', backref='question', lazy=True)


class Project(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(300), nullable=False)
    description = db.Column(db.Text, nullable=False)
    deadline = db.Column(db.DateTime, nullable=False)
    start_date = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=True)
    is_published = db.Column(db.Boolean, default=False)
    submitted_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)

    activities = db.relationship('ProjectActivity', backref='project', lazy=True, cascade='all, delete-orphan')
    participations = db.relationship('ProjectParticipation', backref='project', lazy=True)
    submitter = db.relationship('User', foreign_keys=[submitted_by])


class ProjectActivity(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('project.id'), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    deadline = db.Column(db.DateTime, nullable=True)
    
    participations = db.relationship('ProjectParticipation', backref='activity', lazy=True)


class ProjectParticipation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('project.id'), nullable=False)
    activity_id = db.Column(db.Integer, db.ForeignKey('project_activity.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    participated_at = db.Column(db.DateTime, default=datetime.utcnow)
    # No explicit relationship – defined in User


class Recommendation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    initiative_id = db.Column(db.Integer, db.ForeignKey('initiative.id'), nullable=True)
    question_id = db.Column(db.Integer, db.ForeignKey('question.id'), nullable=True)
    score = db.Column(db.Integer, default=0)


class Vote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    recommendation_id = db.Column(db.Integer, db.ForeignKey('recommendation.id'), nullable=False)
    vote_type = db.Column(db.Integer, nullable=False)
    
    recommendation = db.relationship('Recommendation', backref='votes')


class Translation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(500), nullable=False)
    language = db.Column(db.String(10), default='fr')
    translation = db.Column(db.Text, nullable=False)


# NEW MODELS FOR EVENTS AND POLLS
class Event(db.Model):
    id                 = db.Column(db.Integer, primary_key=True)
    title              = db.Column(db.String(200), nullable=False)
    description        = db.Column(db.Text, nullable=False)
    start_date         = db.Column(db.DateTime, nullable=False)
    end_date           = db.Column(db.DateTime)
    created_at         = db.Column(db.DateTime, default=datetime.utcnow)
    created_by         = db.Column(db.Integer, db.ForeignKey('user.id'))
    is_published       = db.Column(db.Boolean, default=False)
    submitted_by       = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    zoom_webinar_id    = db.Column(db.String(100), nullable=True)   # stores Zoom Meeting ID
    zoom_recording_url = db.Column(db.String(500), nullable=True)

    polls         = db.relationship('Poll',              backref='event', lazy='dynamic', cascade='all, delete-orphan')
    registrations = db.relationship('EventRegistration', backref='event', lazy='dynamic', cascade='all, delete-orphan')
    attachments   = db.relationship('EventAttachment',   backref='event', lazy='dynamic', cascade='all, delete-orphan')
    submitter     = db.relationship('User', foreign_keys=[submitted_by])


class Poll(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('event.id'), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    options = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    tags = db.relationship('PollTag', backref='poll', lazy='dynamic', cascade='all, delete-orphan')


class PollTag(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    poll_id = db.Column(db.Integer, db.ForeignKey('poll.id'), nullable=False)
    tag = db.Column(db.String(100), index=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class EventRegistration(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    event_id = db.Column(db.Integer, db.ForeignKey('event.id'), nullable=False)
    registered_at = db.Column(db.DateTime, default=datetime.utcnow)
    poll_answers = db.Column(db.JSON)


class EventAttachment(db.Model):
    """Up to 5 downloadable files per event, uploaded by admin."""
    id          = db.Column(db.Integer, primary_key=True)
    event_id    = db.Column(db.Integer, db.ForeignKey('event.id'), nullable=False)
    filename    = db.Column(db.String(300), nullable=False)    # original filename shown to user
    stored_name = db.Column(db.String(300), nullable=False)    # UUID-based name on disk
    label       = db.Column(db.String(200))                     # optional friendly label
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)


class BlockedEmail(db.Model):
    """Emails that have unsubscribed and are NOT in our member DB.
    Future member imports will skip sending notifications to these addresses."""
    id         = db.Column(db.Integer, primary_key=True)
    email      = db.Column(db.String(120), unique=True, nullable=False)
    blocked_at = db.Column(db.DateTime, default=datetime.utcnow)


class InitiativeSendQueue(db.Model):
    """Holds auto-approved initiatives with quality score >= 4 that are ready
    to be broadcast to members. Admin can send them individually or all at once."""
    id             = db.Column(db.Integer, primary_key=True)
    initiative_id  = db.Column(db.Integer, db.ForeignKey('initiative.id'), nullable=False, unique=True)
    queued_at      = db.Column(db.DateTime, default=datetime.utcnow)
    sent_at        = db.Column(db.DateTime, nullable=True)

    initiative = db.relationship('Initiative', backref='send_queue_entry')


class PolicyDevelopment(db.Model):
    """A curated ECED-FLN policy news item sourced from a submitted URL.

    Workflow:
      1. Any authenticated member submits a URL (optionally hinting the country).
      2. Background job fetches the page HTML, asks Claude to extract:
           title, ECED-relevant body text, publication date, country, tags.
      3. Saved with is_published=False → appears in admin approval queue.
      4. Admin approves → is_published=True, auto-added to PolicySendQueue.
    """
    id                = db.Column(db.Integer, primary_key=True)
    source_url        = db.Column(db.String(2000), nullable=False)
    title             = db.Column(db.String(300),  nullable=True)
    extracted_text    = db.Column(db.Text,          nullable=True)
    short_summary     = db.Column(db.String(500),   nullable=True)
    country           = db.Column(db.String(100),   nullable=True)
    published_date    = db.Column(db.Date,          nullable=True)
    is_published      = db.Column(db.Boolean, default=False)
    processing_status = db.Column(db.String(50), default='pending')
    # 'pending' → being processed | 'ready' → AI done, awaiting admin | 'failed' → error
    processing_error  = db.Column(db.String(500), nullable=True)
    submitted_by      = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    created_at        = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at        = db.Column(db.DateTime, default=datetime.utcnow)

    submitter = db.relationship('User', foreign_keys=[submitted_by])
    tags      = db.relationship('Tag', secondary='policy_tags', backref='policy_developments')


policy_tags = db.Table('policy_tags',
    db.Column('policy_id', db.Integer, db.ForeignKey('policy_development.id'), primary_key=True),
    db.Column('tag_id',    db.Integer, db.ForeignKey('tag.id'),                primary_key=True)
)


class PolicySendQueue(db.Model):
    """Send queue for approved PolicyDevelopment items."""
    id        = db.Column(db.Integer, primary_key=True)
    policy_id = db.Column(db.Integer, db.ForeignKey('policy_development.id'),
                          nullable=False, unique=True)
    queued_at = db.Column(db.DateTime, default=datetime.utcnow)
    sent_at   = db.Column(db.DateTime, nullable=True)

    policy = db.relationship('PolicyDevelopment', backref='send_queue_entry')


# ===================== HELPER FUNCTIONS =====================

def get_setting(key, default=None):
    setting = Setting.query.filter_by(key=key).first()
    return setting.value if setting else default

def set_setting(key, value):
    setting = Setting.query.filter_by(key=key).first()
    if setting:
        setting.value = value
    else:
        setting = Setting(key=key, value=value)
        db.session.add(setting)
    db.session.commit()


def _enqueue_initiative(flask_app_or_none, initiative_id):
    """Add an initiative to the send queue if not already there and not already sent.
    Safe to call both inside and outside an app context (pass flask_app for bg threads)."""
    try:
        existing = InitiativeSendQueue.query.filter_by(initiative_id=initiative_id).first()
        if not existing:
            db.session.add(InitiativeSendQueue(initiative_id=initiative_id))
            db.session.commit()
    except Exception as e:
        if flask_app_or_none:
            flask_app_or_none.logger.error(f"_enqueue_initiative error: {e}")
        else:
            app.logger.error(f"_enqueue_initiative error: {e}")

def _process_policy_async(flask_app, policy_id):
    """Fetch the article URL via Firecrawl, extract ECED content with Claude, populate the record."""
    with flask_app.app_context():
        policy = PolicyDevelopment.query.get(policy_id)
        if not policy:
            return

        # ── Step 1: Scrape with Firecrawl ──────────────────────────────────
        # Firecrawl handles JS rendering, bot-blocking, and returns clean markdown.
        try:
            from firecrawl import Firecrawl as _FC
            fc = _FC(api_key=os.environ.get('FIRECRAWL_API_KEY', ''))
            result = fc.scrape(
                policy.source_url,
                formats=['markdown']
            )
            # Result is a Document object — access markdown as an attribute
            raw_text = (getattr(result, 'markdown', None) or '').strip()[:12000]
            if not raw_text:
                raise ValueError('Firecrawl returned empty content')
        except Exception as e:
            policy.processing_status = 'failed'
            policy.processing_error  = f'Fetch error: {str(e)[:200]}'
            db.session.commit()
            flask_app.logger.error(f'PolicyDev fetch error (id={policy_id}): {e}')
            return

        # ── Step 2: AI extraction via NVIDIA ──────────────────────────────
        try:
            from utils.ai_services import call_nvidia_api

            prompt = f"""You are an expert in African early-childhood education policy.

Below is text scraped from a news article at this URL: {policy.source_url}

Return ONLY a JSON object (no markdown fences, no preamble) with these keys:
{{
  "title": "<headline of the article>",
  "country": "<African country this policy development is about, or null>",
  "published_date": "<ISO date YYYY-MM-DD if found, or null>",
  "eced_relevant_text": "<the portion(s) of the article most relevant to ECED / FLN / foundational learning policy — up to 800 words, or null if none>",
  "short_summary": "<1-2 sentence plain-English summary of what the policy development is>",
  "tags": ["<tag1>", "<tag2>", ...]
}}

Rules:
- tags should be 3-8 specific ECED/education-policy keywords (e.g. 'early childhood policy', 'foundational literacy', 'pre-primary enrolment').
- If the article has no meaningful ECED/FLN content, set eced_relevant_text to null.
- Return ONLY the JSON object — no explanation, no markdown.

ARTICLE TEXT:
{raw_text}"""

            response_text = call_nvidia_api(prompt, max_tokens=1500, temperature=0.1)
            clean = response_text.strip().replace('```json', '').replace('```', '').strip()
            data = json.loads(clean)

        except Exception as e:
            policy.processing_status = 'failed'
            policy.processing_error  = f'AI extraction error: {str(e)[:200]}'
            db.session.commit()
            flask_app.logger.error(f'PolicyDev AI error (id={policy_id}): {e}')
            return

        # ── Step 3: Persist extracted fields ───────────────────────────────
        try:
            policy.title          = (data.get('title') or '')[:300] or policy.source_url[:300]
            policy.extracted_text = data.get('eced_relevant_text') or ''
            policy.short_summary  = (data.get('short_summary') or '')[:500]
            if not policy.country:
                policy.country    = (data.get('country') or '')[:100] or None

            raw_date = data.get('published_date')
            if raw_date:
                try:
                    from datetime import date as _date
                    policy.published_date = _date.fromisoformat(raw_date)
                except Exception:
                    pass

            raw_tags = data.get('tags', [])
            if isinstance(raw_tags, list):
                for tag_name in raw_tags[:8]:
                    tag_name = str(tag_name).strip()[:100]
                    if not tag_name:
                        continue
                    tag = Tag.query.filter_by(name=tag_name).first()
                    if not tag:
                        tag = Tag(name=tag_name, is_vetted=True)
                        db.session.add(tag)
                        db.session.flush()
                    if tag not in policy.tags:
                        policy.tags.append(tag)
                        tag.usage_count = (tag.usage_count or 0) + 1

            policy.processing_status = 'ready'
            policy.updated_at        = datetime.utcnow()
            db.session.commit()

        except Exception as e:
            policy.processing_status = 'failed'
            policy.processing_error  = f'DB save error: {str(e)[:200]}'
            db.session.commit()
            flask_app.logger.error(f'PolicyDev save error (id={policy_id}): {e}')


ALLOWED_ATTACHMENT_EXTENSIONS = {
    'pdf', 'doc', 'docx', 'xls', 'xlsx',
    'ppt', 'pptx', 'txt', 'zip', 'png',
    'jpg', 'jpeg', 'gif', 'mp4', 'mp3',
}

def allowed_attachment(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_ATTACHMENT_EXTENSIONS

def save_attachment(file_obj):
    """Save an uploaded attachment to UPLOAD_FOLDER/event_attachments/.
    Returns (original_filename, stored_name) tuple."""
    original = secure_filename(file_obj.filename)
    ext      = original.rsplit('.', 1)[1].lower() if '.' in original else 'bin'
    stored   = f"{uuid.uuid4().hex}.{ext}"
    folder   = os.path.join(app.config['UPLOAD_FOLDER'], 'event_attachments')
    os.makedirs(folder, exist_ok=True)
    file_obj.save(os.path.join(folder, stored))
    return original, stored

# ===================== POINTS SYSTEM =====================
# Points weights
POINTS = {
    'recommendation_posted': 10,   # posting a recommendation
    'recommendation_upvote': 5,    # someone upvotes your recommendation
    'recommendation_downvote': -2, # someone downvotes your recommendation
    'initiative_published': 20,    # initiative gets approved and published
    'question_published': 10,      # question gets approved and published
    'event_registered': 5,         # registering for an event
    'project_participated': 15,    # participating in a project activity
}

def award_points(user, activity=None, commit=True):
    """Recalculate a user's total points based on their actual database history."""
    # 1. Resolve Flask-Login's LocalProxy to the actual User model instance
    if hasattr(user, '_get_current_object'):
        user = user._get_current_object()
        
    total_points = 0
    
    # +20: Published Initiatives
    total_points += Initiative.query.filter_by(user_id=user.id, is_published=True).count() * 20
    
    # +10: Published Questions
    total_points += Question.query.filter_by(user_id=user.id, is_published=True).count() * 10
    
    # +10: Posted Recommendations
    total_points += Recommendation.query.filter_by(user_id=user.id).count() * 10
    
    # +15: Project Participations
    total_points += ProjectParticipation.query.filter_by(user_id=user.id).count() * 15
    
    # +5: Event Registrations
    total_points += EventRegistration.query.filter_by(user_id=user.id).count() * 5
    
    # +5 / -2: Upvotes and Downvotes on their Recommendations
    upvotes = Vote.query.join(Recommendation).filter(
        Recommendation.user_id == user.id, 
        Vote.vote_type == 1
    ).count()
    
    downvotes = Vote.query.join(Recommendation).filter(
        Recommendation.user_id == user.id, 
        Vote.vote_type == -1
    ).count()
    
    total_points += (upvotes * 5)
    total_points += (downvotes * -2)
    
    # 2. Update the user's score with the undeniable truth from the DB
    user.points = total_points
    db.session.add(user)
    
    if commit:
        db.session.commit()

# ===================== USER LOADER =====================

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ===================== PUBLIC ROUTES =====================

@app.route('/')
def index():
    from sqlalchemy import func, distinct
    org_count = db.session.query(
        func.count(distinct(func.lower(func.trim(User.organization))))
    ).filter(
        User.is_approved == True,
        User.organization != None,
        func.trim(User.organization) != ''
    ).scalar()
    stats = {
        'total_members': User.query.filter_by(is_approved=True).count(),
        'total_initiatives': Initiative.query.filter_by(is_published=True).count(),
        'total_organizations': org_count or 0,
        'stakeholders': {}
    }
    
    for stype in ['Government', 'NGO / Civil Society', 'Development Partner / Donor', 
                  'Academic / Research', 'UN Agency', 'Private Sector']:
        stats['stakeholders'][stype] = User.query.filter_by(
            stakeholder_type=stype, is_approved=True
        ).count()
    
    # Get recent initiatives with short_description
    recent_initiatives = Initiative.query.filter_by(is_published=True).order_by(
        Initiative.created_at.desc()
    ).limit(6).all()
    
    return render_template('index.html', stats=stats, initiatives=recent_initiatives)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        user = User.query.filter_by(email=email).first()
        
        if not user or not user.is_approved:
            flash('Email not found or account pending approval.', 'error')
            return redirect(url_for('login'))
            
        # Admin uses password + OTP (OTP can be disabled via ADMIN_OTP_ENABLED=false)
        if user.is_admin:
            from werkzeug.security import check_password_hash
            password = request.form.get('password')
            if not password:
                return render_template('login.html', show_password=True, email=email)
            if not user.password_hash or not check_password_hash(user.password_hash, password):
                flash('Invalid password.', 'error')
                return redirect(url_for('login'))

            # If OTP is disabled, log the admin in directly after password check
            if not app.config.get('ADMIN_OTP_ENABLED', True):
                login_user(user)
                flash('Welcome back!', 'success')
                return redirect(url_for('dashboard'))

            # Password correct — send OTP to ADMIN_OTP_EMAIL if configured,
            # otherwise fall back to the admin's own email.
            # ADMIN_OTP_EMAIL avoids the Brevo self-send block when the admin
            # address matches the platform sender address.
            otp = ''.join(random.choices(string.digits, k=6))
            user.otp = otp
            user.otp_expiry = datetime.utcnow() + timedelta(minutes=10)
            db.session.commit()

            otp_dest = app.config.get('ADMIN_OTP_EMAIL') or user.email
            send_otp_email(otp_dest, otp)
            flash(f'Password accepted. OTP sent to {otp_dest}.', 'info')
            return redirect(url_for('verify_otp', email=email))
        
        # Regular users get OTP
        otp = ''.join(random.choices(string.digits, k=6))
        user.otp = otp
        user.otp_expiry = datetime.utcnow() + timedelta(minutes=10)
        db.session.commit()
        
        send_otp_email(user.email, otp)
        flash('OTP sent to your email.', 'info')
        return redirect(url_for('verify_otp', email=email))
    
    return render_template('login.html')

@app.route('/verify-otp', methods=['GET', 'POST'])
def verify_otp():
    email = request.args.get('email') or request.form.get('email')
    user = User.query.filter_by(email=email).first()
    
    if not user:
        flash('User not found.', 'error')
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        otp = request.form.get('otp')
        
        if user.otp == otp and user.otp_expiry > datetime.utcnow():
            user.otp = None
            user.otp_expiry = None
            db.session.commit()
            login_user(user)
            flash('Welcome back!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid or expired OTP.', 'error')
    
    return render_template('verify_otp.html', email=email)
    
@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    custom_fields = RegistrationField.query.filter_by(is_active=True).order_by(RegistrationField.order).all()
    
    if request.method == 'POST':
        email = request.form.get('email', '').lower().strip()
        
        if User.query.filter_by(email=email).first():
            flash('Email already registered.', 'error')
            return redirect(url_for('register'))
        
        # Validate initiative fields
        initiative_title = request.form.get('initiative_title', '').strip()
        initiative_short_desc = request.form.get('initiative_short_description', '').strip()
        initiative_content = request.form.get('initiative_content', '').strip()

        if not initiative_title:
            flash('Please provide an initiative title.', 'error')
            return redirect(url_for('register'))
        if not initiative_content:
            flash('Please provide initiative content.', 'error')
            return redirect(url_for('register'))

        # Auto-approve new registrations immediately
        user = User(
            email=email,
            name=request.form.get('name'),
            organization=request.form.get('organization'),
            stakeholder_type=request.form.get('stakeholder_type'),
            country=request.form.get('country'),
            is_approved=True,
            is_admin=False
        )
        db.session.add(user)
        db.session.commit()

        # Build a unique slug for the initiative
        slug = re.sub(r'[^\w]+', '-', initiative_title.lower()).strip('-')[:190]
        base_slug = slug
        counter = 1
        while Initiative.query.filter_by(slug=slug).first():
            slug = f"{base_slug}-{counter}"
            counter += 1

        initiative = Initiative(
            title=initiative_title[:200],
            slug=slug,
            content=initiative_content,
            short_description=initiative_short_desc[:300] if initiative_short_desc else None,
            user_id=user.id,
            stakeholder_type=user.stakeholder_type,
            country=user.country,
            is_published=True   # Auto-published on registration
        )
        db.session.add(initiative)
        db.session.commit()

        # Award points for the published initiative
        award_points(user, 'initiative_published')

        # Send welcome/approval email
        try:
            send_approval_email(user.email, initiative.slug)
        except Exception as e:
            app.logger.error(f"Registration welcome email error: {e}")

        # Auto-register new member for the next upcoming published event (if any)
        now = datetime.utcnow()
        next_event = Event.query.filter(
            Event.start_date >= now,
            Event.is_published == True
        ).order_by(Event.start_date).first()

        if next_event:
            existing_reg = EventRegistration.query.filter_by(
                event_id=next_event.id, user_id=user.id
            ).first()
            if not existing_reg:
                reg = EventRegistration(
                    user_id=user.id,
                    event_id=next_event.id,
                    poll_answers={},
                )
                db.session.add(reg)
                db.session.commit()
                award_points(user, 'event_registered')
                # Register on Zoom if applicable
                if next_event.zoom_webinar_id:
                    try:
                        register_user_for_webinar(next_event.zoom_webinar_id, user)
                    except Exception as e:
                        app.logger.error(f"Auto event Zoom registration error for new user {user.id}: {e}")
                # Send confirmation email
                try:
                    send_event_registration_confirmation(user, next_event)
                except Exception as e:
                    app.logger.error(f"Auto event confirmation email error for new user {user.id}: {e}")
                # Store event info in session so dashboard can show the flash
                session['new_member_event_title'] = next_event.title
                session['new_member_event_id'] = next_event.id

        # Extract and vet tags + score quality in a background thread
        # Also adds to send queue if score >= 4 once scored
        def _process_tags_async(flask_app, initiative_id, content, title, short_desc):
            with flask_app.app_context():
                try:
                    phrases = extract_noun_phrases(content)
                    vetted_tags = vet_tags_nvidia(phrases)
                    ini = Initiative.query.get(initiative_id)
                    for tag_name in vetted_tags:
                        tag = Tag.query.filter_by(name=tag_name).first()
                        if not tag:
                            tag = Tag(name=tag_name, is_vetted=True)
                            db.session.add(tag)
                            db.session.flush()
                        ini.tags.append(tag)
                        tag.usage_count += 1
                    db.session.commit()
                    update_noun_phrase_db(initiative_id, phrases)
                except Exception as e:
                    flask_app.logger.error(f"Registration initiative tag processing error: {e}")
                # Score quality; if >= 4 add to send queue
                try:
                    from utils.ai_services import score_initiative_quality, detect_language
                    score = score_initiative_quality(title, content, short_desc or "")
                    if score is not None:
                        ini = Initiative.query.get(initiative_id)
                        if ini:
                            ini.quality_score = score
                            db.session.commit()
                            if score >= 4:
                                _enqueue_initiative(flask_app, initiative_id)
                except Exception as e:
                    flask_app.logger.error(f"Registration initiative quality scoring error: {e}")
                # Detect language
                try:
                    from utils.ai_services import detect_language
                    lang = detect_language(title, content)
                    if lang:
                        ini = Initiative.query.get(initiative_id)
                        if ini and not ini.detected_lang:
                            ini.detected_lang = lang
                            db.session.commit()
                except Exception as e:
                    flask_app.logger.error(f"Registration initiative language detection error: {e}")

        t = threading.Thread(
            target=_process_tags_async,
            args=(app, initiative.id, initiative_content, initiative_title, initiative_short_desc),
            daemon=True
        )
        t.start()

        flash(
            'Welcome! Your account has been created and you can now log in.',
            'success'
        )
        return redirect(url_for('login'))
    
    stakeholder_types = ['Government', 'NGO / Civil Society', 'Development Partner / Donor', 
                        'Academic / Research', 'UN Agency', 'Private Sector']
    
    return render_template('register.html', stakeholder_types=stakeholder_types, custom_fields=custom_fields)

@app.route('/profile/edit', methods=['GET', 'POST'])
@login_required
def edit_profile():
    if request.method == 'POST':
        new_name = request.form.get('name', '').strip()
        new_email = request.form.get('email', '').lower().strip()
        new_organization = request.form.get('organization', '').strip()
        projects = request.form.getlist('project[]')
        projects = [p.strip() for p in projects if p.strip()]

        errors = []
        if not new_name:
            errors.append('Name is required.')
        if not new_email:
            errors.append('Email is required.')
        if not new_organization:
            errors.append('Organization is required.')
        if len(projects) == 0:
            errors.append('At least one project is required.')

        # Check email uniqueness (allow keeping same email)
        if new_email and new_email != current_user.email:
            if User.query.filter_by(email=new_email).first():
                errors.append('That email address is already in use by another account.')

        if errors:
            for e in errors:
                flash(e, 'error')
        else:
            current_user.name = new_name
            current_user.email = new_email
            current_user.organization = new_organization
            MemberProject.query.filter_by(user_id=current_user.id).delete()
            for desc in projects:
                proj = MemberProject(user_id=current_user.id, description=desc[:300])
                db.session.add(proj)
            db.session.commit()
            flash('Profile updated successfully.', 'success')
            next_page = request.args.get('next') or url_for('dashboard')
            return redirect(next_page)

    current_projects = [p.description for p in current_user.member_projects.all()]
    return render_template('profile_edit.html', projects=current_projects)

@app.route('/search_members')
@login_required
def search_members():
    query = request.args.get('q', '')
    if not query:
        return render_template('search_members.html', results=[])
    
    # Only search members who have at least one published initiative
    users = User.query.filter(
        User.is_approved == True,
        User.initiatives.any(Initiative.is_published == True)
    ).all()

    if not users:
        return render_template('search_members.html', query=query, results=[])
    
    # Prepare data for AI ranking using initiative titles + short descriptions
    user_data = []
    for u in users:
        published = [i for i in u.initiatives if i.is_published]
        if published:
            descriptions = [
                f"{i.title}: {i.short_description or ''}" for i in published[:5]
            ]
            user_data.append({'id': u.id, 'projects': descriptions})
    
    ranked_ids = rank_members_by_query(query, user_data)
    
    user_map = {u.id: u for u in users}
    ranked_users = [user_map[uid] for uid in ranked_ids if uid in user_map]
    
    return render_template('search_members.html', query=query, results=ranked_users)
    
@app.route('/test-email')
def test_email():
    from utils.email_sender import send_email
    result = send_email(
        to_email='cluster@eced-au.org',  # send to yourself
        subject='Test email from platform',
        html_content='<p>If you see this, Brevo is working.</p>'
    )
    return f"Result: {result}"

@app.route('/dashboard')
@login_required
def dashboard():
    user_initiatives = Initiative.query.filter_by(
        user_id=current_user.id
    ).order_by(Initiative.created_at.desc()).all()

    participations = ProjectParticipation.query.filter_by(user_id=current_user.id).all()
    project_ids    = list({p.project_id for p in participations})
    user_projects  = Project.query.filter(Project.id.in_(project_ids)).all() \
                     if project_ids else []

    submitted_projects = Project.query.filter_by(
        submitted_by=current_user.id
    ).order_by(Project.created_at.desc()).all()

    event_registrations  = EventRegistration.query.filter_by(user_id=current_user.id).all()
    registered_event_ids = [r.event_id for r in event_registrations]
    registered_events    = Event.query.filter(
        Event.id.in_(registered_event_ids),
        Event.is_published == True,
    ).order_by(Event.start_date.desc()).all() if registered_event_ids else []

    return render_template(
        'dashboard.html',
        initiatives=user_initiatives,
        projects=user_projects,
        submitted_projects=submitted_projects,
        registered_events=registered_events,
        now=datetime.utcnow(),
        new_event_title=session.pop('new_member_event_title', None),
        new_event_id=session.pop('new_member_event_id', None),
    )

@app.route('/initiative/new', methods=['GET', 'POST'])
@login_required
def new_initiative():
    if request.method == 'POST':
        title = request.form.get('title')
        short_description = request.form.get('short_description')
        content = request.form.get('content')                     # MARKDOWN CHANGE: raw content

        # Duplicate guard: same title by the same user already exists
        existing = Initiative.query.filter(
            Initiative.user_id == current_user.id,
            db.func.lower(Initiative.title) == title.lower().strip()
        ).first()
        if existing:
            flash('You have already submitted an initiative with this title. Please use a different title or edit your existing one.', 'warning')
            return render_template('article_form.html', initiative=None)
        
        slug = re.sub(r'[^\w]+', '-', title.lower()).strip('-')
        base_slug = slug
        counter = 1
        while Initiative.query.filter_by(slug=slug).first():
            slug = f"{base_slug}-{counter}"
            counter += 1
        
        initiative = Initiative(
            title=title,
            slug=slug,
            content=content,                                     # MARKDOWN CHANGE: no bleach.clean
            short_description=short_description[:300] if short_description else None,
            user_id=current_user.id,
            stakeholder_type=current_user.stakeholder_type,
            country=current_user.country,
            is_published=True   # Auto-published for existing approved members
        )
        
        db.session.add(initiative)
        db.session.commit()

        # Award points immediately since it's auto-published
        award_points(current_user, 'initiative_published')

        # Score content quality in background; if >= 4, add to send queue
        def _score_async(flask_app, initiative_id, title, content, short_desc):
            with flask_app.app_context():
                try:
                    from utils.ai_services import score_initiative_quality
                    score = score_initiative_quality(title, content, short_desc or "")
                    if score is not None:
                        ini = Initiative.query.get(initiative_id)
                        if ini:
                            ini.quality_score = score
                            db.session.commit()
                            if score >= 4:
                                _enqueue_initiative(flask_app, initiative_id)
                except Exception as e:
                    flask_app.logger.error(f"Quality scoring error (initiative {initiative_id}): {e}")
                # Detect language
                try:
                    from utils.ai_services import detect_language
                    lang = detect_language(title, content)
                    if lang:
                        ini = Initiative.query.get(initiative_id)
                        if ini and not ini.detected_lang:
                            ini.detected_lang = lang
                            db.session.commit()
                except Exception as e:
                    flask_app.logger.error(f"Language detection error (initiative {initiative_id}): {e}")

        threading.Thread(
            target=_score_async,
            args=(app, initiative.id, title, content, short_description),
            daemon=True
        ).start()

        # Process tags (extract noun phrases and vet with AI)
        try:
            phrases = extract_noun_phrases(content)
            vetted_tags = vet_tags_nvidia(phrases)
            
            for tag_name in vetted_tags:
                tag = Tag.query.filter_by(name=tag_name).first()
                if not tag:
                    tag = Tag(name=tag_name, is_vetted=True)
                    db.session.add(tag)
                    db.session.flush()
                initiative.tags.append(tag)
                tag.usage_count += 1
            
            db.session.commit()
            update_noun_phrase_db(initiative.id, phrases)
            
        except Exception as e:
            app.logger.error(f"Tag processing error: {e}")
        
        flash('Initiative published successfully.', 'success')
        return redirect(url_for('dashboard'))
    
    return render_template('article_form.html', initiative=None)

@app.route('/admin/initiative/<int:id>/delete', methods=['POST'])
@login_required
def admin_delete_initiative(id):
    if not current_user.is_admin:
        abort(403)
    initiative = Initiative.query.get_or_404(id)
    title = initiative.title

    # Remove tag associations first
    initiative.tags = []
    db.session.flush()

    # Delete send queue entry (NOT NULL FK — must go before the initiative row)
    InitiativeSendQueue.query.filter_by(initiative_id=id).delete()

    # Delete related noun phrases
    NounPhrase.query.filter_by(initiative_id=id).delete()

    # Delete the initiative
    db.session.delete(initiative)
    db.session.commit()

    flash(f'Initiative "{title}" has been deleted.', 'success')
    return redirect(url_for('admin_approvals', type='initiatives'))

@app.route('/admin/project/<int:id>/delete', methods=['POST'])
@login_required
def admin_delete_project(id):
    if not current_user.is_admin:
        abort(403)
    project = Project.query.get_or_404(id)
    title = project.title
    # Cascade handles activities and participations via the relationship
    db.session.delete(project)
    db.session.commit()
    flash(f'Project "{title}" has been deleted.', 'success')
    return redirect(url_for('admin_approvals', type='projects'))

@app.route('/initiative/<slug>')
def view_initiative(slug):
    initiative = Initiative.query.filter_by(slug=slug, is_published=True).first_or_404()
    # Increment view count, skip for the initiative's own author to avoid self-inflation
    if not current_user.is_authenticated or current_user.id != initiative.user_id:
        initiative.view_count = (initiative.view_count or 0) + 1
        db.session.commit()

    # Approved comments
    comments = Comment.query.filter_by(
        initiative_id=initiative.id, is_approved=True
    ).order_by(Comment.created_at.asc()).all()

    # Related initiatives: same tags, excluding self
    related = []
    if initiative.tags:
        tag_ids = [t.id for t in initiative.tags]
        related = (Initiative.query
                   .join(Initiative.tags)
                   .filter(
                       Tag.id.in_(tag_ids),
                       Initiative.id != initiative.id,
                       Initiative.is_published == True
                   )
                   .distinct()
                   .order_by(Initiative.created_at.desc())
                   .limit(4)
                   .all())
    # Fallback: recent initiatives if no tag matches
    if not related:
        related = (Initiative.query
                   .filter(Initiative.id != initiative.id, Initiative.is_published == True)
                   .order_by(Initiative.created_at.desc())
                   .limit(4)
                   .all())

    return render_template('article.html', initiative=initiative,
                           comments=comments, related=related)

# ===================== COMMENT ROUTES =====================

@app.route('/initiative/<slug>/comment', methods=['POST'])
@login_required
def post_comment(slug):
    initiative = Initiative.query.filter_by(slug=slug, is_published=True).first_or_404()
    content = request.form.get('content', '').strip()
    if not content:
        flash('Comment cannot be empty.', 'error')
        return redirect(url_for('view_initiative', slug=slug))
    if len(content) > 2000:
        flash('Comment is too long (max 2000 characters).', 'error')
        return redirect(url_for('view_initiative', slug=slug))

    comment = Comment(
        initiative_id=initiative.id,
        user_id=current_user.id,
        content=bleach.clean(content),
        is_approved=False
    )
    db.session.add(comment)
    db.session.commit()
    flash('Your comment has been submitted and is awaiting approval.', 'success')
    return redirect(url_for('view_initiative', slug=slug))


@app.route('/admin/comment/<int:id>/approve', methods=['POST'])
@login_required
def admin_approve_comment(id):
    if not current_user.is_admin:
        abort(403)
    comment = Comment.query.get_or_404(id)
    comment.is_approved = True
    db.session.commit()
    flash('Comment approved.', 'success')
    return redirect(request.referrer or url_for('admin_approvals', type='comments'))


@app.route('/admin/comment/<int:id>/delete', methods=['POST'])
@login_required
def admin_delete_comment(id):
    if not current_user.is_admin:
        abort(403)
    comment = Comment.query.get_or_404(id)
    db.session.delete(comment)
    db.session.commit()
    flash('Comment deleted.', 'success')
    return redirect(request.referrer or url_for('admin_approvals', type='comments'))

# ===================== DISCUSSIONS (NEWSFEED) =====================

@app.route('/discussions')
def discussions():
    """Newsfeed-style view of all published initiatives, sorted by latest comment activity."""
    page = request.args.get('page', 1, type=int)
    per_page = 15

    from sqlalchemy import func

    latest_comment_sq = (
        db.session.query(
            Comment.initiative_id,
            func.max(Comment.created_at).label('latest_comment_at')
        )
        .filter(Comment.is_approved == True)
        .group_by(Comment.initiative_id)
        .subquery()
    )

    initiatives_q = (
        Initiative.query
        .outerjoin(latest_comment_sq, Initiative.id == latest_comment_sq.c.initiative_id)
        .filter(Initiative.is_published == True)
        .order_by(
            db.func.coalesce(latest_comment_sq.c.latest_comment_at, Initiative.created_at).desc()
        )
    )

    pagination = initiatives_q.paginate(page=page, per_page=per_page, error_out=False)
    initiatives = pagination.items

    feed_items = []
    for ini in initiatives:
        approved_comments = Comment.query.filter_by(
            initiative_id=ini.id, is_approved=True
        ).order_by(Comment.created_at.desc()).limit(3).all()
        comment_count = Comment.query.filter_by(
            initiative_id=ini.id, is_approved=True
        ).count()
        feed_items.append({
            'initiative': ini,
            'comments': list(reversed(approved_comments)),
            'comment_count': comment_count,
        })

    return render_template('discussions.html',
                           feed_items=feed_items,
                           pagination=pagination)
    
@app.route('/admin/initiatives')
@login_required
def admin_initiatives():
    if not current_user.is_admin:
        abort(403)
    
    filter_type = request.args.get('filter', 'all')
    score_filter = request.args.get('score', '')

    query = Initiative.query

    if filter_type == 'published':
        query = query.filter_by(is_published=True)
    elif filter_type == 'pending':
        query = query.filter_by(is_published=False)

    if score_filter == 'unscored':
        query = query.filter(Initiative.quality_score == None)
    elif score_filter and score_filter.isdigit():
        query = query.filter(Initiative.quality_score == int(score_filter))

    initiatives = query.order_by(Initiative.created_at.desc()).all()

    return render_template('admin/initiatives.html',
                         initiatives=initiatives,
                         current_filter=filter_type,
                         score_filter=score_filter)


@app.route('/admin/initiative/<int:id>/rescore', methods=['POST'])
@login_required
def admin_rescore_initiative(id):
    """Trigger a fresh AI quality score for a single initiative."""
    if not current_user.is_admin:
        abort(403)
    initiative = Initiative.query.get_or_404(id)

    def _rescore(flask_app, initiative_id, title, content, short_desc):
        with flask_app.app_context():
            try:
                score = score_initiative_quality(title, content, short_desc or "")
                if score is not None:
                    ini = Initiative.query.get(initiative_id)
                    if ini:
                        ini.quality_score = score
                        db.session.commit()
            except Exception as e:
                flask_app.logger.error(f"Manual rescore error (initiative {initiative_id}): {e}")

    threading.Thread(
        target=_rescore,
        args=(app, initiative.id, initiative.title, initiative.content, initiative.short_description),
        daemon=True
    ).start()

    flash(f'Re-scoring "{initiative.title}" in the background — refresh in a moment.', 'info')
    return redirect(request.referrer or url_for('admin_initiatives'))

@app.route('/initiative/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit_initiative(id):
    initiative = Initiative.query.get_or_404(id)
    
    if initiative.user_id != current_user.id and not current_user.is_admin:
        abort(403)
    
    if request.method == 'POST':
        # OTP verification removed for simplicity; admin can edit directly
        if not current_user.is_admin:
            # Optionally verify OTP (implement if needed)
            pass
        
        initiative.title = request.form.get('title')
        initiative.short_description = request.form.get('short_description')[:300] if request.form.get('short_description') else None
        initiative.content = request.form.get('content')                     # MARKDOWN CHANGE: raw content
        initiative.updated_at = datetime.utcnow()
        
        # Optionally reprocess tags if content changed
        if request.form.get('regenerate_tags'):
            try:
                phrases = extract_noun_phrases(initiative.content)
                vetted_tags = vet_tags_nvidia(phrases)
                # Clear existing tags and add new
                initiative.tags = []
                for tag_name in vetted_tags:
                    tag = Tag.query.filter_by(name=tag_name).first()
                    if not tag:
                        tag = Tag(name=tag_name, is_vetted=True)
                        db.session.add(tag)
                        db.session.flush()
                    initiative.tags.append(tag)
                    tag.usage_count += 1
                update_noun_phrase_db(initiative.id, phrases)
            except Exception as e:
                app.logger.error(f"Tag regeneration error: {e}")
        
        db.session.commit()
        flash('Initiative updated.', 'success')
        return redirect(url_for('view_initiative', slug=initiative.slug))
    
    return render_template('article_form.html', initiative=initiative)

@app.route('/search')
def search():
    # NEW: Use tag filter instead of text search
    tag_name = request.args.get('tag', '')
    initiatives = Initiative.query.filter_by(is_published=True)
    
    if tag_name:
        tag = Tag.query.filter_by(name=tag_name).first()
        if tag:
            initiatives = initiatives.filter(Initiative.tags.contains(tag))
    
    initiatives = initiatives.order_by(Initiative.created_at.desc()).all()
    tags = Tag.query.order_by(Tag.name).all()  # for dropdown
    
    return render_template('search.html', initiatives=initiatives, tags=tags, selected_tag=tag_name)

@app.route('/tags/<tag_name>')
def tag_view(tag_name):
    tag = Tag.query.filter_by(name=tag_name).first_or_404()
    initiatives = Initiative.query.join(Initiative.tags).filter(
        Tag.id == tag.id,
        Initiative.is_published == True
    ).order_by(Initiative.created_at.desc()).all()
    
    tags = Tag.query.order_by(Tag.name).all()
    return render_template('search.html', initiatives=initiatives, tags=tags, selected_tag=tag_name)

# ===================== FORUM ROUTES =====================

@app.route('/forum')
def forum():
    questions = Question.query.filter_by(is_published=True).order_by(Question.created_at.desc()).all()
    return render_template('forum.html', questions=questions)

@app.route('/forum/question/new', methods=['GET', 'POST'])
@login_required
def new_question():
    if request.method == 'POST':
        question = Question(
            title=request.form.get('title'),
            content=bleach.clean(request.form.get('content')),
            user_id=current_user.id,
            is_published=False
        )
        db.session.add(question)
        db.session.commit()
        flash('Your question has been submitted for approval.', 'success')
        return redirect(url_for('forum'))
    return render_template('question_form.html', question=None)

@app.route('/forum/question/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit_question(id):
    question = Question.query.get_or_404(id)
    if question.user_id != current_user.id and not current_user.is_admin:
        abort(403)
    if request.method == 'POST':
        question.title = request.form.get('title')
        question.content = bleach.clean(request.form.get('content'))
        db.session.commit()
        flash('Question updated.', 'success')
        return redirect(url_for('view_question', id=id))
    return render_template('question_form.html', question=question)

@app.route('/forum/<int:id>')
def view_question(id):
    question = Question.query.get_or_404(id)
    recommendations = Recommendation.query.filter_by(question_id=id).order_by(
        Recommendation.score.desc(),
        Recommendation.created_at.desc()
    ).all()
    return render_template('question.html', question=question, recommendations=recommendations)

@app.route('/forum/question/<int:id>/recommendation', methods=['POST'])
@login_required
def add_recommendation(id):
    question = Question.query.get_or_404(id)
    if not question.is_published:
        return redirect(url_for('view_question', id=id))
    content = bleach.clean(request.form.get('content'))
    if not content:
        flash('Please provide a recommendation.', 'error')
        return redirect(url_for('view_question', id=id))
    recommendation = Recommendation(
        content=content,
        user_id=current_user.id,
        question_id=id,
        score=0
    )
    db.session.add(recommendation)
    db.session.commit()
    award_points(current_user, 'recommendation_posted')
    flash('Your recommendation has been posted.', 'success')
    return redirect(url_for('view_question', id=id))

@app.route('/forum/recommendation/<int:id>/vote', methods=['POST'])
@login_required
def vote_recommendation(id):
    recommendation = Recommendation.query.get_or_404(id)
    vote_type = int(request.form.get('vote_type'))
    existing_vote = Vote.query.filter_by(
        user_id=current_user.id,
        recommendation_id=id
    ).first()
    if existing_vote:
        existing_vote.vote_type = vote_type
    else:
        vote = Vote(
            user_id=current_user.id,
            recommendation_id=id,
            vote_type=vote_type
        )
        db.session.add(vote)
    upvotes = Vote.query.filter_by(recommendation_id=id, vote_type=1).count()
    downvotes = Vote.query.filter_by(recommendation_id=id, vote_type=-1).count()
    recommendation.score = upvotes - downvotes
    # Award points to the recommendation author based on vote
    rec_author = User.query.get(recommendation.user_id)
    if rec_author:
        if vote_type == 1:
            award_points(rec_author, 'recommendation_upvote', commit=False)
        elif vote_type == -1:
            award_points(rec_author, 'recommendation_downvote', commit=False)
    db.session.commit()
    return jsonify({'success': True, 'score': recommendation.score})

# ===================== MEMBERS & LEADERBOARD =====================

@app.route('/members')
def members():
    type_filter = request.args.get('type', '').strip()
    query = db.session.query(
        User.organization,
        User.stakeholder_type,
        db.func.count(User.id).label('member_count')
    ).filter_by(is_approved=True)
    if type_filter:
        query = query.filter(User.stakeholder_type == type_filter)
    orgs = query.group_by(User.organization, User.stakeholder_type).all()
    return render_template('members.html', organizations=orgs, active_type=type_filter)

@app.route('/leaderboard')
def leaderboard():
    # Top individual members by points
    expert_stats = User.query.filter_by(is_approved=True, is_admin=False)\
        .order_by(User.points.desc())\
        .limit(10).all()

    # Top organisations by sum of member points
    org_stats = db.session.query(
        User.organization,
        User.stakeholder_type,
        db.func.sum(User.points).label('total_points'),
        db.func.count(User.id).label('member_count'),
        db.func.count(db.distinct(Initiative.id)).label('initiative_count'),
    ).select_from(User)\
    .outerjoin(Initiative, (User.id == Initiative.user_id) & (Initiative.is_published == True))\
    .filter(User.is_approved == True, User.is_admin == False)\
    .group_by(User.organization, User.stakeholder_type)\
    .order_by(db.desc('total_points'))\
    .limit(10).all()

    return render_template('leaderboard.html', org_stats=org_stats, expert_stats=expert_stats)

# ===================== EVENTS AND POLLS =====================

@app.route('/events')
def events():
    now      = datetime.utcnow()
    upcoming = Event.query.filter(
        Event.start_date >= now, Event.is_published == True
    ).order_by(Event.start_date).all()
    past     = Event.query.filter(
        Event.start_date < now, Event.is_published == True
    ).order_by(Event.start_date.desc()).all()
    return render_template('events.html', upcoming=upcoming, past=past, now=now)

@app.route('/event/<int:id>')
def event_detail(id):
    event             = Event.query.get_or_404(id)
    registered        = False
    user_poll_answers = None

    if current_user.is_authenticated:
        registration = EventRegistration.query.filter_by(
            event_id=id, user_id=current_user.id
        ).first()
        registered = registration is not None
        if registered:
            user_poll_answers = registration.poll_answers

    attachments = EventAttachment.query.filter_by(event_id=id).all()

    return render_template(
        'event_detail.html',
        event=event,
        registered=registered,
        user_poll_answers=user_poll_answers,
        attachments=attachments,
        now=datetime.utcnow(),
    )

@app.route('/event/<int:id>/register', methods=['GET', 'POST'])
@login_required
def event_register(id):
    event = Event.query.get_or_404(id)
    if event.start_date < datetime.utcnow():
        flash('This event has already passed.', 'error')
        return redirect(url_for('event_detail', id=id))

    existing = EventRegistration.query.filter_by(
        event_id=id, user_id=current_user.id
    ).first()
    if existing:
        flash('You are already registered for this event.', 'info')
        return redirect(url_for('event_detail', id=id))

    if request.method == 'POST':
        poll_answers = {}
        for poll in event.polls:
            selected = request.form.get(f'poll_{poll.id}')
            if selected:
                poll_answers[str(poll.id)] = selected

        registration = EventRegistration(
            user_id=current_user.id,
            event_id=id,
            poll_answers=poll_answers,
        )
        db.session.add(registration)
        db.session.commit()
        award_points(current_user, 'event_registered')

        # Register on Zoom Meeting — Zoom sends its own branded confirmation email
        if event.zoom_webinar_id:
            try:
                join_url = register_user_for_webinar(event.zoom_webinar_id, current_user)
                app.logger.info(
                    f"Zoom meeting registration OK: user={current_user.id} join_url={join_url}"
                )
            except Exception as e:
                app.logger.error(
                    f"Zoom meeting registration failed: user={current_user.id} event={id}: {e}"
                )
                try:
                    send_event_registration_confirmation(current_user, event)
                except Exception as email_err:
                    app.logger.error(f"Fallback confirmation email error: {email_err}")
                flash(
                    'Registered! Note: Zoom confirmation email may be delayed — '
                    'check your inbox shortly.',
                    'warning',
                )
                return redirect(url_for('event_detail', id=id))
        else:
            try:
                send_event_registration_confirmation(current_user, event)
            except Exception as e:
                app.logger.error(f"Confirmation email error: {e}")

        flash(
            'You have successfully registered! Check your email for the Zoom join link.',
            'success',
        )
        return redirect(url_for('event_detail', id=id))

    return render_template('event_register.html', event=event)


@app.route('/event/<int:id>/register-email', methods=['POST'])
def event_register_by_email(id):
    """Public registration endpoint: user provides their email, no login required.
    Registers them if their email is in the members database, then redirects to
    an optional polls page. If email is not found, shows a sign-up prompt."""
    event = Event.query.get_or_404(id)

    if event.start_date < datetime.utcnow():
        flash('This event has already passed.', 'error')
        return redirect(url_for('event_detail', id=id))

    email = request.form.get('email', '').lower().strip()
    if not email:
        flash('Please enter your email address.', 'error')
        return redirect(url_for('event_detail', id=id))

    user = User.query.filter_by(email=email, is_approved=True).first()
    if not user:
        # Email not in member database — prompt to sign up
        return render_template('event_detail.html',
            event=event,
            registered=False,
            user_poll_answers=None,
            attachments=EventAttachment.query.filter_by(event_id=id).all(),
            now=datetime.utcnow(),
            email_not_found=email,
        )

    existing = EventRegistration.query.filter_by(event_id=id, user_id=user.id).first()
    if existing:
        flash('This email is already registered for the event.', 'info')
        return redirect(url_for('event_polls', id=id, user_id=user.id))

    # Create registration (poll answers saved later on polls page)
    registration = EventRegistration(
        user_id=user.id,
        event_id=id,
        poll_answers={},
    )
    db.session.add(registration)
    db.session.commit()
    award_points(user, 'event_registered')

    # Send Zoom / confirmation email
    if event.zoom_webinar_id:
        try:
            register_user_for_webinar(event.zoom_webinar_id, user)
        except Exception as e:
            app.logger.error(f"Zoom registration failed for {email} event {id}: {e}")
            try:
                send_event_registration_confirmation(user, event)
            except Exception as email_err:
                app.logger.error(f"Fallback confirmation email error: {email_err}")
    else:
        try:
            send_event_registration_confirmation(user, event)
        except Exception as e:
            app.logger.error(f"Confirmation email error: {e}")

    # Redirect to polls page so user can optionally answer polls
    return redirect(url_for('event_polls', id=id, user_id=user.id))


@app.route('/event/<int:id>/polls/<int:user_id>', methods=['GET', 'POST'])
def event_polls(id, user_id):
    """Optional post-registration poll page. Shows polls for the event and
    lets the registrant answer them. Accessible without login."""
    event = Event.query.get_or_404(id)
    registration = EventRegistration.query.filter_by(
        event_id=id, user_id=user_id
    ).first_or_404()

    if request.method == 'POST':
        poll_answers = registration.poll_answers or {}
        for poll in event.polls:
            selected = request.form.get(f'poll_{poll.id}')
            if selected:
                poll_answers[str(poll.id)] = selected
        registration.poll_answers = poll_answers
        db.session.commit()
        flash('Thank you! Your responses have been saved.', 'success')
        return redirect(url_for('event_detail', id=id))

    polls = list(event.polls)
    return render_template(
        'event_polls.html',
        event=event,
        polls=polls,
        registration=registration,
    )

@app.route('/event/attachment/<int:att_id>')
def download_attachment(att_id):
    """Serve an event attachment file. Publicly accessible."""
    att    = EventAttachment.query.get_or_404(att_id)
    folder = os.path.join(app.config['UPLOAD_FOLDER'], 'event_attachments')
    return send_from_directory(
        folder, att.stored_name,
        as_attachment=True,
        download_name=att.filename,
    )

@app.route('/polls')
def polls():
    # Get all polls (from events)
    polls = Poll.query.all()
    # Get tags for filtering
    tags = PollTag.query.with_entities(PollTag.tag).distinct().all()
    tags = [t[0] for t in tags]
    
    selected_tag = request.args.get('tag')
    if selected_tag:
        polls = Poll.query.join(PollTag).filter(PollTag.tag == selected_tag)
    
    # For each poll, compute quick stats
    poll_stats = []
    for poll in polls:
        # Fetch all registrations that have poll_answers and filter in Python
        # to avoid the JSON LIKE operator incompatibility in PostgreSQL.
        registrations = EventRegistration.query.filter(
            EventRegistration.poll_answers.isnot(None)
        ).all()
        registrations = [r for r in registrations if r.poll_answers and str(poll.id) in r.poll_answers]
        answers = []
        for reg in registrations:
            if reg.poll_answers and str(poll.id) in reg.poll_answers:
                answers.append(reg.poll_answers[str(poll.id)])
        total = len(answers)
        option_counts = {}
        for opt in poll.options:
            opt_text = opt['text']
            count = answers.count(opt_text)
            if count > 0:
                option_counts[opt_text] = count
        poll_stats.append({
            'poll': poll,
            'total_responses': total,
            'option_counts': option_counts
        })
    
    return render_template('polls.html', polls=polls, poll_stats=poll_stats, tags=tags, selected_tag=selected_tag)

@app.route('/poll/<int:id>')
def poll_detail(id):
    poll = Poll.query.get_or_404(id)
    registrations = EventRegistration.query.all()
    responses = []
    for reg in registrations:
        if reg.poll_answers and str(poll.id) in reg.poll_answers:
            responses.append({
                'user': reg.user,
                'answer': reg.poll_answers[str(poll.id)],
                'country': reg.user.country
            })
    countries = list(set([r['country'] for r in responses]))
    options = [opt['text'] for opt in poll.options]
    data = []
    for opt in options:
        row = {'option': opt}
        for country in countries:
            row[country] = sum(1 for r in responses if r['answer'] == opt and r['country'] == country)
        data.append(row)
    
    return render_template('poll_detail.html', poll=poll, data=data, countries=countries, options=options, responses=responses)

# ===================== ADMIN ROUTES =====================

@app.route('/admin')
@login_required
def admin_dashboard():
    if not current_user.is_admin:
        abort(403)
    pending_users = User.query.filter_by(is_approved=False).count()
    pending_initiatives = Initiative.query.filter_by(is_published=False).count()
    pending_questions = Question.query.filter_by(is_published=False).count()
    pending_projects = Project.query.filter_by(is_published=False).count()
    pending_events = Event.query.filter_by(is_published=False).count()
    pending_comments = Comment.query.filter_by(is_approved=False).count()
    pending_policy = PolicyDevelopment.query.filter_by(
        is_published=False, processing_status='ready'
    ).count()
    queue_count = InitiativeSendQueue.query.filter_by(sent_at=None).count()
    policy_queue_count = PolicySendQueue.query.filter_by(sent_at=None).count()
    undetected_lang_count = Initiative.query.filter(Initiative.detected_lang.is_(None)).count()
    return render_template('admin/dashboard.html',
                         pending_users=pending_users,
                         pending_initiatives=pending_initiatives,
                         pending_questions=pending_questions,
                         pending_projects=pending_projects,
                         pending_events=pending_events,
                         pending_comments=pending_comments,
                         pending_policy=pending_policy,
                         queue_count=queue_count,
                         policy_queue_count=policy_queue_count,
                         undetected_lang_count=undetected_lang_count)

@app.route('/admin/approvals')
@login_required
def admin_approvals():
    if not current_user.is_admin:
        abort(403)
    type_filter = request.args.get('type', 'all')
    if type_filter == 'users':
        items = User.query.filter_by(is_approved=False).all()
    elif type_filter == 'initiatives':
        items = Initiative.query.filter_by(is_published=False).all()
    elif type_filter == 'questions':
        items = Question.query.filter_by(is_published=False).all()
    elif type_filter == 'projects':
        items = Project.query.filter_by(is_published=False).all()
    elif type_filter == 'events':
        items = Event.query.filter_by(is_published=False).all()
    elif type_filter == 'comments':
        items = Comment.query.filter_by(is_approved=False).order_by(Comment.created_at.desc()).all()
    elif type_filter == 'policy':
        items = PolicyDevelopment.query.filter_by(
            is_published=False, processing_status='ready'
        ).order_by(PolicyDevelopment.created_at.desc()).all()
    else:
        users = User.query.filter_by(is_approved=False).all()
        initiatives = Initiative.query.filter_by(is_published=False).all()
        questions = Question.query.filter_by(is_published=False).all()
        projects = Project.query.filter_by(is_published=False).all()
        events = Event.query.filter_by(is_published=False).all()
        comments = Comment.query.filter_by(is_approved=False).all()
        policy_items = PolicyDevelopment.query.filter_by(
            is_published=False, processing_status='ready'
        ).order_by(PolicyDevelopment.created_at.desc()).all()
        items = list(users) + list(initiatives) + list(questions) + list(projects) + list(events) + list(comments) + list(policy_items)
    return render_template('admin/approvals.html', items=items, type_filter=type_filter)

@app.route('/admin/approve/<type>/<int:id>', methods=['POST'])
@login_required
def approve_item(type, id):
    if not current_user.is_admin:
        abort(403)

    if type == 'initiative':
        item = Initiative.query.get_or_404(id)
        item.is_published = True
        submitter = User.query.get(item.user_id)
        if submitter:
            award_points(submitter, 'initiative_approved')
        db.session.commit()

        # Notify author
        if submitter:
            try:
                send_initiative_approved_email(submitter, item.slug, item.title)
            except Exception as e:
                app.logger.error(f"Initiative approved email error: {e}")

        # Add to send queue if quality score >= 4 (or unscored) — admin sends manually
        if item.quality_score is None or item.quality_score >= 4:
            _enqueue_initiative(None, item.id)

        flash('Initiative published and added to send queue (if score ≥ 4).', 'success')

    elif type == 'project':
        item = Project.query.get_or_404(id)
        item.is_published = True
        if item.submitted_by:
            submitter = User.query.get(item.submitted_by)
            if submitter:
                award_points(submitter, 'project_approved')
        db.session.commit()
        flash('Project published.', 'success')

    elif type == 'event':
        item = Event.query.get_or_404(id)
        item.is_published = True                         # <-- was missing in old code
        if not item.zoom_webinar_id:
            try:
                meeting_id = create_zoom_webinar(item)
                item.zoom_webinar_id = meeting_id
                app.logger.info(f"Zoom meeting created on approval: {meeting_id} for event {item.id}")
            except Exception as e:
                app.logger.error(f"Zoom meeting creation failed on approval (event {item.id}): {e}")
                flash('Event published, but Zoom meeting creation failed. '
                      'Edit the event to retry.', 'warning')
        db.session.commit()
        flash('Event published.', 'success')

    elif type == 'comment':
        item = Comment.query.get_or_404(id)
        item.is_approved = True
        db.session.commit()
        flash('Comment approved.', 'success')

    elif type == 'policydevelopment':
        item = PolicyDevelopment.query.get_or_404(id)
        item.is_published = True
        item.updated_at   = datetime.utcnow()
        db.session.commit()
        existing_q = PolicySendQueue.query.filter_by(policy_id=item.id).first()
        if not existing_q:
            db.session.add(PolicySendQueue(policy_id=item.id))
            db.session.commit()
        flash('Policy development published and added to send queue.', 'success')

    else:
        abort(400)

    return redirect(request.referrer or url_for('admin_dashboard'))

@app.route('/admin/approve-all', methods=['POST'])
@login_required
def approve_all():
    """Approve every pending user registration and every pending initiative in one click.

    - Each newly approved *user* gets an individual welcome / approval email.
    - All newly approved *initiatives* are announced in a single digest email
      sent to all members (instead of one email per initiative).
    - Individual authors are still notified per-initiative that their work is live.
    """
    if not current_user.is_admin:
        abort(403)

    suppress_member_notifications = request.form.get('suppress_member_notifications') == '1'

    # ── 1. Approve all pending users ──────────────────────────────────────────
    pending_users = User.query.filter_by(is_approved=False).all()
    approved_user_count = 0
    published_initiative_count = 0
    for user in pending_users:
        user.is_approved = True
        approved_user_count += 1

        # Publish any initiative this user submitted that is still pending
        reg_initiative = Initiative.query.filter_by(user_id=user.id, is_published=False).first()
        if reg_initiative:
            reg_initiative.is_published = True
            published_initiative_count += 1
            try:
                phrases = extract_noun_phrases(reg_initiative.content)
                update_noun_phrase_db(reg_initiative.id, phrases)
            except Exception as e:
                app.logger.error(f"Noun phrase error on bulk approve (user {user.id}): {e}")
            award_points(user, 'initiative_published', commit=False)

            # Add to send queue if quality score >= 4 (or unscored)
            if reg_initiative.quality_score is None or reg_initiative.quality_score >= 4:
                try:
                    existing = InitiativeSendQueue.query.filter_by(initiative_id=reg_initiative.id).first()
                    if not existing:
                        db.session.add(InitiativeSendQueue(initiative_id=reg_initiative.id))
                except Exception as e:
                    app.logger.error(f"Queue error on bulk approve (initiative {reg_initiative.id}): {e}")

        # Individual welcome email to the newly approved member
        try:
            send_approval_email(user.email, reg_initiative.slug if reg_initiative else None)
        except Exception as e:
            app.logger.error(f"Bulk approve – welcome email error for {user.email}: {e}")

    db.session.commit()

    # ── 2. Approve all remaining pending initiatives ───────────────────────────
    pending_initiatives = Initiative.query.filter_by(is_published=False).all()

    for initiative in pending_initiatives:
        initiative.is_published = True
        published_initiative_count += 1
        author = User.query.get(initiative.user_id)

        # Noun phrases
        try:
            phrases = extract_noun_phrases(initiative.content)
            update_noun_phrase_db(initiative.id, phrases)
        except Exception as e:
            app.logger.error(f"Noun phrase error on bulk approve (initiative {initiative.id}): {e}")

        # Points for the author
        if author:
            award_points(author, 'initiative_published', commit=False)

        # Notify the author individually that their initiative is live
        if author:
            try:
                send_initiative_approved_email(author, initiative.slug, initiative.title)
            except Exception as e:
                app.logger.error(f"Bulk approve – author email error for {author.email}: {e}")

        # Add to send queue if quality score >= 4 (or unscored)
        if initiative.quality_score is None or initiative.quality_score >= 4:
            try:
                existing = InitiativeSendQueue.query.filter_by(initiative_id=initiative.id).first()
                if not existing:
                    db.session.add(InitiativeSendQueue(initiative_id=initiative.id))
            except Exception as e:
                app.logger.error(f"Queue error on bulk approve (initiative {initiative.id}): {e}")

    db.session.commit()

    # ── 3. Flash summary ──────────────────────────────────────────────────────
    parts = []
    if approved_user_count:
        parts.append(f"{approved_user_count} user{'s' if approved_user_count != 1 else ''} approved")
    if published_initiative_count:
        parts.append(
            f"{published_initiative_count} "
            f"initiative{'s' if published_initiative_count != 1 else ''} published"
        )
    if parts:
        flash("Approved: " + " and ".join(parts) + ". High-quality initiatives added to the Send Queue.", 'success')
    else:
        flash("Nothing pending — everything is already approved.", 'info')

    return redirect(url_for('admin_approvals'))

@app.route('/admin/unpublish/<type>/<int:id>', methods=['POST'])
@login_required
def unpublish_item(type, id):
    if not current_user.is_admin:
        abort(403)
    if type == 'initiative':
        item = Initiative.query.get_or_404(id)
        item.is_published = False
    elif type == 'question':
        item = Question.query.get_or_404(id)
        item.is_published = False
    elif type == 'project':
        item = Project.query.get_or_404(id)
        item.is_published = False
    elif type == 'event':
        item = Event.query.get_or_404(id)
        item.is_published = False
    elif type == 'comment':
        item = Comment.query.get_or_404(id)
        db.session.delete(item)
        db.session.commit()
        flash('Comment rejected and deleted.', 'success')
        return redirect(request.referrer or url_for('admin_approvals'))
    elif type == 'policydevelopment':
        item = PolicyDevelopment.query.get_or_404(id)
        item.is_published = False
        # Also remove from send queue if present
        PolicySendQueue.query.filter_by(policy_id=id).delete()
        db.session.commit()
        flash('Policy development rejected and removed from send queue.', 'success')
        return redirect(request.referrer or url_for('admin_approvals'))
    db.session.commit()
    flash('Item unpublished.', 'success')
    return redirect(url_for('admin_approvals'))


# ===================== SEND QUEUE ROUTES =====================

@app.route('/admin/send-queue')
@login_required
def admin_send_queue():
    """View of all approved initiatives and policy developments waiting to be sent to members."""
    if not current_user.is_admin:
        abort(403)

    # Initiatives
    initiative_unsent = (InitiativeSendQueue.query
              .filter_by(sent_at=None)
              .order_by(InitiativeSendQueue.queued_at.desc())
              .all())
    initiative_sent = (InitiativeSendQueue.query
            .filter(InitiativeSendQueue.sent_at.isnot(None))
            .order_by(InitiativeSendQueue.sent_at.desc())
            .limit(20).all())

    # Policy developments
    policy_unsent = (PolicySendQueue.query
              .filter_by(sent_at=None)
              .order_by(PolicySendQueue.queued_at.desc())
              .all())
    policy_sent = (PolicySendQueue.query
            .filter(PolicySendQueue.sent_at.isnot(None))
            .order_by(PolicySendQueue.sent_at.desc())
            .limit(20).all())

    return render_template('admin/send_queue.html', 
                         initiative_unsent=initiative_unsent, 
                         initiative_sent=initiative_sent,
                         policy_unsent=policy_unsent,
                         policy_sent=policy_sent)


@app.route('/admin/send-queue/send/<int:queue_id>', methods=['POST'])
@login_required
def send_queue_item(queue_id):
    """Send a single initiative from the queue to all subscribed members."""
    if not current_user.is_admin:
        abort(403)
    entry = InitiativeSendQueue.query.get_or_404(queue_id)
    if entry.sent_at:
        flash('This initiative has already been sent.', 'warning')
        return redirect(url_for('admin_send_queue'))
    initiative = entry.initiative
    try:
        initiative_url = url_for('view_initiative', slug=initiative.slug, _external=True)
        subscribed_users = User.query.filter_by(is_approved=True, is_subscribed=True).all()
        send_single_initiative_notification({
            'title': initiative.title,
            'short_description': initiative.short_description or '',
            'url': initiative_url,
        }, subscribed_users)
        entry.sent_at = datetime.utcnow()
        db.session.commit()
        flash(f'"{initiative.title}" sent to {len(subscribed_users)} member(s).', 'success')
    except Exception as e:
        app.logger.error(f"Send queue item error: {e}")
        flash('Failed to send. Check logs.', 'error')
    return redirect(url_for('admin_send_queue'))


@app.route('/admin/send-queue/send-all', methods=['POST'])
@login_required
def send_queue_all():
    """Send ALL unsent initiatives in the queue as a single digest email."""
    if not current_user.is_admin:
        abort(403)
    unsent = InitiativeSendQueue.query.filter_by(sent_at=None).all()
    if not unsent:
        flash('No unsent initiatives in the queue.', 'info')
        return redirect(url_for('admin_send_queue'))
    initiatives_data = []
    for entry in unsent:
        initiative = entry.initiative
        initiative_url = url_for('view_initiative', slug=initiative.slug, _external=True)
        initiatives_data.append({
            'title': initiative.title,
            'short_description': initiative.short_description or '',
            'url': initiative_url,
        })
    try:
        subscribed_users = User.query.filter_by(is_approved=True, is_subscribed=True).all()
        send_bulk_initiatives_digest(initiatives_data, subscribed_users)
        now = datetime.utcnow()
        for entry in unsent:
            entry.sent_at = now
        db.session.commit()
        flash(
            f'{len(unsent)} initiative(s) sent to {len(subscribed_users)} member(s) as a digest.',
            'success'
        )
    except Exception as e:
        app.logger.error(f"Send all queue error: {e}")
        flash('Failed to send digest. Check logs.', 'error')
    return redirect(url_for('admin_send_queue'))


@app.route('/admin/send-queue/remove/<int:queue_id>', methods=['POST'])
@login_required
def remove_queue_item(queue_id):
    """Remove an initiative from the send queue without sending."""
    if not current_user.is_admin:
        abort(403)
    entry = InitiativeSendQueue.query.get_or_404(queue_id)
    db.session.delete(entry)
    db.session.commit()
    flash('Initiative removed from send queue.', 'success')
    return redirect(url_for('admin_send_queue'))


# ===================== POLICY SEND QUEUE ROUTES =====================


@app.route('/admin/policy-send-queue/send/<int:queue_id>', methods=['POST'])
@login_required
def send_policy_queue_item(queue_id):
    """Send a single policy development from the queue to all subscribed members."""
    if not current_user.is_admin:
        abort(403)
    entry = PolicySendQueue.query.get_or_404(queue_id)
    if entry.sent_at:
        flash('This policy development has already been sent.', 'warning')
        return redirect(url_for('admin_policy_send_queue'))
    policy = entry.policy
    try:
        policy_url = url_for('view_policy', id=policy.id, _external=True)
        subscribed_users = User.query.filter_by(is_approved=True, is_subscribed=True).all()
        member_count = len(subscribed_users)

        # Build policy_data dict before the email loop so we don't rely on
        # the SQLAlchemy-tracked `policy` object staying live during a long send.
        policy_data = {
            'title': policy.title or policy.source_url[:100],
            'short_summary': policy.short_summary or '',
            'url': policy_url,
            'country': policy.country or '',
            'published_date': policy.published_date.strftime('%B %d, %Y') if policy.published_date else '',
        }
        policy_label = policy_data['title']

        send_single_policy_notification(policy_data, subscribed_users)

        # Re-fetch entry after the potentially long email loop to avoid a
        # stale/detached SQLAlchemy object causing an internal server error
        # even though all emails were delivered successfully.
        entry = PolicySendQueue.query.get(queue_id)
        entry.sent_at = datetime.utcnow()
        db.session.commit()
        flash(f'"{policy_label}" sent to {member_count} member(s).', 'success')
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Policy send queue item error: {e}", exc_info=True)
        flash('Failed to send. Check logs.', 'error')
    return redirect(url_for('admin_policy_send_queue'))


@app.route('/admin/policy-send-queue/send-all', methods=['POST'])
@login_required
def send_policy_queue_all():
    """Send ALL unsent policy developments in the queue as a single digest email."""
    if not current_user.is_admin:
        abort(403)
    unsent = PolicySendQueue.query.filter_by(sent_at=None).all()
    if not unsent:
        flash('No unsent policy developments in the queue.', 'info')
        return redirect(url_for('admin_policy_send_queue'))
    policies_data = []
    for entry in unsent:
        policy = entry.policy
        policy_url = url_for('view_policy', id=policy.id, _external=True)
        policies_data.append({
            'title': policy.title or policy.source_url[:100],
            'short_summary': policy.short_summary or '',
            'url': policy_url,
            'country': policy.country or '',
            'published_date': policy.published_date.strftime('%B %d, %Y') if policy.published_date else '',
        })
    try:
        subscribed_users = User.query.filter_by(is_approved=True, is_subscribed=True).all()
        send_bulk_policies_digest(policies_data, subscribed_users)
        now = datetime.utcnow()
        for entry in unsent:
            entry.sent_at = now
        db.session.commit()
        flash(
            f'{len(unsent)} policy development(s) sent to {len(subscribed_users)} member(s) as a digest.',
            'success'
        )
    except Exception as e:
        app.logger.error(f"Send all policy queue error: {e}")
        flash('Failed to send digest. Check logs.', 'error')
    return redirect(url_for('admin_policy_send_queue'))


@app.route('/admin/policy-send-queue/remove/<int:queue_id>', methods=['POST'])
@login_required
def remove_policy_queue_item(queue_id):
    """Remove a policy development from the send queue without sending."""
    if not current_user.is_admin:
        abort(403)
    entry = PolicySendQueue.query.get_or_404(queue_id)
    db.session.delete(entry)
    db.session.commit()
    flash('Policy development removed from send queue.', 'success')
    return redirect(url_for('admin_policy_send_queue'))

@app.route('/admin/settings', methods=['GET', 'POST'])
@login_required
def admin_settings():
    if not current_user.is_admin:
        abort(403)
    if request.method == 'POST':
        # Update API keys
        if request.form.get('nvidia_api_key'):
            os.environ['NVIDIA_API_KEY'] = request.form.get('nvidia_api_key')
            Config.NVIDIA_API_KEY = request.form.get('nvidia_api_key')
        if request.form.get('mail_password'):
            os.environ['MAIL_PASSWORD'] = request.form.get('mail_password')
            app.config['MAIL_PASSWORD'] = request.form.get('mail_password')
        if request.form.get('mail_username'):
            os.environ['MAIL_USERNAME'] = request.form.get('mail_username')
            app.config['MAIL_USERNAME'] = request.form.get('mail_username')
            app.config['MAIL_DEFAULT_SENDER'] = request.form.get('mail_username')
        # auto-approve toggle
        auto_approve = 'true' if request.form.get('auto_approve_members') else 'false'
        set_setting('auto_approve_members', auto_approve)
        flash('Settings updated successfully.', 'success')
        return redirect(url_for('admin_settings'))
    
    # Database stats
    stats = {
        'users': User.query.count(),
        'initiatives': Initiative.query.filter_by(is_published=True).count(),
        'pending_initiatives': Initiative.query.filter_by(is_published=False).count(),
        'phrases': NounPhrase.query.count(),
        'tags': Tag.query.count(),
        'questions': Question.query.filter_by(is_published=True).count(),
        'pending_questions': Question.query.filter_by(is_published=False).count()
    }
    # Get current auto_approve setting
    auto_approve = get_setting('auto_approve_members', 'true').lower() == 'true'
    return render_template('admin/settings.html', stats=stats, config=Config, auto_approve=auto_approve)

@app.route('/admin/fields', methods=['GET', 'POST'])
@login_required
def admin_fields():
    if not current_user.is_admin:
        abort(403)
    if request.method == 'POST':
        field = RegistrationField(
            field_name=request.form.get('field_name'),
            field_label=request.form.get('field_label'),
            field_type=request.form.get('field_type'),
            is_required=request.form.get('is_required') == 'on',
            options=request.form.get('options')
        )
        db.session.add(field)
        db.session.commit()
        flash('Field added.', 'success')
        return redirect(url_for('admin_fields'))
    fields = RegistrationField.query.order_by(RegistrationField.order).all()
    return render_template('admin/fields.html', fields=fields)

@app.route('/admin/field/delete/<int:id>', methods=['POST'])
@login_required
def admin_delete_field(id):
    if not current_user.is_admin:
        abort(403)
    field = RegistrationField.query.get_or_404(id)
    db.session.delete(field)
    db.session.commit()
    flash('Field deleted.', 'success')
    return redirect(url_for('admin_fields'))

@app.route('/admin/trigger-nlp', methods=['POST'])
@login_required
def trigger_nlp():
    if not current_user.is_admin:
        abort(403)
    try:
        initiatives = Initiative.query.filter_by(is_published=True).all()
        for initiative in initiatives:
            phrases = extract_noun_phrases(initiative.content)
            update_noun_phrase_db(initiative.id, phrases)
        flash('Noun phrase database updated.', 'success')
    except Exception as e:
        flash(f'Error: {str(e)}', 'error')
    return redirect(url_for('admin_dashboard'))


# ===================== BULK SCORING =====================
# In-memory job state — survives for the lifetime of the process.
# Only one bulk-score job can run at a time.
_bulk_score_job = {
    'running': False,
    'total': 0,
    'done': 0,
    'errors': 0,
    'last_title': '',
}
_bulk_score_lock = threading.Lock()


def _run_bulk_scoring(flask_app):
    """Background thread: score every unscored initiative one by one."""
    with flask_app.app_context():
        unscored = Initiative.query.filter(
            Initiative.quality_score.is_(None)
        ).all()

        with _bulk_score_lock:
            _bulk_score_job['total'] = len(unscored)
            _bulk_score_job['done'] = 0
            _bulk_score_job['errors'] = 0
            _bulk_score_job['last_title'] = ''

        for ini in unscored:
            try:
                score = score_initiative_quality(
                    ini.title, ini.content, ini.short_description or ""
                )
                if score is not None:
                    obj = Initiative.query.get(ini.id)
                    if obj:
                        obj.quality_score = score
                        db.session.commit()
                        if score >= 4:
                            _enqueue_initiative(flask_app, ini.id)
            except Exception as e:
                flask_app.logger.error(
                    f"Bulk scoring error (initiative {ini.id}): {e}"
                )
                with _bulk_score_lock:
                    _bulk_score_job['errors'] += 1

            with _bulk_score_lock:
                _bulk_score_job['done'] += 1
                _bulk_score_job['last_title'] = ini.title

        with _bulk_score_lock:
            _bulk_score_job['running'] = False


@app.route('/admin/bulk-score', methods=['POST'])
@login_required
def admin_bulk_score():
    """Start a background job that scores all unscored initiatives."""
    if not current_user.is_admin:
        abort(403)

    with _bulk_score_lock:
        if _bulk_score_job['running']:
            flash('A scoring job is already running — check progress on the dashboard.', 'warning')
            return redirect(url_for('admin_dashboard'))

        unscored_count = Initiative.query.filter(
            Initiative.quality_score.is_(None)
        ).count()

        if unscored_count == 0:
            flash('All initiatives already have quality scores.', 'info')
            return redirect(url_for('admin_dashboard'))

        _bulk_score_job['running'] = True
        _bulk_score_job['total'] = unscored_count
        _bulk_score_job['done'] = 0
        _bulk_score_job['errors'] = 0
        _bulk_score_job['last_title'] = ''

    threading.Thread(
        target=_run_bulk_scoring,
        args=(app,),
        daemon=True
    ).start()

    flash(
        f'Scoring {unscored_count} unscored initiative(s) in the background. '
        f'Progress is shown below.',
        'info'
    )
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/bulk-score/progress')
@login_required
def admin_bulk_score_progress():
    """JSON endpoint polled by the dashboard to show live scoring progress."""
    if not current_user.is_admin:
        abort(403)
    with _bulk_score_lock:
        data = dict(_bulk_score_job)
    return jsonify(data)


# ===================== BULK LANGUAGE DETECTION =====================

_bulk_lang_job = {
    'running': False,
    'total': 0,
    'done': 0,
    'errors': 0,
    'last_title': '',
}
_bulk_lang_lock = threading.Lock()


def _run_bulk_lang_detection(flask_app):
    """Background thread: detect language for every initiative missing detected_lang."""
    with flask_app.app_context():
        undetected = Initiative.query.filter(
            Initiative.detected_lang.is_(None)
        ).all()

        with _bulk_lang_lock:
            _bulk_lang_job['total'] = len(undetected)
            _bulk_lang_job['done'] = 0
            _bulk_lang_job['errors'] = 0
            _bulk_lang_job['last_title'] = ''

        for ini in undetected:
            try:
                lang = detect_language(ini.title, ini.content)
                if lang:
                    obj = Initiative.query.get(ini.id)
                    if obj:
                        obj.detected_lang = lang
                        db.session.commit()
            except Exception as e:
                flask_app.logger.error(
                    f"Bulk lang detection error (initiative {ini.id}): {e}"
                )
                with _bulk_lang_lock:
                    _bulk_lang_job['errors'] += 1

            with _bulk_lang_lock:
                _bulk_lang_job['done'] += 1
                _bulk_lang_job['last_title'] = ini.title

        with _bulk_lang_lock:
            _bulk_lang_job['running'] = False


@app.route('/admin/bulk-detect-lang', methods=['POST'])
@login_required
def admin_bulk_detect_lang():
    """Start a background job that detects language for all initiatives without one."""
    if not current_user.is_admin:
        abort(403)

    with _bulk_lang_lock:
        if _bulk_lang_job['running']:
            flash('A language detection job is already running — check progress on the dashboard.', 'warning')
            return redirect(url_for('admin_dashboard'))

        undetected_count = Initiative.query.filter(
            Initiative.detected_lang.is_(None)
        ).count()

        if undetected_count == 0:
            flash('All initiatives already have a detected language.', 'info')
            return redirect(url_for('admin_dashboard'))

        _bulk_lang_job['running'] = True
        _bulk_lang_job['total'] = undetected_count
        _bulk_lang_job['done'] = 0
        _bulk_lang_job['errors'] = 0
        _bulk_lang_job['last_title'] = ''

    threading.Thread(
        target=_run_bulk_lang_detection,
        args=(app,),
        daemon=True
    ).start()

    flash(
        f'Detecting language for {undetected_count} initiative(s) in the background. '
        f'Progress is shown below.',
        'info'
    )
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/bulk-detect-lang/progress')
@login_required
def admin_bulk_detect_lang_progress():
    """JSON endpoint polled by the dashboard to show live lang-detection progress."""
    if not current_user.is_admin:
        abort(403)
    with _bulk_lang_lock:
        data = dict(_bulk_lang_job)
    return jsonify(data)


# ===================== ADMIN IMPORT MEMBERS =====================

@app.route('/admin/import-members', methods=['GET', 'POST'])
@login_required
def admin_import_members():
    if not current_user.is_admin:
        abort(403)
    now = datetime.utcnow()
    # Fetch upcoming/current events for the event-invite dropdown
    upcoming_events = Event.query.filter(
        Event.start_date >= now,
        Event.is_published == True
    ).order_by(Event.start_date.asc()).all()

    if request.method == 'POST':
        if 'file' not in request.files:
            flash('No file selected', 'error')
            return redirect(request.url)
        file = request.files['file']
        if file.filename == '':
            flash('No file selected', 'error')
            return redirect(request.url)
        # Read mode checkboxes
        invite_only         = request.form.get('invite_only') == 'on'
        custom_message_mode = request.form.get('custom_message_mode') == 'on'
        event_invite_mode   = request.form.get('event_invite_mode') == 'on'
        custom_subject      = request.form.get('custom_subject', '').strip()
        custom_body         = request.form.get('custom_body', '').strip()
        event_invite_id     = request.form.get('event_invite_id', '').strip()

        # Resolve selected event for event-invite mode
        selected_event = None
        event_invite_url = None
        if event_invite_mode and event_invite_id:
            selected_event = Event.query.get(int(event_invite_id))
            if selected_event:
                event_invite_url = url_for('event_detail', id=selected_event.id, _external=True)

        if file and file.filename.endswith('.csv'):
            try:
                stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
                csv_reader = csv.DictReader(stream)
                imported = 0
                invited  = 0
                errors   = []
                for row_num, row in enumerate(csv_reader, start=2):
                    # In non-import modes only email+name are required
                    if custom_message_mode or invite_only or event_invite_mode:
                        required = ['email', 'name']
                    else:
                        required = ['email', 'name', 'organization', 'stakeholder_type', 'country']
                    missing = [f for f in required if not row.get(f) or not row.get(f).strip()]
                    if missing:
                        errors.append(f"Row {row_num}: Missing fields {missing}")
                        continue
                    email = row['email'].lower().strip()
                    name  = row['name'].strip()

                    # ── EVENT INVITE MODE ─────────────────────────────────────
                    if event_invite_mode:
                        if not selected_event:
                            flash('Please select an event for event invitation mode.', 'error')
                            return redirect(request.url)
                        # Skip if email is on the blocked list
                        if BlockedEmail.query.filter_by(email=email).first():
                            errors.append(f"Row {row_num}: {email} has unsubscribed — skipped")
                            continue
                        try:
                            from utils.email_sender import send_event_invitation_email
                            send_event_invitation_email(email, name, selected_event, event_invite_url)
                            invited += 1
                        except Exception as e:
                            app.logger.error(f"Event invite email error for {email}: {e}")
                            errors.append(f"Row {row_num}: Failed to send event invite to {email}")
                        continue

                    # ── CUSTOM MESSAGE MODE ───────────────────────────────────
                    if custom_message_mode:
                        if not custom_subject or not custom_body:
                            flash('Custom subject and message body are required in Custom Message mode.', 'error')
                            return redirect(request.url)
                        # Skip if email is on the blocked list
                        if BlockedEmail.query.filter_by(email=email).first():
                            errors.append(f"Row {row_num}: {email} has unsubscribed — skipped")
                            continue
                        try:
                            send_custom_bulk_email(email, name, custom_subject, custom_body)
                            invited += 1
                        except Exception as e:
                            app.logger.error(f"Custom message email error for {email}: {e}")
                            errors.append(f"Row {row_num}: Failed to send message to {email}")
                        continue

                    # ── INVITE-ONLY MODE ──────────────────────────────────────
                    if invite_only:
                        # Skip if email is on the blocked list
                        if BlockedEmail.query.filter_by(email=email).first():
                            errors.append(f"Row {row_num}: {email} has unsubscribed — skipped")
                            continue
                        try:
                            send_invitation_email(email, name)
                            invited += 1
                        except Exception as e:
                            app.logger.error(f"Invitation email error for {email}: {e}")
                            errors.append(f"Row {row_num}: Failed to send invitation to {email}")
                        continue

                    # ── NORMAL IMPORT MODE ────────────────────────────────────
                    # Check duplicate
                    if User.query.filter_by(email=email).first():
                        errors.append(f"Row {row_num}: Email already exists")
                        continue
                    # Validate stakeholder_type
                    valid_types = ['Government', 'NGO / Civil Society', 'Development Partner / Donor', 
                                   'Academic / Research', 'UN Agency', 'Private Sector']
                    if row['stakeholder_type'].strip() not in valid_types:
                        errors.append(f"Row {row_num}: Invalid stakeholder_type")
                        continue
                    # Create user — always approved when imported by admin
                    user = User(
                        email=email,
                        name=name,
                        organization=row['organization'].strip(),
                        stakeholder_type=row['stakeholder_type'].strip(),
                        country=row['country'].strip(),
                        is_approved=True,
                        is_admin=False
                    )
                    db.session.add(user)
                    db.session.flush()
                    # Always send welcome email to imported members (unless blocked)
                    if not BlockedEmail.query.filter_by(email=email).first():
                        try:
                            send_import_welcome_email(user)
                        except Exception as e:
                            app.logger.error(f"Import welcome email error for {user.email}: {e}")
                    imported += 1
                db.session.commit()
                if event_invite_mode:
                    flash(f'Sent {invited} event invitation(s). Errors: {len(errors)}', 'info' if errors else 'success')
                elif custom_message_mode:
                    flash(f'Sent {invited} custom message(s). Errors: {len(errors)}', 'info' if errors else 'success')
                elif invite_only:
                    flash(f'Sent {invited} invitation(s). Errors: {len(errors)}', 'info' if errors else 'success')
                else:
                    flash(f'Imported {imported} members. Errors: {len(errors)}', 'info' if errors else 'success')
                if errors:
                    for err in errors[:5]:
                        flash(err, 'error')
                return redirect(url_for('admin_import_members'))
            except Exception as e:
                flash(f'Error processing file: {str(e)}', 'error')
                return redirect(request.url)
    return render_template('admin/import_members.html', upcoming_events=upcoming_events)

@app.route('/admin/import-members-template')
@login_required
def admin_import_members_template():
    if not current_user.is_admin:
        abort(403)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['email', 'name', 'organization', 'stakeholder_type', 'country'])
    writer.writerow(['john@unicef.org', 'John Doe', 'UNICEF Ghana', 'UN Agency', 'Ghana'])
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=member_import_template.csv'}
    )

# ===================== ADMIN EXPORT MEMBERS =====================

@app.route('/admin/export-members')
@login_required
def admin_export_members():
    if not current_user.is_admin:
        abort(403)

    members = User.query.filter_by(is_admin=False).order_by(User.created_at.asc()).all()

    output = io.StringIO()
    writer = csv.writer(output)

    # Header
    writer.writerow([
        'id', 'email', 'name', 'organization', 'stakeholder_type', 'country',
        'is_approved', 'is_subscribed', 'points', 'created_at',
        'initiatives_published', 'initiatives_total',
        'event_registrations', 'project_participations',
    ])

    for user in members:
        initiatives_published = Initiative.query.filter_by(user_id=user.id, is_published=True).count()
        initiatives_total     = Initiative.query.filter_by(user_id=user.id).count()
        event_regs            = EventRegistration.query.filter_by(user_id=user.id).count()
        project_parts         = ProjectParticipation.query.filter_by(user_id=user.id).count()

        writer.writerow([
            user.id,
            user.email,
            user.name,
            user.organization,
            user.stakeholder_type,
            user.country,
            user.is_approved,
            user.is_subscribed,
            user.points,
            user.created_at.strftime('%Y-%m-%d %H:%M:%S') if user.created_at else '',
            initiatives_published,
            initiatives_total,
            event_regs,
            project_parts,
        ])

    output.seek(0)
    from datetime import date
    filename = f"members_export_{date.today().isoformat()}.csv"
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


# ===================== ADMIN IMPORT INITIATIVES =====================

@app.route('/admin/import-initiatives', methods=['GET', 'POST'])
@login_required
def admin_import_initiatives():
    if not current_user.is_admin:
        abort(403)

    # ── PHASE 2: confirmed import ──────────────────────────────────────────────
    if request.method == 'POST' and request.form.get('action') == 'confirm':
        rows_json   = request.form.get('rows_data', '[]')
        create_new  = request.form.get('create_new_members') == 'on'
        send_emails = request.form.get('send_emails') == 'on'

        try:
            rows = json.loads(rows_json)
        except Exception:
            flash('Session data corrupted. Please re-upload the CSV.', 'error')
            return redirect(url_for('admin_import_initiatives'))

        imported_count = 0
        created_users  = 0
        skipped_rows   = []

        # Collect emails to send AFTER commit, so we never send emails for
        # data that gets rolled back due to a later error in the same batch.
        pending_welcome_emails  = []  # list of User objects
        pending_pending_emails  = []  # list of (User, initiative_title) tuples

        for row in rows:
            email = row['email'].lower().strip()
            user  = User.query.filter_by(email=email).first()

            if not user:
                if not create_new:
                    skipped_rows.append(f"Skipped {email}: not a member")
                    continue
                valid_types = ['Government', 'NGO / Civil Society', 'Development Partner / Donor',
                               'Academic / Research', 'UN Agency', 'Private Sector']
                if row.get('stakeholder_type', '').strip() not in valid_types:
                    skipped_rows.append(f"Skipped {email}: invalid stakeholder_type")
                    continue
                user = User(
                    email=email,
                    name=row['name'].strip(),
                    organization=row['organization'].strip(),
                    stakeholder_type=row['stakeholder_type'].strip(),
                    country=row['country'].strip(),
                    is_approved=True,
                    is_admin=False
                )
                db.session.add(user)
                db.session.flush()
                created_users += 1
                if send_emails:
                    pending_welcome_emails.append(user)

            # Check duplicate initiative
            # Truncate to 200 chars for the duplicate check too, so it matches
            # what will actually be stored.
            initiative_title = row['initiative_title'].strip()[:200]
            existing = Initiative.query.filter_by(
                user_id=user.id,
                title=initiative_title
            ).first()
            if existing:
                skipped_rows.append(f"Skipped duplicate: \"{initiative_title}\" for {email}")
                continue

            # Build slug — truncate base to 190 chars so a numeric suffix still fits
            # within the VARCHAR(200) column.
            base_slug = re.sub(r'[^\w]+', '-', row['initiative_title'].lower().strip()).strip('-')[:190]
            slug = base_slug
            counter = 1
            while Initiative.query.filter_by(slug=slug).first():
                slug = f"{base_slug}-{counter}"
                counter += 1

            # Always unpublished — goes into approval queue
            initiative = Initiative(
                title=initiative_title,
                slug=slug,
                content=row['initiative_content'].strip(),
                short_description=row.get('short_description', '')[:300] if row.get('short_description') else None,
                user_id=user.id,
                stakeholder_type=user.stakeholder_type,
                country=user.country,
                is_published=False,
                created_at=datetime.utcnow()
            )
            db.session.add(initiative)
            db.session.flush()
            imported_count += 1

            # Tags
            if row.get('tags'):
                tag_names = [t.strip().lower() for t in row['tags'].split(',') if t.strip()]
                for tag_name in tag_names:
                    tag = Tag.query.filter_by(name=tag_name).first()
                    if not tag:
                        tag = Tag(name=tag_name, is_vetted=True)
                        db.session.add(tag)
                        db.session.flush()
                    initiative.tags.append(tag)
                    tag.usage_count += 1

            if send_emails:
                pending_pending_emails.append((user, initiative.title))

        # Commit everything first — only send emails once data is safely persisted.
        db.session.commit()

        # Score imported initiatives in background threads (after commit so IDs are stable)
        def _score_imported(flask_app, initiative_id, title, content, short_desc):
            with flask_app.app_context():
                try:
                    score = score_initiative_quality(title, content, short_desc or "")
                    if score is not None:
                        ini = Initiative.query.get(initiative_id)
                        if ini:
                            ini.quality_score = score
                            db.session.commit()
                except Exception as e:
                    flask_app.logger.error(f"CSV import quality scoring error (initiative {initiative_id}): {e}")

        # Re-query the batch we just imported to get their IDs
        for pending_user, pending_title in pending_pending_emails:
            ini = Initiative.query.filter_by(
                user_id=User.query.filter_by(email=pending_user.email).first().id,
                title=pending_title
            ).order_by(Initiative.created_at.desc()).first()
            if ini and ini.quality_score is None:
                threading.Thread(
                    target=_score_imported,
                    args=(app, ini.id, ini.title, ini.content, ini.short_description),
                    daemon=True
                ).start()

        # Send welcome emails for newly created members
        for welcome_user in pending_welcome_emails:
            try:
                send_import_welcome_email(welcome_user)
            except Exception as e:
                app.logger.error(f"Welcome email error for {welcome_user.email}: {e}")

        # Notify each user their initiative is pending review
        for pending_user, pending_title in pending_pending_emails:
            try:
                send_initiative_pending_email(pending_user, pending_title)
            except Exception as e:
                app.logger.error(f"Pending email error for {pending_user.email}: {e}")
        flash(
            f'Import complete: {imported_count} initiative(s) queued for approval'
            + (f', {created_users} new member(s) created' if created_users else '') + '.',
            'success'
        )
        if skipped_rows:
            flash(f'Skipped {len(skipped_rows)} row(s): {", ".join(skipped_rows[:5])}', 'warning')
        return redirect(url_for('admin_approvals', type='initiatives'))

    # ── PHASE 1: parse CSV and show preview ───────────────────────────────────
    if request.method == 'POST':
        if 'file' not in request.files or request.files['file'].filename == '':
            flash('No file selected.', 'error')
            return redirect(request.url)

        file = request.files['file']
        if not file.filename.endswith('.csv'):
            flash('Please upload a .csv file.', 'error')
            return redirect(request.url)

        try:
            stream     = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
            csv_reader = csv.DictReader(stream)
            rows_valid   = []
            rows_invalid = []
            unknown_emails = []

            for row_num, row in enumerate(csv_reader, start=2):
                required = ['email', 'name', 'organization', 'stakeholder_type',
                            'country', 'initiative_title', 'initiative_content']
                missing = [f for f in required if not row.get(f) or not row.get(f).strip()]
                if missing:
                    rows_invalid.append({'row': row_num, 'reason': f"Missing: {', '.join(missing)}"})
                    continue

                email = row['email'].lower().strip()
                user  = User.query.filter_by(email=email).first()
                row_data = {k: v for k, v in row.items()}
                row_data['_row_num'] = row_num
                row_data['_is_known'] = user is not None and user.is_approved
                row_data['_exists_unapproved'] = user is not None and not user.is_approved
                rows_valid.append(row_data)

                if not user:
                    unknown_emails.append(email)

            send_emails = request.form.get('send_emails') == 'on'

            return render_template(
                'admin/import_initiatives.html',
                preview=True,
                rows_valid=rows_valid,
                rows_invalid=rows_invalid,
                unknown_emails=unknown_emails,
                rows_json=json.dumps(rows_valid),
                send_emails=send_emails
            )

        except Exception as e:
            flash(f'Error reading CSV: {str(e)}', 'error')
            return redirect(request.url)

    # GET
    return render_template('admin/import_initiatives.html', preview=False)

@app.route('/admin/import-template')
@login_required
def admin_import_template():
    if not current_user.is_admin:
        abort(403)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['email', 'name', 'organization', 'stakeholder_type', 'country',
                     'initiative_title', 'initiative_content', 'short_description', 'tags'])
    writer.writerow(['example@unicef.org', 'John Doe', 'UNICEF Ghana', 'UN Agency', 'Ghana',
                     'Early Literacy Program', 'This initiative focuses on...', 'Improving early grade reading', 'literacy,teacher training'])
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=initiative_import_template.csv'}
    )

# ===================== ADMIN PROJECTS =====================

@app.route('/admin/projects')
@login_required
def admin_projects():
    if not current_user.is_admin:
        abort(403)
    projects = Project.query.order_by(Project.created_at.desc()).all()
    now = datetime.utcnow()
    return render_template('admin/projects.html', projects=projects, now=now)

@app.route('/admin/project/new', methods=['GET', 'POST'])
@login_required
def admin_new_project():
    if not current_user.is_admin:
        abort(403)
    if request.method == 'POST':
        project = Project(
            title=request.form.get('title'),
            description=request.form.get('description'),
            deadline=datetime.fromisoformat(request.form.get('deadline')),
            start_date=datetime.fromisoformat(request.form.get('start_date')) if request.form.get('start_date') else None,
            is_published=True,
            submitted_by=current_user.id
        )
        db.session.add(project)
        db.session.flush()
        titles = request.form.getlist('activity_title[]')
        descs = request.form.getlist('activity_desc[]')
        deadlines = request.form.getlist('activity_deadline[]')
        for i, title in enumerate(titles):
            if title.strip():
                activity = ProjectActivity(
                    project_id=project.id,
                    title=title,
                    description=descs[i] if i < len(descs) else '',
                    deadline=datetime.fromisoformat(deadlines[i]) if i < len(deadlines) and deadlines[i] else None
                )
                db.session.add(activity)
        db.session.commit()
        if request.form.get('send_notification'):
            try:
                send_project_notification(project)
            except Exception as e:
                app.logger.error(f"Project notification error: {e}")
        flash('Project created successfully.', 'success')
        return redirect(url_for('admin_projects'))
    return render_template('admin/project_form.html', project=None)


@app.route('/admin/project/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def admin_edit_project(id):
    if not current_user.is_admin:
        abort(403)
    project = Project.query.get_or_404(id)
    if request.method == 'POST':
        project.title = request.form.get('title')
        project.description = request.form.get('description')
        project.deadline = datetime.fromisoformat(request.form.get('deadline'))
        project.start_date = datetime.fromisoformat(request.form.get('start_date')) if request.form.get(
            'start_date') else None

        # REMOVED: ProjectParticipation.query.filter_by(project_id=id).delete()

        titles = request.form.getlist('activity_title[]')
        descs = request.form.getlist('activity_desc[]')
        deadlines = request.form.getlist('activity_deadline[]')

        # Get existing activities
        existing_activities = ProjectActivity.query.filter_by(project_id=id).all()

        # Update existing or create new
        for i, title in enumerate(titles):
            if not title.strip():
                continue

            deadline = datetime.fromisoformat(deadlines[i]) if i < len(deadlines) and deadlines[i] else None
            description = descs[i] if i < len(descs) else ''

            if i < len(existing_activities):
                # Update existing activity (preserves signups)
                existing_activities[i].title = title
                existing_activities[i].description = description
                existing_activities[i].deadline = deadline
            else:
                # Create new activity
                activity = ProjectActivity(
                    project_id=project.id,
                    title=title,
                    description=description,
                    deadline=deadline
                )
                db.session.add(activity)

        # Only delete removed activities that have NO signups
        for i in range(len(titles), len(existing_activities)):
            if not existing_activities[i].participations:
                db.session.delete(existing_activities[i])
            # If it has signups, we keep it even if removed from form (to preserve data)

        db.session.commit()
        flash('Project updated successfully.', 'success')
        return redirect(url_for('admin_projects'))
    return render_template('admin/project_form.html', project=project)

# ===================== ADMIN EVENTS =====================

@app.route('/admin/events', methods=['GET'])
@login_required
def admin_events():
    if not current_user.is_admin:
        abort(403)
    events = Event.query.order_by(Event.start_date.desc()).all()
    return render_template('admin/events.html', events=events)

@app.route('/admin/event/new', methods=['GET', 'POST'])
@login_required
def admin_event_new():
    if not current_user.is_admin:
        abort(403)

    if request.method == 'POST':
        title       = request.form.get('title')
        description = request.form.get('description')
        start_date  = datetime.fromisoformat(request.form.get('start_date'))
        end_date    = datetime.fromisoformat(request.form.get('end_date')) \
                      if request.form.get('end_date') else None

        event = Event(
            title=title,
            description=description,
            start_date=start_date,
            end_date=end_date,
            created_by=current_user.id,
            is_published=True,   # admin-created events go live immediately
        )
        db.session.add(event)
        db.session.flush()

        # ── Polls (max 5) ──────────────────────────────────────────────
        poll_titles  = request.form.getlist('poll_title[]')
        poll_descs   = request.form.getlist('poll_desc[]')
        poll_options = request.form.getlist('poll_options[]')

        for i, poll_title in enumerate(poll_titles[:5]):
            if not poll_title.strip():
                continue
            raw_opts = poll_options[i] if i < len(poll_options) else ''
            options  = [
                {'text': o.strip(), 'order': idx}
                for idx, o in enumerate(raw_opts.splitlines())
                if o.strip()
            ]
            if not options:
                continue
            poll = Poll(
                event_id=event.id,
                title=poll_title.strip(),
                description=poll_descs[i].strip() if i < len(poll_descs) else '',
                options=options,
            )
            db.session.add(poll)
            db.session.flush()
            try:
                tags = clean_tags_for_polls(poll_title)
                for tag in tags:
                    db.session.add(PollTag(poll_id=poll.id, tag=tag))
            except Exception as e:
                app.logger.error(f"Poll tag extraction error: {e}")

        # ── Attachments (max 5) ────────────────────────────────────────
        files  = request.files.getlist('attachments[]')
        labels = request.form.getlist('attachment_labels[]')
        for i, f in enumerate(files[:5]):
            if f and f.filename and allowed_attachment(f.filename):
                original, stored = save_attachment(f)
                label = labels[i].strip() if i < len(labels) else ''
                db.session.add(EventAttachment(
                    event_id=event.id,
                    filename=original,
                    stored_name=stored,
                    label=label or original,
                ))

        db.session.commit()

        # ── Create Zoom Meeting ────────────────────────────────────────
        try:
            meeting_id = create_zoom_webinar(event)   # uses Meetings API internally
            event.zoom_webinar_id = meeting_id
            db.session.commit()
            app.logger.info(f"Zoom meeting created: {meeting_id} for event {event.id}")
        except Exception as e:
            app.logger.error(f"Zoom meeting creation failed for event {event.id}: {e}")
            flash('Event saved, but Zoom meeting creation failed. '
                  'Check your Zoom credentials and try editing the event.', 'warning')

        if request.form.get('send_notification'):
            try:
                send_event_notification(event)
            except Exception as e:
                app.logger.error(f"Event notification error: {e}")

        flash('Event created and Zoom meeting scheduled.', 'success')
        return redirect(url_for('admin_events'))

    return render_template('admin/event_form.html')

@app.route('/admin/event/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def admin_event_edit(id):
    if not current_user.is_admin:
        abort(403)
    event = Event.query.get_or_404(id)

    if request.method == 'POST':
        event.title       = request.form.get('title')
        event.description = request.form.get('description')
        event.start_date  = datetime.fromisoformat(request.form.get('start_date'))
        event.end_date    = datetime.fromisoformat(request.form.get('end_date')) \
                            if request.form.get('end_date') else None

        # ── Polls ──────────────────────────────────────────────────────
        existing_polls = Poll.query.filter_by(event_id=id).all()
        poll_titles    = request.form.getlist('poll_title[]')
        poll_descs     = request.form.getlist('poll_desc[]')
        poll_options   = request.form.getlist('poll_options[]')

        for i, poll_title in enumerate(poll_titles[:5]):
            if not poll_title.strip():
                continue
            raw_opts = poll_options[i] if i < len(poll_options) else ''
            options  = [
                {'text': o.strip(), 'order': idx}
                for idx, o in enumerate(raw_opts.splitlines())
                if o.strip()
            ]
            if not options:
                continue
            if i < len(existing_polls):
                poll             = existing_polls[i]
                poll.title       = poll_title.strip()
                poll.description = poll_descs[i].strip() if i < len(poll_descs) else ''
                if poll.options != options:
                    poll.options = options
                PollTag.query.filter_by(poll_id=poll.id).delete()
                db.session.flush()
                try:
                    tags = clean_tags_for_polls(poll_title)
                    for tag in tags:
                        db.session.add(PollTag(poll_id=poll.id, tag=tag))
                except Exception as e:
                    app.logger.error(f"Poll tag extraction error: {e}")
            else:
                poll = Poll(
                    event_id=event.id,
                    title=poll_title.strip(),
                    description=poll_descs[i].strip() if i < len(poll_descs) else '',
                    options=options,
                )
                db.session.add(poll)
                db.session.flush()
                try:
                    tags = clean_tags_for_polls(poll_title)
                    for tag in tags:
                        db.session.add(PollTag(poll_id=poll.id, tag=tag))
                except Exception as e:
                    app.logger.error(f"Poll tag extraction error: {e}")

        # Remove polls beyond those submitted (only if no responses recorded)
        for i in range(len(poll_titles), len(existing_polls)):
            poll = existing_polls[i]
            has_responses = any(
                reg.poll_answers and str(poll.id) in reg.poll_answers
                for reg in EventRegistration.query.filter_by(event_id=id).all()
            )
            if not has_responses:
                PollTag.query.filter_by(poll_id=poll.id).delete()
                db.session.delete(poll)

        # ── Attachments: add new uploads ──────────────────────────────
        files          = request.files.getlist('attachments[]')
        labels         = request.form.getlist('attachment_labels[]')
        existing_count = EventAttachment.query.filter_by(event_id=id).count()
        slots          = max(0, 5 - existing_count)
        for i, f in enumerate(files[:slots]):
            if f and f.filename and allowed_attachment(f.filename):
                original, stored = save_attachment(f)
                label = labels[i].strip() if i < len(labels) else ''
                db.session.add(EventAttachment(
                    event_id=event.id,
                    filename=original,
                    stored_name=stored,
                    label=label or original,
                ))

        # ── Attachments: delete checked ones ──────────────────────────
        for att_id in request.form.getlist('delete_attachment[]'):
            att = EventAttachment.query.get(int(att_id))
            if att and att.event_id == id:
                path = os.path.join(
                    app.config['UPLOAD_FOLDER'], 'event_attachments', att.stored_name
                )
                try:
                    os.remove(path)
                except OSError:
                    pass
                db.session.delete(att)

        db.session.commit()

        # ── Create Zoom Meeting if not yet linked ──────────────────────
        if not event.zoom_webinar_id:
            try:
                meeting_id = create_zoom_webinar(event)
                event.zoom_webinar_id = meeting_id
                db.session.commit()
                app.logger.info(f"Zoom meeting created on edit: {meeting_id}")
            except Exception as e:
                app.logger.error(f"Zoom meeting creation failed on edit (event {id}): {e}")
                flash('Event saved, but Zoom meeting creation failed. '
                      'Check your Zoom credentials.', 'warning')

        if request.form.get('send_notification'):
            try:
                send_event_notification(event)
            except Exception as e:
                app.logger.error(f"Event notification error: {e}")

        flash('Event updated.', 'success')
        return redirect(url_for('admin_events'))

    return render_template('admin/event_form.html', event=event)

@app.route('/admin/event/<int:id>/delete', methods=['POST'])
@login_required
def admin_event_delete(id):
    if not current_user.is_admin:
        abort(403)
    event = Event.query.get_or_404(id)

    if event.zoom_webinar_id:
        try:
            delete_zoom_webinar(event.zoom_webinar_id)
        except Exception as e:
            app.logger.error(f"Zoom meeting deletion failed for event {id}: {e}")

    for att in event.attachments:
        path = os.path.join(
            app.config['UPLOAD_FOLDER'], 'event_attachments', att.stored_name
        )
        try:
            os.remove(path)
        except OSError:
            pass

    db.session.delete(event)
    db.session.commit()
    flash('Event deleted.', 'success')
    return redirect(url_for('admin_events'))

@app.route('/admin/event/<int:id>/fetch-recording', methods=['POST'])
@login_required
def admin_event_fetch_recording(id):
    """Manually trigger a fetch of the Zoom cloud recording URL for a past event."""
    if not current_user.is_admin:
        abort(403)
    event = Event.query.get_or_404(id)
    if not event.zoom_webinar_id:
        flash('This event has no Zoom meeting linked.', 'error')
        return redirect(url_for('admin_events'))
    try:
        url = fetch_recording_url(event.zoom_webinar_id)
        if url:
            event.zoom_recording_url = url
            db.session.commit()
            flash('Recording URL fetched and saved successfully.', 'success')
        else:
            flash('No recording found yet — Zoom may still be processing it. '
                  'Try again later.', 'warning')
    except Exception as e:
        app.logger.error(f"Fetch recording error for event {id}: {e}")
        flash(f'Failed to fetch recording: {e}', 'error')
    return redirect(url_for('admin_events'))

# ===================== ADMIN MEMBERS ROUTES =====================

@app.route('/admin/members')
@login_required
def admin_members():
    if not current_user.is_admin:
        abort(403)
    
    # Get filter parameters
    search = request.args.get('search', '')
    stakeholder_type = request.args.get('stakeholder_type', '')
    
    query = User.query
    
    if search:
        query = query.filter(
            db.or_(
                User.name.ilike(f'%{search}%'),
                User.email.ilike(f'%{search}%'),
                User.organization.ilike(f'%{search}%')
            )
        )
    
    if stakeholder_type:
        query = query.filter_by(stakeholder_type=stakeholder_type)
    
    members = query.order_by(User.created_at.desc()).all()
    
    # Get stakeholder types for filter dropdown
    stakeholder_types = db.session.query(User.stakeholder_type).distinct().all()
    stakeholder_types = [s[0] for s in stakeholder_types]
    
    return render_template('admin/members.html', 
                         members=members, 
                         stakeholder_types=stakeholder_types,
                         search=search,
                         selected_type=stakeholder_type)

@app.route('/admin/member/<int:id>/delete', methods=['POST'])
@login_required
def admin_delete_member(id):
    if not current_user.is_admin:
        abort(403)
    
    # Prevent admin from deleting themselves
    if id == current_user.id:
        flash('You cannot delete your own account.', 'error')
        return redirect(url_for('admin_dashboard'))
    
    user = User.query.get_or_404(id)
    email = user.email
    
    # Check if user has content that might be important
    initiative_count = Initiative.query.filter_by(user_id=id).count()
    question_count = Question.query.filter_by(user_id=id).count()
    recommendation_count = Recommendation.query.filter_by(user_id=id).count()
    
    if initiative_count > 0 or question_count > 0 or recommendation_count > 0:
        flash(f'Cannot delete {email}: User has {initiative_count} initiative(s), {question_count} question(s), and {recommendation_count} recommendation(s). Please delete their content first or reassign it.', 'error')
        return redirect(url_for('admin_dashboard'))
    
    # Delete user's projects and participations
    MemberProject.query.filter_by(user_id=id).delete()
    ProjectParticipation.query.filter_by(user_id=id).delete()
    EventRegistration.query.filter_by(user_id=id).delete()
    Vote.query.filter_by(user_id=id).delete()
    
    # Delete the user
    db.session.delete(user)
    db.session.commit()
    
    flash(f'Member {email} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/member/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def admin_edit_member(id):
    if not current_user.is_admin:
        abort(403)
    user = User.query.get_or_404(id)
    if request.method == 'POST':
        new_name = request.form.get('name', '').strip()
        new_email = request.form.get('email', '').lower().strip()
        new_organization = request.form.get('organization', '').strip()
        new_stakeholder_type = request.form.get('stakeholder_type', '').strip()
        new_country = request.form.get('country', '').strip()

        errors = []
        if not new_name:
            errors.append('Name is required.')
        if not new_email:
            errors.append('Email is required.')
        if not new_organization:
            errors.append('Organization is required.')
        if new_email and new_email != user.email:
            if User.query.filter_by(email=new_email).first():
                errors.append('That email address is already in use by another account.')

        if errors:
            for e in errors:
                flash(e, 'error')
        else:
            user.name = new_name
            user.email = new_email
            user.organization = new_organization
            user.stakeholder_type = new_stakeholder_type
            user.country = new_country
            db.session.commit()
            flash(f'Member {user.email} updated successfully.', 'success')
            return redirect(url_for('admin_members'))

    stakeholder_types = ['Government', 'NGO / Civil Society', 'Development Partner / Donor',
                         'Academic / Research', 'UN Agency', 'Private Sector']
    return render_template('admin/edit_member.html', user=user, stakeholder_types=stakeholder_types)

# ===================== MEMBER PROJECT SUBMISSION =====================

@app.route('/project/new', methods=['GET', 'POST'])
@login_required
def member_new_project():
    if request.method == 'POST':
        project = Project(
            title=request.form.get('title'),
            description=request.form.get('description'),
            deadline=datetime.fromisoformat(request.form.get('deadline')),
            start_date=datetime.fromisoformat(request.form.get('start_date')) if request.form.get('start_date') else None,
            is_published=False,
            submitted_by=current_user.id
        )
        db.session.add(project)
        db.session.flush()
        titles = request.form.getlist('activity_title[]')
        descs = request.form.getlist('activity_desc[]')
        deadlines = request.form.getlist('activity_deadline[]')
        for i, title in enumerate(titles):
            if title.strip():
                activity = ProjectActivity(
                    project_id=project.id,
                    title=title,
                    description=descs[i] if i < len(descs) else '',
                    deadline=datetime.fromisoformat(deadlines[i]) if i < len(deadlines) and deadlines[i] else None
                )
                db.session.add(activity)
        db.session.commit()
        flash('Project submitted for admin approval. You will be notified when it goes live.', 'success')
        return redirect(url_for('dashboard'))
    return render_template('project_form_member.html')


# ===================== PROJECTS PUBLIC =====================

@app.route('/projects')
def projects():
    now = datetime.utcnow()
    current_projects = Project.query.filter(
        Project.deadline > now,
        Project.is_published == True,
        (Project.start_date == None) | (Project.start_date <= now)
    ).order_by(Project.deadline.asc()).all()
    upcoming_projects = Project.query.filter(
        Project.start_date > now,
        Project.is_published == True
    ).order_by(Project.start_date.asc()).all()
    past_projects = Project.query.filter(
        Project.deadline <= now,
        Project.is_published == True
    ).order_by(Project.deadline.desc()).limit(5).all()
    stats = {
        'total_projects': Project.query.count(),
        'active_projects': len(current_projects),
        'total_activities': ProjectActivity.query.count(),
        'total_participants': ProjectParticipation.query.distinct(ProjectParticipation.user_id).count()
    }
    for project in current_projects + past_projects:
        project.participant_count = ProjectParticipation.query.filter_by(project_id=project.id).distinct(ProjectParticipation.user_id).count()
    return render_template('projects.html', 
                         current_projects=current_projects,
                         upcoming_projects=upcoming_projects,
                         past_projects=past_projects,
                         stats=stats)

@app.route('/project/<int:id>')
def project_detail(id):
    project = Project.query.get_or_404(id)
    now = datetime.utcnow()
    delta = project.deadline - now
    days_left = delta.days if delta.total_seconds() > 0 else -1
    already_participating = False
    if current_user.is_authenticated:
        already_participating = ProjectParticipation.query.filter_by(
            project_id=id, 
            user_id=current_user.id
        ).first() is not None
    participant_count = ProjectParticipation.query.filter_by(project_id=id).distinct(ProjectParticipation.user_id).count()
    return render_template('project_detail.html', 
                         project=project, 
                         now=now, 
                         days_left=days_left,
                         already_participating=already_participating,
                         participant_count=participant_count)

@app.route('/project/<int:id>/participate', methods=['POST'])
@login_required
def participate_project(id):
    project = Project.query.get_or_404(id)
    if project.deadline < datetime.utcnow():
        flash('This project is no longer accepting participants.', 'error')
        return redirect(url_for('project_detail', id=id))
    existing = ProjectParticipation.query.filter_by(
        project_id=id, 
        user_id=current_user.id
    ).first()
    if existing:
        flash('You are already participating in this project.', 'info')
        return redirect(url_for('project_detail', id=id))
    activity_ids = request.form.getlist('activities', type=int)
    if not activity_ids:
        flash('Please select at least one activity.', 'error')
        return redirect(url_for('project_detail', id=id))
    signed_up_activities = []
    for activity_id in activity_ids:
        activity = ProjectActivity.query.get(activity_id)
        if activity and activity.project_id == id:
            if activity.deadline and activity.deadline < datetime.utcnow():
                continue
            participation = ProjectParticipation(
                project_id=id,
                activity_id=activity_id,
                user_id=current_user.id
            )
            db.session.add(participation)
            signed_up_activities.append(activity)
    db.session.commit()
    award_points(current_user, 'project_participated')
    flash('You have successfully joined the project!', 'success')

    # Notify the participant with a confirmation email
    try:
        send_project_signup_confirmation(current_user, project, signed_up_activities)
    except Exception as e:
        app.logger.error(f"Project signup confirmation email error: {e}")

    # Alert the admin about the new sign-up
    try:
        admin = User.query.filter_by(is_admin=True).first()
        if admin:
            send_project_signup_admin_alert(admin.email, current_user, project, signed_up_activities)
    except Exception as e:
        app.logger.error(f"Project signup admin alert email error: {e}")

    return redirect(url_for('dashboard'))

@app.route('/unsubscribe', methods=['GET', 'POST'])
def unsubscribe():
    """Token-based unsubscribe page.
    - Known members: set is_subscribed=False on their User record.
    - Unknown emails: added to BlockedEmail so future import emails skip them.
    """
    import hmac, hashlib

    def _make_token(email):
        secret = app.config.get('SECRET_KEY', 'fallback-secret')
        return hmac.new(secret.encode(), email.lower().encode(), hashlib.sha256).hexdigest()

    if request.method == 'POST':
        email = request.form.get('email', '').lower().strip()
        token = request.form.get('token', '')
        if not email or not token or not hmac.compare_digest(token, _make_token(email)):
            return render_template('unsubscribe.html', error=True, email=email, confirmed=False)
        user = User.query.filter_by(email=email).first()
        if user:
            user.is_subscribed = False
            db.session.commit()
        else:
            # Not a member — add to blocked list so import notifications are suppressed
            if not BlockedEmail.query.filter_by(email=email).first():
                db.session.add(BlockedEmail(email=email))
                db.session.commit()
        return render_template('unsubscribe.html', confirmed=True, email=email, error=False)

    email = request.args.get('email', '').lower().strip()
    token = request.args.get('token', '')
    if not email or not token or not hmac.compare_digest(token, _make_token(email)):
        return render_template('unsubscribe.html', error=True, email='', confirmed=False)
    return render_template('unsubscribe.html', confirmed=False, error=False, email=email, token=token)


# ===================== POLICY DEVELOPMENTS =====================

@app.route('/policy-developments', methods=['GET', 'POST'])
def policy_developments():
    """Public listing of approved policy developments + URL submission form."""
    if request.method == 'POST':
        if not current_user.is_authenticated:
            flash('Please log in to submit a policy development.', 'warning')
            return redirect(url_for('login'))

        url_input = request.form.get('source_url', '').strip()
        country   = request.form.get('country', '').strip()[:100]

        if not url_input or not url_input.startswith(('http://', 'https://')):
            flash('Please enter a valid URL starting with http:// or https://', 'error')
            return redirect(url_for('policy_developments'))

        # Duplicate guard
        existing = PolicyDevelopment.query.filter_by(source_url=url_input).first()
        if existing:
            flash('This URL has already been submitted. Thank you!', 'info')
            return redirect(url_for('policy_developments'))

        policy = PolicyDevelopment(
            source_url=url_input,
            country=country or None,
            submitted_by=current_user.id if current_user.is_authenticated else None,
            processing_status='pending',
        )
        db.session.add(policy)
        db.session.commit()

        t = threading.Thread(
            target=_process_policy_async,
            args=(app, policy.id),
            daemon=True
        )
        t.start()

        flash(
            'Thank you! Your submission is being processed and will appear after admin review.',
            'success'
        )
        return redirect(url_for('policy_developments'))

    page       = request.args.get('page', 1, type=int)
    country_f  = request.args.get('country', '').strip()
    tag_f      = request.args.get('tag', '').strip()

    query = PolicyDevelopment.query.filter_by(is_published=True)
    if country_f:
        query = query.filter(PolicyDevelopment.country.ilike(f'%{country_f}%'))
    if tag_f:
        query = query.join(PolicyDevelopment.tags).filter(Tag.name == tag_f)

    policies = query.order_by(
        PolicyDevelopment.published_date.desc().nullslast(),
        PolicyDevelopment.created_at.desc()
    ).paginate(page=page, per_page=12, error_out=False)

    countries = (
        db.session.query(PolicyDevelopment.country)
        .filter(PolicyDevelopment.is_published == True,
                PolicyDevelopment.country != None)
        .distinct().order_by(PolicyDevelopment.country).all()
    )
    countries = [c[0] for c in countries if c[0]]

    return render_template(
        'policy_developments.html',
        policies=policies,
        countries=countries,
        active_country=country_f,
        active_tag=tag_f,
    )


@app.route('/policy-developments/<int:id>')
def view_policy(id):
    policy = PolicyDevelopment.query.filter_by(id=id, is_published=True).first_or_404()
    return render_template('policy_development_detail.html', policy=policy)


@app.route('/admin/policy-developments')
@login_required
def admin_policy_list():
    """Admin view of all policy submissions including pending/failed."""
    if not current_user.is_admin:
        abort(403)
    policies = PolicyDevelopment.query.order_by(PolicyDevelopment.created_at.desc()).all()
    return render_template('admin/policy_list.html', policies=policies)


@app.route('/admin/policy/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def admin_edit_policy(id):
    """Edit a policy development (title, summary, extracted content, country, date, etc.)."""
    if not current_user.is_admin:
        abort(403)
    policy = PolicyDevelopment.query.get_or_404(id)
    if request.method == 'POST':
        policy.title = request.form.get('title', '').strip()[:300] or policy.title
        policy.short_summary = request.form.get('short_summary', '').strip()[:500] or None
        policy.extracted_text = request.form.get('extracted_text', '').strip() or None
        policy.country = request.form.get('country', '').strip()[:100] or None
        raw_date = request.form.get('published_date', '').strip()
        if raw_date:
            try:
                from datetime import date as _date
                policy.published_date = _date.fromisoformat(raw_date)
            except Exception:
                pass
        else:
            policy.published_date = None
        policy.updated_at = datetime.utcnow()
        db.session.commit()
        flash('Policy development updated.', 'success')
        return redirect(url_for('admin_policy_list'))
    return render_template('admin/edit_policy.html', policy=policy)


@app.route('/admin/policy/<int:id>/delete', methods=['POST'])
@login_required
def admin_delete_policy(id):
    if not current_user.is_admin:
        abort(403)
    policy = PolicyDevelopment.query.get_or_404(id)
    # Delete send queue entry first (NOT NULL FK — must go before the policy row)
    PolicySendQueue.query.filter_by(policy_id=id).delete()
    db.session.flush()
    db.session.delete(policy)
    db.session.commit()
    flash('Policy development deleted.', 'success')
    return redirect(request.referrer or url_for('admin_policy_list'))


@app.route('/admin/policy/<int:id>/reprocess', methods=['POST'])
@login_required
def admin_reprocess_policy(id):
    """Re-trigger background processing for a failed/pending policy item."""
    if not current_user.is_admin:
        abort(403)
    policy = PolicyDevelopment.query.get_or_404(id)
    policy.processing_status = 'pending'
    policy.processing_error  = None
    db.session.commit()
    t = threading.Thread(
        target=_process_policy_async, args=(app, policy.id), daemon=True
    )
    t.start()
    flash('Reprocessing started.', 'info')
    return redirect(request.referrer or url_for('admin_approvals'))


# ===================== API ROUTES =====================

@app.route('/health')
def health_check():
    return {"status": "ok", "message": "Application is running"}, 200

@app.route('/api/translate', methods=['POST'])
def api_translate():
    data = request.get_json()
    text = data.get('text', '')
    target_lang = data.get('lang', 'fr')
    source_lang = data.get('source_lang', 'auto')
    try:
        translated = translate_text(text, target_lang, source_lang)
        return jsonify({'success': True, 'translation': translated})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/stats')
def api_stats():
    from sqlalchemy import func, distinct
    org_count = db.session.query(
        func.count(distinct(func.lower(func.trim(User.organization))))
    ).filter(
        User.is_approved == True,
        User.organization != None,
        func.trim(User.organization) != ''
    ).scalar()
    stats = {
        'members': User.query.filter_by(is_approved=True).count(),
        'initiatives': Initiative.query.filter_by(is_published=True).count(),
        'organizations': org_count or 0
    }
    return jsonify(stats)


@app.route('/api/organisations')
def api_organisations():
    """Return distinct organisation names matching the query (3+ chars). Used by the
    registration form autocomplete so users can select an existing organisation."""
    q = request.args.get('q', '').strip()
    if len(q) < 3:
        return jsonify([])
    results = (
        db.session.query(User.organization)
        .filter(User.organization.ilike(f'%{q}%'))
        .distinct()
        .order_by(User.organization)
        .limit(10)
        .all()
    )
    return jsonify([r[0] for r in results if r[0]])

# ===================== TEMPLATE FILTER =====================

@app.template_filter('format_date')
def format_date(value):
    if value is None:
        return ""
    return value.strftime('%B %d, %Y')


@app.template_filter('markdown')
def markdown_filter(text):
    """Convert Markdown to safe HTML and mark as safe for Jinja2."""
    if not text:
        return ''
    
    # Generate HTML from Markdown
    md = mistune.create_markdown()
    html = md(text)
    
    # Define what tags are allowed (Sanitization)
    allowed_tags = [
        'p', 'br', 'strong', 'em', 'u', 'strike', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
        'ul', 'ol', 'li', 'a', 'blockquote', 'code', 'pre', 'hr', 'img'
    ]
    allowed_attrs = {
        'a': ['href', 'title', 'target'],
        'img': ['src', 'alt', 'title'],
        'code': ['class'],
        'pre': ['class']
    }
    
    # Clean the HTML to prevent XSS
    cleaned_html = bleach.clean(html, tags=allowed_tags, attributes=allowed_attrs, strip=False)
    
    # CRITICAL FIX: Wrap in Markup() so Jinja2 doesn't escape it
    return Markup(cleaned_html)

# ===================== INIT DB COMMAND =====================

@app.cli.command('set-admin-password')
@click.argument('password')
def set_admin_password(password):
    from werkzeug.security import generate_password_hash
    admin = User.query.filter_by(is_admin=True).first()
    if not admin:
        print('No admin user found.')
        return
    admin.password_hash = generate_password_hash(password)
    db.session.commit()
    print(f'Password set for {admin.email}')
    
@app.cli.command('sync-all-points')
def sync_all_points():
    users = User.query.all()
    for user in users:
        award_points(user, commit=False)
    db.session.commit()
    print("All user points have been successfully synced with their database history!")

@app.cli.command('init-db')
def init_db():
    db.create_all()
    # Create admin user
    admin = User(
        email=Config.ADMIN_EMAIL,
        name='Administrator',
        organization='AU ECED-FLN',
        stakeholder_type='Government',
        country='Ethiopia',
        is_approved=True,
        is_admin=True
    )
    db.session.add(admin)
    # Default registration fields
    fields = [
        RegistrationField(field_name='expertise', field_label='Area of Expertise', field_type='textarea'),
        RegistrationField(field_name='website', field_label='Organization Website', field_type='text', is_required=False)
    ]
    for field in fields:
        db.session.add(field)
    db.session.commit()
    print('Database initialized.')

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True, host='0.0.0.0', port=5000)
