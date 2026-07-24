# -*- coding: utf-8 -*-
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
import time
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
    send_individual_invitation_email,
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
    send_single_document_notification,
    send_bulk_documents_digest,
    send_certificate_email,
)
from utils.ai_services import generate_title_description, vet_tags_nvidia, rank_members_by_query, clean_tags_for_polls, score_initiative_quality, detect_language, generate_summary, clean_title
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
    value = db.Column(db.Text)


class PageView(db.Model):
    """Lightweight site-usage analytics — one row per HTML page request."""
    id = db.Column(db.Integer, primary_key=True)
    path = db.Column(db.String(300), nullable=False, index=True)
    visitor_id = db.Column(db.String(36), index=True)   # anonymous cookie id
    is_authenticated = db.Column(db.Boolean, default=False)
    referrer_host = db.Column(db.String(255))            # external referrer host only
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class Certificate(db.Model):
    """Contributor certificate, issued (in individual-first mode) the first time a
    member has a published article. Rendered as a printable page at /certificate/<token>."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), unique=True, nullable=False)
    token = db.Column(db.String(32), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref=db.backref('certificate', uselist=False))


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


class StakeholderType(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    is_member_state = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)
    order = db.Column(db.Integer, default=0)


class Label(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text, nullable=False, default='')
    category = db.Column(db.String(50), default='general')


# NEW MODELS FOR EVENTS AND POLLS
class EmailTemplate(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    key          = db.Column(db.String(80), unique=True, nullable=False)
    subject      = db.Column(db.String(500), nullable=False)
    title        = db.Column(db.String(500), nullable=False, default='')
    body_html    = db.Column(db.Text, nullable=False)
    is_confirmed = db.Column(db.Boolean, default=False)


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
    view_count        = db.Column(db.Integer, default=0, nullable=False)
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


class DocumentSendQueue(db.Model):
    """Send queue for approved DocumentLibrary items."""
    id          = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey('document_library.id'),
                            nullable=False, unique=True)
    queued_at   = db.Column(db.DateTime, default=datetime.utcnow)
    sent_at     = db.Column(db.DateTime, nullable=True)

    document = db.relationship('DocumentLibrary', backref=db.backref('send_queue_entry', cascade='all, delete-orphan', uselist=False))


class LearnMoreRequest(db.Model):
    """Tracks 'Request to Learn More' clicks on initiatives.

    Each authenticated user may send at most three such requests per initiative
    per calendar month.  The record is used both to enforce the rate-limit and
    to surface counts in the member-export CSV.
    """
    __tablename__ = 'learn_more_request'

    id              = db.Column(db.Integer, primary_key=True)
    requester_id    = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'),
                                nullable=False)
    initiative_id   = db.Column(db.Integer, db.ForeignKey('initiative.id', ondelete='CASCADE'),
                                nullable=False)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    requester  = db.relationship('User',       foreign_keys=[requester_id],
                                 backref='learn_more_sent')
    initiative = db.relationship('Initiative', foreign_keys=[initiative_id],
                                 backref='learn_more_requests')


class DocumentLibrary(db.Model):
    """Member-uploaded documents with AI-extracted metadata.

    Workflow:
      1. Member uploads a document (PDF, DOC, DOCX, etc.)
      2. Background thread extracts text from the document
      3. AI extracts metadata: title, year, tags, description
      4. Admin reviews and approves before it goes public
    """
    id                = db.Column(db.Integer, primary_key=True)
    title             = db.Column(db.String(300), nullable=True)
    description       = db.Column(db.Text, nullable=True)
    year_published    = db.Column(db.Integer, nullable=True)
    filename          = db.Column(db.String(300), nullable=False)    # original filename
    stored_name       = db.Column(db.String(300), nullable=False)    # UUID-based name on disk
    file_size         = db.Column(db.Integer, nullable=True)       # bytes
    file_type         = db.Column(db.String(50), nullable=True)     # mime type or extension
    extracted_text    = db.Column(db.Text, nullable=True)            # raw text extracted from doc
    tags              = db.relationship('Tag', secondary='document_tags', backref='documents')
    is_published      = db.Column(db.Boolean, default=False)
    processing_status = db.Column(db.String(50), default='pending')
    # 'pending' -> uploaded, awaiting text extraction
    # 'extracting' -> text extraction in progress
    # 'ready' -> AI done, awaiting admin approval
    # 'failed' -> error during extraction or AI processing
    processing_error  = db.Column(db.String(500), nullable=True)
    submitted_by      = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    view_count        = db.Column(db.Integer, default=0, nullable=False)
    created_at        = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at        = db.Column(db.DateTime, default=datetime.utcnow)

    submitter = db.relationship('User', foreign_keys=[submitted_by])


document_tags = db.Table('document_tags',
    db.Column('document_id', db.Integer, db.ForeignKey('document_library.id'), primary_key=True),
    db.Column('tag_id', db.Integer, db.ForeignKey('tag.id'), primary_key=True)
)


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


# ===================== STAKEHOLDER TYPES =====================

DEFAULT_STAKEHOLDER_TYPES = [
    'Government', 'NGO / Civil Society',
    'Development Partner / Donor', 'Academic / Research',
    'UN Agency', 'Private Sector'
]


def get_stakeholder_types():
    try:
        types = StakeholderType.query.filter_by(is_active=True).order_by(StakeholderType.order).all()
        if types:
            return [t.name for t in types]
    except Exception:
        pass
    return DEFAULT_STAKEHOLDER_TYPES


def get_member_state_type():
    # The "Member State" type was merged into "Government"; no type is flagged
    # member-state anymore. Return '' so it matches no user's stakeholder_type.
    try:
        mst = StakeholderType.query.filter_by(is_member_state=True, is_active=True).first()
        if mst:
            return mst.name
    except Exception:
        pass
    return ''


def get_label(key, default=''):
    try:
        label = Label.query.filter_by(key=key).first()
        if label and label.value:
            return label.value
    except Exception:
        pass
    return default


# ===================== SITE CONFIG / FRONT-END OVERRIDES =====================

# Canonical label keys with their default English values.
# Admin can override any of these via the Labels admin page.
LABEL_DEFAULTS = {
    # Nav / Auth
    'nav_dashboard': 'Dashboard',
    'nav_admin': 'Admin',
    'nav_logout': 'Logout',
    'nav_login': 'Login',
    'nav_join': 'Join Cluster',
    # Hero CTA links
    'hero_cta_stakeholders': 'Stakeholders →',
    'hero_cta_initiatives': 'Initiatives →',
    'hero_cta_documents': 'Policy Documents →',
    # Homepage stat cards
    'stat_countries': 'Countries',
    'stat_initiatives': 'ECED Initiatives',
    'stat_organizations': 'Organizations',
    'stat_members': 'Practitioners',
    # Homepage sections
    'section_stakeholder_ecosystem': 'Stakeholder Ecosystem',
    'section_recent_initiatives': 'Recent Initiatives',
    'section_view_all': 'View All',
    'read_more': 'Read More',
    'cta_title': 'Join the Cluster',
    'cta_text': 'Share your ECED/FLN initiatives and connect with stakeholders across Africa.',
    'cta_button': 'Request Access',
    # Form labels
    'form_full_name': 'Full Name',
    'form_email': 'Email Address',
    'form_organization': 'Organization',
    'form_country': 'Country',
    'form_stakeholder_type': 'Stakeholder Type',
    # Actions
    'btn_save': 'Save',
    'btn_cancel': 'Cancel',
    'btn_delete': 'Delete',
    'btn_edit': 'Edit',
    'btn_submit': 'Submit',
    'btn_search': 'Search',
    # Dashboard
    'dashboard_title': 'Dashboard',
    'dashboard_my_initiatives': 'My Initiatives',
    'dashboard_my_projects': 'My Projects',
    'dashboard_my_events': 'My Events',
    # Members
    'members_title': 'Stakeholders',
    'members_filter': 'Filter by type',
    # Pages
    'page_initiatives': 'Initiatives',
    'page_documents': 'Policy Documents',
    'page_events': 'Events',
    'page_stats': 'Participation',
    'page_about': 'About Us',
    # Stats
    'stats_title': 'Participation',
    'stats_stakeholder_breakdown': 'Stakeholder Breakdown',
    'stats_growth': 'Member Growth',
    # Footer
    'footer_tagline': 'Accelerating Early Childhood Education and Development & Foundational Learning across Africa.',
    # Register
    'register_title': 'Join the Cluster',
    'register_subtitle': 'Complete the form below to request access to the cluster.',
    # Admin sidebar
    'admin_dashboard': 'Dashboard',
    'admin_approvals': 'Approvals',
    'admin_comments': 'Comments',
    'admin_members': 'Members',
    'admin_initiatives': 'Initiatives',
    'admin_send_queue': 'Send Queue',
    'admin_projects': 'Projects',
    'admin_events': 'Events',
    'admin_policy': 'Policy Developments',
    'admin_documents': 'Document Library',
    'admin_form_fields': 'Form Fields',
    'admin_appearance': 'Appearance',
    'admin_labels': 'Labels',
    'admin_stakeholder_types': 'Stakeholder Types',
    'admin_analytics': 'Site Analytics',
    'admin_settings': 'Settings',
    'admin_import_initiatives': 'Import Initiatives',
    'admin_import_members': 'Import Members',
    'admin_export_members': 'Export Members CSV',
    'admin_view_site': 'View Site',
    'admin_my_dashboard': 'My Dashboard',
    'admin_bulk_import': 'Bulk Import',
    'admin_quick_links': 'Quick Links',
    'admin_administration': 'Administration',
    'admin_export': 'Export',


    # Browser tab titles (static pages — full title)
    'page_title_dashboard': 'Dashboard',
    'page_title_login': 'Login',
    'page_title_register': 'Join Cluster',
    'page_title_explore': 'Explore Initiatives',
    'page_title_members': 'Participating Organisations',
    'page_title_search_members': 'Search Stakeholders',
    'page_title_events': 'Events',
    'page_title_polls': 'Polls',
    'page_title_forum': 'Q&A Forum',
    'page_title_documents': 'ECED Policy Documents',
    'page_title_document_upload': 'Upload Document',
    'page_title_projects': 'Projects',
    'page_title_policy': 'ECED Policy Developments',
    'page_title_verify_otp': 'Verify OTP',
    'page_title_profile_edit': 'Edit Profile',
    'page_title_unsubscribe': 'Unsubscribe',
    'page_title_stats': 'Participation',
    'page_title_leaderboard': 'Leaderboard',
    'page_title_discussions': 'Discussions',
    'page_title_initiative_form': 'New Initiative',
    'page_title_event_form': 'Submit an Event',
    'page_title_project_form': 'Submit a Project',
    'page_title_question_form': 'New Question',
    'page_title_ta_form': 'Submit TA Need',
    # Dynamic page — suffix after the variable
    'page_title_suffix': 'AU ECED-FLN Cluster',

    # ── Shared form strings (used across multiple forms) ──
    'form_back_to_dashboard': 'Back to Dashboard',
    'form_submit_review': 'Submit for Review',
    'form_remove': 'Remove',
    'form_markdown_hint': 'You can use <a href="https://www.markdownguide.org/basic-syntax/" target="_blank" rel="noopener">Markdown</a> for formatting (headings, lists, links, etc.).',

    # ── Registration form: initiative section, placeholders, notices, button ──
    'reg_org_ph': 'Start typing your organisation…',
    'reg_country_ph': 'Select Country',
    'reg_stakeholder_ph': 'Select Type',
    'reg_initiative_heading': 'Your ECED-FLN Initiative',
    'reg_initiative_help': 'Share an initiative you have led or contributed to in Early Childhood Education & Development or Foundational Learning. This is how other members will find you through expertise search, and it will be published on the platform once your account is approved.',
    'reg_initiative_title_label': 'Initiative Title',
    'reg_initiative_title_ph': 'e.g., Community-Based Early Literacy Programme in Northern Ghana',
    'reg_initiative_title_min': '5',
    'reg_content_label': 'Full Initiative Description',
    'reg_content_hint': '(Markdown supported)',
    'reg_content_help': "You can use **bold**, *italic*, bullet lists, and headings. This full description will appear on your initiative's dedicated page.",
    'reg_content_ph': 'Describe the initiative in detail: objectives, target beneficiaries, geographic scope, outcomes, lessons learned, etc.',
    'reg_content_min': '300',
    'reg_notice_created': 'Your account will be created immediately. Your initiative will be published and visible on the platform right away.',
    'reg_submit_btn': 'Join the Cluster',

    # ── Initiative (article) form ──
    'initf_heading_new': 'Submit New Initiative',
    'initf_heading_edit': 'Edit Initiative',
    'initf_title_label': 'Initiative Title',
    'initf_short_label': 'Short Description',
    'initf_short_max': '300',
    'initf_short_help': 'A concise summary that will appear in search results and cards.',
    'initf_short_ph': 'Brief summary of your initiative...',
    'initf_short_min': '10',
    'initf_content_label': 'Full Description',
    'initf_content_ph': 'Describe your initiative in detail. Include objectives, target beneficiaries, implementation approach, and any results or lessons learned...',
    'initf_content_min': '300',
    'initf_title_min': '5',
    'initf_tags_label': 'Tags',
    'initf_tags_help': 'Add or remove tags manually, or check below to regenerate from AI.',
    'initf_tags_ph': 'Type a tag and press Enter or comma to add...',
    'initf_regen_label': 'Regenerate tags from updated content (replaces manual tags)',
    'initf_tag_add_btn': 'Add',
    'initf_submit_new': 'Submit Initiative',
    'initf_submit_edit': 'Save Changes',

    # ── Project form ──
    'projf_heading': 'Submit a Project',
    'projf_help': "Propose a collaborative project for the cluster. An admin will review and publish it — you'll receive an email once it goes live.",
    'projf_title_label': 'Project Title',
    'projf_title_ph': 'e.g. Early Grade Reading Assessment – East Africa',
    'projf_desc_label': 'Description',
    'projf_desc_ph': 'Describe the project goals, context, and expected outcomes...',
    'projf_start_label': 'Start Date',
    'projf_start_hint': 'Leave blank if starting immediately.',
    'projf_deadline_label': 'Deadline',
    'projf_activities_label': 'Activities',
    'projf_activities_help': 'Break the project into specific activities that members can sign up for.',
    'projf_activity_title_ph': 'Activity title (e.g. Data collection)',
    'projf_activity_desc_ph': 'Brief description of this activity...',
    'projf_activity_deadline_hint': 'Activity deadline (optional)',
    'projf_add_activity_btn': 'Add Another Activity',


    # ── Question form ──
    'qf_heading_new': 'Ask a Question',
    'qf_heading_edit': 'Edit Question',
    'qf_title_label': 'Question Title',
    'qf_title_ph': 'e.g., How to improve literacy in rural areas?',
    'qf_content_label': 'Detailed Description',
    'qf_content_help': 'Be specific to get better recommendations from experts.',
    'qf_content_ph': 'Provide context, background, and specific details about your question...',
    'qf_notice': 'Your question will be reviewed by an admin before being published.',
    'qf_submit_new': 'Submit Question',
    'qf_submit_edit': 'Save Changes',
}


# Drives the unified "Forms" admin (/admin/forms). Each form groups the editable
# label keys a user sees on it. Field = (label_key, friendly_name, kind) where
# kind is 'text' | 'textarea' | 'number'. Only 'register' also manages the
# structural custom fields (RegistrationField) via the existing field routes.
FORM_DEFINITIONS = [
    {
        'key': 'register', 'name': 'Registration form', 'endpoint': 'register',
        'has_custom_fields': True,
        'groups': [
            {'label': 'Heading & intro', 'fields': [
                ('register_title', 'Form title', 'text'),
                ('register_subtitle', 'Subtitle', 'text'),
                ('reg_submit_btn', 'Submit button', 'text'),
            ]},
            {'label': 'Core field labels & placeholders', 'fields': [
                ('form_full_name', 'Full name — label', 'text'),
                ('form_email', 'Email — label', 'text'),
                ('form_organization', 'Organization — label', 'text'),
                ('reg_org_ph', 'Organization — placeholder', 'text'),
                ('form_country', 'Country — label', 'text'),
                ('reg_country_ph', 'Country — placeholder', 'text'),
                ('form_stakeholder_type', 'Stakeholder type — label', 'text'),
                ('reg_stakeholder_ph', 'Stakeholder type — placeholder', 'text'),
            ]},
            {'label': 'Initiative section', 'fields': [
                ('reg_initiative_heading', 'Section heading', 'text'),
                ('reg_initiative_help', 'Section help text', 'textarea'),
                ('reg_initiative_title_label', 'Initiative title — label', 'text'),
                ('reg_initiative_title_ph', 'Initiative title — placeholder', 'text'),
                ('reg_initiative_title_min', 'Initiative title — min words', 'number'),
                ('reg_content_label', 'Full description — label', 'text'),
                ('reg_content_hint', 'Full description — hint', 'text'),
                ('reg_content_help', 'Full description — help text', 'textarea'),
                ('reg_content_ph', 'Full description — placeholder', 'textarea'),
                ('reg_content_min', 'Full description — min words', 'number'),
            ]},
            {'label': 'Notices', 'fields': [
                ('reg_notice_created', 'After-submit notice', 'textarea'),
            ]},
        ],
    },
    {
        'key': 'initiative', 'name': 'Initiative form', 'endpoint': 'new_initiative',
        'groups': [
            {'label': 'Headings & buttons', 'fields': [
                ('initf_heading_new', 'Heading (new)', 'text'),
                ('initf_heading_edit', 'Heading (edit)', 'text'),
                ('initf_submit_new', 'Submit button (new)', 'text'),
                ('initf_submit_edit', 'Submit button (edit)', 'text'),
            ]},
            {'label': 'Fields', 'fields': [
                ('initf_title_label', 'Title — label', 'text'),
                ('initf_title_min', 'Title — min words', 'number'),
                ('initf_short_label', 'Short description — label', 'text'),
                ('initf_short_max', 'Short description — max characters', 'number'),
                ('initf_short_min', 'Short description — min words', 'number'),
                ('initf_short_help', 'Short description — help', 'text'),
                ('initf_short_ph', 'Short description — placeholder', 'text'),
                ('initf_content_label', 'Full description — label', 'text'),
                ('initf_content_min', 'Full description — min words', 'number'),
                ('initf_content_ph', 'Full description — placeholder', 'textarea'),
                ('initf_tags_label', 'Tags — label', 'text'),
                ('initf_tags_help', 'Tags — help', 'text'),
                ('initf_tags_ph', 'Tags — placeholder', 'text'),
                ('initf_regen_label', 'Regenerate-tags checkbox', 'text'),
                ('initf_tag_add_btn', 'Add-tag button', 'text'),
            ]},
        ],
    },
    {
        'key': 'project', 'name': 'Project form', 'endpoint': 'member_new_project',
        'groups': [
            {'label': 'Heading & intro', 'fields': [
                ('projf_heading', 'Heading', 'text'),
                ('projf_help', 'Intro help text', 'textarea'),
            ]},
            {'label': 'Fields', 'fields': [
                ('projf_title_label', 'Title — label', 'text'),
                ('projf_title_ph', 'Title — placeholder', 'text'),
                ('projf_desc_label', 'Description — label', 'text'),
                ('projf_desc_ph', 'Description — placeholder', 'text'),
                ('projf_start_label', 'Start date — label', 'text'),
                ('projf_start_hint', 'Start date — hint', 'text'),
                ('projf_deadline_label', 'Deadline — label', 'text'),
                ('projf_activities_label', 'Activities — label', 'text'),
                ('projf_activities_help', 'Activities — help', 'text'),
                ('projf_activity_title_ph', 'Activity title — placeholder', 'text'),
                ('projf_activity_desc_ph', 'Activity description — placeholder', 'text'),
                ('projf_activity_deadline_hint', 'Activity deadline — hint', 'text'),
                ('projf_add_activity_btn', 'Add-activity button', 'text'),
            ]},
        ],
    },
    {
        'key': 'question', 'name': 'Question form', 'endpoint': 'new_question',
        'groups': [
            {'label': 'Headings, notice & buttons', 'fields': [
                ('qf_heading_new', 'Heading (new)', 'text'),
                ('qf_heading_edit', 'Heading (edit)', 'text'),
                ('qf_notice', 'Intro notice', 'textarea'),
                ('qf_submit_new', 'Submit button (new)', 'text'),
                ('qf_submit_edit', 'Submit button (edit)', 'text'),
            ]},
            {'label': 'Fields', 'fields': [
                ('qf_title_label', 'Title — label', 'text'),
                ('qf_title_ph', 'Title — placeholder', 'text'),
                ('qf_content_label', 'Description — label', 'text'),
                ('qf_content_help', 'Description — help', 'text'),
                ('qf_content_ph', 'Description — placeholder', 'textarea'),
            ]},
        ],
    },
]


DEFAULT_SITE_NAME = 'AU ECED-FLN'
DEFAULT_SITE_TAGLINE = ('Accelerating Early Childhood Education and Development & '
                        'Foundational Learning across Africa.')
DEFAULT_HERO_IMAGE = 'images/au-eced-logo.png'   # static-relative path
DEFAULT_HERO_HEADING = 'African Union ECED-FLN Cluster'
DEFAULT_HERO_TEXT = ('Connecting experts and organizations to accelerate Early Childhood '
                     'Education and Development & Foundational Learning across Africa.')

# Canonical primary-nav items. Site admins can hide or rename each one; the
# `members` item is also auto-relabelled in individual-first mode.
NAV_ITEMS = [
    {'key': 'home',        'endpoint': 'index',     'label': 'Home'},
    {'key': 'initiatives', 'endpoint': 'search',    'label': 'Initiatives'},
    {'key': 'documents',   'endpoint': 'documents', 'label': 'Policy Documents'},
    {'key': 'events',      'endpoint': 'events',    'label': 'Events'},
    {'key': 'members',     'endpoint': 'members',   'label': 'Stakeholders'},
    {'key': 'stats',       'endpoint': 'stats',     'label': 'Participation'},
    {'key': 'about',       'url': 'https://ecedcluster.africa/', 'label': 'About Us', 'external': True},
]


def is_certificates_enabled():
    return get_setting('certificates_enabled', 'false').lower() == 'true'


# ── AI quality-scoring health ────────────────────────────────────────────────
# If scoring is unavailable (e.g. expired API key) we must NOT auto-publish or
# auto-approve — everything is held for admin review, and admins are warned.
def record_ai_scoring_result(ok):
    try:
        if ok:
            if get_setting('ai_scoring_healthy', 'true') != 'true':
                set_setting('ai_scoring_healthy', 'true')
        else:
            set_setting('ai_scoring_healthy', 'false')
            set_setting('ai_scoring_last_failure', datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC'))
    except Exception:
        pass


def is_ai_scoring_healthy():
    return get_setting('ai_scoring_healthy', 'true') == 'true'


# ── Lightweight in-memory rate limiter (per-process backstop against floods) ──
_RATE_BUCKETS = {}
_RATE_LOCK = threading.Lock()


def rate_ok(key, max_hits, window_seconds):
    """Return False if `key` has already hit `max_hits` within the window."""
    now = time.time()
    with _RATE_LOCK:
        hits = [t for t in _RATE_BUCKETS.get(key, []) if now - t < window_seconds]
        if len(hits) >= max_hits:
            _RATE_BUCKETS[key] = hits
            return False
        hits.append(now)
        _RATE_BUCKETS[key] = hits
        return True


def client_ip():
    fwd = request.headers.get('X-Forwarded-For', '')
    if fwd:
        return fwd.split(',')[0].strip()
    return request.remote_addr or 'unknown'


def get_menu_overrides():
    """Return {key: {'hidden': bool, 'label': str}} from the stored JSON, safely."""
    try:
        return json.loads(get_setting('menu_overrides', '') or '{}')
    except (ValueError, TypeError):
        return {}


def build_nav():
    """Resolve the visible primary-nav items, applying admin show/hide/rename overrides."""
    overrides = get_menu_overrides()
    nav = []
    for item in NAV_ITEMS:
        ov = overrides.get(item['key'], {})
        if ov.get('hidden'):
            continue
        label = (ov.get('label') or '').strip() or item['label']
        try:
            href = item['url'] if item.get('external') else url_for(item['endpoint'])
        except Exception:
            continue
        nav.append({
            'key': item['key'], 'label': label, 'href': href,
            'external': item.get('external', False),
        })
    return nav


@app.template_global()
def label(key, default=''):
    """Return the admin-overridden value for *key*, or *default* (which falls
    back to LABEL_DEFAULTS[key] if omitted)."""
    if not default and key in LABEL_DEFAULTS:
        default = LABEL_DEFAULTS[key]
    return get_label(key, default)


@app.context_processor
def inject_site_config():
    """Expose site branding + computed nav + labels to every template."""
    try:
        name = get_setting('site_name', DEFAULT_SITE_NAME) or DEFAULT_SITE_NAME
        site = {
            'name': name,
            'tagline': get_setting('site_tagline', DEFAULT_SITE_TAGLINE) or DEFAULT_SITE_TAGLINE,
            'certificates_enabled': is_certificates_enabled(),
            'hero_image': url_for('custom_hero_image') if get_setting('hero_image_data') else url_for('static', filename=DEFAULT_HERO_IMAGE),
            'hero_heading': get_setting('hero_heading') or DEFAULT_HERO_HEADING,
            'hero_text': get_setting('hero_text') or DEFAULT_HERO_TEXT,
            'footer_note': (get_setting('footer_note')
                            or f'© 2026 {name}. This platform is open source.'),
        }
        resolved = {}
        for k, v in LABEL_DEFAULTS.items():
            resolved[k] = get_label(k, v)
        return {
            'site': site,
            'nav': build_nav(),
            'labels': resolved,
            'stakeholder_types': get_stakeholder_types(),
            'member_state_type': get_member_state_type(),
            'ai_scoring_healthy': is_ai_scoring_healthy(),
            'ai_scoring_last_failure': get_setting('ai_scoring_last_failure', ''),
        }
    except Exception:
        return {
            'site': {'name': DEFAULT_SITE_NAME, 'tagline': DEFAULT_SITE_TAGLINE,
                     'certificates_enabled': False, 'hero_image': DEFAULT_HERO_IMAGE,
                     'hero_heading': DEFAULT_HERO_HEADING, 'hero_text': DEFAULT_HERO_TEXT,
                     'footer_note': f'© 2026 {DEFAULT_SITE_NAME}. This platform is open source.'},
            'nav': [],
            'labels': dict(LABEL_DEFAULTS),
            'stakeholder_types': DEFAULT_STAKEHOLDER_TYPES,
            'member_state_type': '',
            'ai_scoring_healthy': True,
            'ai_scoring_last_failure': '',
        }


# ===================== CONTRIBUTOR CERTIFICATES =====================

def grant_certificate(user, send_notification=True):
    """Idempotently issue a contributor certificate for `user`.
    Returns (certificate, created_bool). Sends a notification email only on first issue."""
    if hasattr(user, '_get_current_object'):
        user = user._get_current_object()
    existing = Certificate.query.filter_by(user_id=user.id).first()
    if existing:
        return existing, False
    cert = Certificate(user_id=user.id, token=uuid.uuid4().hex, created_at=datetime.utcnow())
    db.session.add(cert)
    db.session.commit()
    if send_notification:
        try:
            cert_url = (Config.APP_URL or '').rstrip('/') + url_for('certificate', token=cert.token)
            site_name = get_setting('site_name', DEFAULT_SITE_NAME) or DEFAULT_SITE_NAME
            send_certificate_email(user, cert_url, site_name)
        except Exception as e:
            app.logger.error(f"Certificate email error for user {user.id}: {e}")
    return cert, True



def _brevo_already_contacted(email):
    """Return True if Brevo already has this email as a contact (i.e. we have emailed them before).
    Returns False on any API error so we do not silently block people."""
    api_key = os.environ.get("BREVO_API_KEY") or app.config.get("BREVO_API_KEY", "")
    if not api_key:
        return False
    try:
        import requests as _requests
        resp = _requests.get(
            f"https://api.brevo.com/v3/contacts/{email}",
            headers={"api-key": api_key, "Accept": "application/json"},
            timeout=5,
        )
        if resp.status_code == 200:
            return True
        if resp.status_code == 404:
            return False
        app.logger.warning(f"Brevo contact lookup unexpected status {resp.status_code} for {email}")
        return False
    except Exception as e:
        app.logger.warning(f"Brevo contact lookup failed for {email}: {e}")
        return False

# Quality-score gates (initiatives are scored 1-5 by AI).
#   < AUTO_PUBLISH_MIN_SCORE  → held unpublished for admin approval (score 1-2)
#   >= AUTO_PUBLISH_MIN_SCORE → auto-published (score 3-5)
#   >= NOTIFY_MIN_SCORE       → also queued for the member-notification broadcast
# A None score (scoring unavailable) is treated as passing, so content is never lost.
AUTO_PUBLISH_MIN_SCORE = 3
NOTIFY_MIN_SCORE = 4


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


def save_document(file_obj):
    """Save an uploaded document to UPLOAD_FOLDER/documents/.
    Returns (original_filename, stored_name, file_size) tuple."""
    original = secure_filename(file_obj.filename)
    ext      = original.rsplit('.', 1)[1].lower() if '.' in original else 'bin'
    stored   = f"{uuid.uuid4().hex}.{ext}"
    folder   = os.path.join(app.config['UPLOAD_FOLDER'], 'documents')
    os.makedirs(folder, exist_ok=True)
    filepath = os.path.join(folder, stored)
    file_obj.save(filepath)
    file_size = os.path.getsize(filepath)
    return original, stored, file_size, ext


def _extract_document_text(filepath, file_ext):
    """Extract raw text from a document file. Supports PDF, DOCX, TXT."""
    text = ""
    try:
        if file_ext == 'pdf':
            try:
                import PyPDF2
                with open(filepath, 'rb') as f:
                    reader = PyPDF2.PdfReader(f)
                    for page in reader.pages:
                        page_text = page.extract_text()
                        if page_text:
                            text += page_text + "\n"
            except Exception as e:
                return None, f"PyPDF2 error: {str(e)[:200]}"

        elif file_ext in ('docx', 'doc'):
            try:
                import docx
                doc = docx.Document(filepath)
                for para in doc.paragraphs:
                    text += para.text + "\n"
            except Exception as e:
                return None, f"python-docx error: {str(e)[:200]}"

        elif file_ext == 'txt':
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                text = f.read()
        else:
            return None, f"Unsupported file type: {file_ext}"

        return text[:15000], None  # Limit to 15k chars for AI processing
    except Exception as e:
        return None, f"Extraction error: {str(e)[:200]}"


def _process_document_async(flask_app, document_id):
    """Background thread: extract text from document, then use AI to extract metadata."""
    with flask_app.app_context():
        doc = DocumentLibrary.query.get(document_id)
        if not doc:
            return

        filepath = os.path.join(
            app.config['UPLOAD_FOLDER'], 'documents', doc.stored_name
        )

        # Step 1: Extract text
        try:
            doc.processing_status = 'extracting'
            db.session.commit()

            raw_text, error = _extract_document_text(filepath, doc.file_type)
            if error:
                doc.processing_status = 'failed'
                doc.processing_error = error
                db.session.commit()
                flask_app.logger.error(f"Document extraction error (id={document_id}): {error}")
                return

            doc.extracted_text = raw_text
            db.session.commit()
        except Exception as e:
            doc.processing_status = 'failed'
            doc.processing_error = f'Text extraction error: {str(e)[:200]}'
            db.session.commit()
            flask_app.logger.error(f"Document extraction error (id={document_id}): {e}")
            return

        # Step 2: AI metadata extraction via NVIDIA
        try:
            from utils.ai_services import call_nvidia_api

            prompt = f"""You are an expert document librarian for African education policy documents.

Below is text extracted from a document. Return ONLY a JSON object (no markdown fences, no preamble) with these keys:
{{
  "title": "<formal title of the document, or a descriptive title if none is obvious>",
  "year_published": <4-digit year if found in the text, or null>,
  "description": "<2-3 sentence summary of what the document covers>",
  "tags": ["<tag1>", "<tag2>", ...]
}}

Rules:
- title should be concise but descriptive (max 200 chars)
- year_published: look for copyright dates, publication dates, or references to years. Return ONLY the 4-digit integer or null.
- description: plain English, max 300 chars
- tags: 3-8 specific keywords related to ECED, FLN, education policy, African education, etc.
- If the document has no meaningful educational content, set description to "No relevant educational content found."
- Return ONLY the JSON object — no explanation, no markdown.

DOCUMENT TEXT:
{raw_text[:8000]}"""

            response_text = call_nvidia_api(prompt, max_tokens=800, temperature=0.1)
            clean = response_text.strip().replace('```json', '').replace('```', '').strip()
            data = json.loads(clean)

        except Exception as e:
            doc.processing_status = 'failed'
            doc.processing_error = f'AI extraction error: {str(e)[:200]}'
            db.session.commit()
            flask_app.logger.error(f"Document AI error (id={document_id}): {e}")
            return

        # Step 3: Persist extracted fields
        try:
            doc.title = (data.get('title') or doc.filename)[:300]
            doc.description = (data.get('description') or '')[:500]

            raw_year = data.get('year_published')
            if raw_year and isinstance(raw_year, int) and 1900 <= raw_year <= 2030:
                doc.year_published = raw_year
            elif raw_year and isinstance(raw_year, str) and raw_year.isdigit():
                year_int = int(raw_year)
                if 1900 <= year_int <= 2030:
                    doc.year_published = year_int

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
                    if tag not in doc.tags:
                        doc.tags.append(tag)
                        tag.usage_count = (tag.usage_count or 0) + 1

            doc.processing_status = 'ready'
            doc.updated_at = datetime.utcnow()
            db.session.commit()

        except Exception as e:
            doc.processing_status = 'failed'
            doc.processing_error = f'DB save error: {str(e)[:200]}'
            db.session.commit()
            flask_app.logger.error(f"Document save error (id={document_id}): {e}")

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

    # 3. Issue a contributor certificate once the member has a published initiative
    #    that scored well (3-5). Low-quality (1-2) and unscored initiatives do not
    #    qualify, even if published.
    if activity in ('initiative_published', 'initiative_approved'):
        try:
            if is_certificates_enabled() and Initiative.query.filter(
                    Initiative.user_id == user.id,
                    Initiative.is_published == True,
                    Initiative.quality_score >= AUTO_PUBLISH_MIN_SCORE).count() > 0:
                grant_certificate(user)
        except Exception as e:
            app.logger.error(f"Certificate grant error for user {getattr(user, 'id', '?')}: {e}")

# ===================== USER LOADER =====================

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ===================== SITE-USAGE ANALYTICS =====================

@app.after_request
def _track_page_view(response):
    """Record one PageView per successful HTML GET. Resilient — analytics
    failures must never break a page render."""
    try:
        if request.method != 'GET':
            return response
        path = request.path or '/'
        if path == '/favicon.ico' or path.startswith(('/static', '/api', '/health')):
            return response
        if response.status_code >= 400:
            return response
        if not (response.content_type or '').startswith('text/html'):
            return response

        # Identify visitor with a long-lived anonymous cookie
        vid = request.cookies.get('vid')
        new_vid = None
        if not vid:
            vid = uuid.uuid4().hex
            new_vid = vid

        # Keep only genuine external referrers
        ref_host = None
        if request.referrer:
            try:
                from urllib.parse import urlparse
                host = (request.host or '').split(':')[0].lower()
                rh = (urlparse(request.referrer).hostname or '').lower()
                if rh and rh != host:
                    ref_host = rh[:255]
            except Exception:
                ref_host = None

        db.session.add(PageView(
            path=path[:300],
            visitor_id=vid,
            is_authenticated=bool(getattr(current_user, 'is_authenticated', False)),
            referrer_host=ref_host,
            created_at=datetime.utcnow(),
        ))
        db.session.commit()

        if new_vid:
            response.set_cookie(
                'vid', new_vid, max_age=60 * 60 * 24 * 730,
                samesite='Lax', httponly=True, secure=request.is_secure
            )
    except Exception:
        db.session.rollback()
    return response

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
        'total_countries': count_participating_countries(),
        'stakeholders': {}
    }
    
    for stype in get_stakeholder_types():
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
            otp_enabled = app.config.get('ADMIN_OTP_ENABLED', True)
            if os.environ.get('ADMIN_OTP_ENABLED', 'true').lower() != 'true':
                otp_enabled = False
            if not otp_enabled:
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
        # Anti-flood: cap registrations per network so nobody can mass-create
        # accounts/initiatives and overload the server.
        if not rate_ok(f'register:{client_ip()}', 5, 3600):
            flash('Too many sign-up attempts from your network. Please try again later.', 'error')
            return redirect(url_for('register'))

        email = request.form.get('email', '').lower().strip()
        stakeholder_type = request.form.get('stakeholder_type', '').strip()

        if User.query.filter_by(email=email).first():
            flash('Email already registered.', 'error')
            return redirect(url_for('register'))

        # Every registrant submits an initiative (the summary is AI-generated)
        initiative_title = request.form.get('initiative_title', '').strip()
        initiative_content = request.form.get('initiative_content', '').strip()

        if not initiative_title:
            flash('Please provide an initiative title.', 'error')
            return redirect(url_for('register'))
        if not initiative_content:
            flash('Please provide initiative content.', 'error')
            return redirect(url_for('register'))

        # In individual-first mode the organisation field is removed from the form;
        # fall back to a neutral default so the (non-null) column is always satisfied.
        organization = (request.form.get('organization') or '').strip() or 'Independent'
        # Approval is decided below from the initiative's AI quality score.
        user = User(
            email=email,
            name=request.form.get('name'),
            organization=organization,
            stakeholder_type=stakeholder_type,
            country=request.form.get('country'),
            is_approved=False,
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
            short_description=None,   # generated by AI in the background thread below
            user_id=user.id,
            stakeholder_type=user.stakeholder_type,
            country=user.country,
            is_published=False   # decided below from the AI quality score
        )
        db.session.add(initiative)
        db.session.commit()

        # Score the initiative now so we can decide whether to auto-publish it and
        # auto-approve the account. Only a genuine score of 3-5 passes. If scoring
        # is UNAVAILABLE (e.g. expired API key) we must NOT auto-approve — the
        # account and initiative are held for admin review. Scores of 1-2 are held
        # too.
        try:
            score = score_initiative_quality(initiative_title, initiative_content, "")
        except Exception as e:
            app.logger.error(f"Registration quality scoring error: {e}")
            score = None
        record_ai_scoring_result(score is not None)
        initiative.quality_score = score
        approved = (score is not None) and (score >= AUTO_PUBLISH_MIN_SCORE)
        initiative.is_published = approved
        user.is_approved = approved
        db.session.commit()

        if approved:
            # Award points for the published initiative
            award_points(user, 'initiative_published')

            # Send welcome/approval email
            try:
                send_approval_email(user.email, initiative.slug)
            except Exception as e:
                app.logger.error(f"Registration welcome email error: {e}")

            # High-quality initiatives are queued for the member broadcast
            if score is not None and score >= NOTIFY_MIN_SCORE:
                _enqueue_initiative(app, initiative.id)

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
                    if next_event.zoom_webinar_id:
                        try:
                            register_user_for_webinar(next_event.zoom_webinar_id, user)
                        except Exception as e:
                            app.logger.error(f"Auto event Zoom registration error for new user {user.id}: {e}")
                    try:
                        send_event_registration_confirmation(user, next_event)
                    except Exception as e:
                        app.logger.error(f"Auto event confirmation email error for new user {user.id}: {e}")
                    session['new_member_event_title'] = next_event.title
                    session['new_member_event_id'] = next_event.id
        else:
            # Held for admin approval — tell the applicant it's under review
            try:
                send_initiative_pending_email(user, initiative_title)
            except Exception as e:
                app.logger.error(f"Registration pending email error: {e}")

        # Generate the AI summary, vet tags, and detect language in a background
        # thread. This does not affect the publish/approval decision made above.
        def _process_tags_async(flask_app, initiative_id, content, title):
            with flask_app.app_context():
                try:
                    cleaned = clean_title(title)
                    if cleaned and cleaned != title:
                        ini = Initiative.query.get(initiative_id)
                        if ini:
                            ini.title = cleaned
                            db.session.commit()
                except Exception as e:
                    flask_app.logger.error(f"Registration title cleanup error: {e}")
                try:
                    summary = generate_summary(title, content)
                    if summary:
                        ini = Initiative.query.get(initiative_id)
                        if ini:
                            ini.short_description = summary
                            db.session.commit()
                except Exception as e:
                    flask_app.logger.error(f"Registration summary generation error: {e}")
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
            args=(app, initiative.id, initiative_content, initiative_title),
            daemon=True
        )
        t.start()

        if approved:
            flash(
                'Welcome! Your account has been created and you can now log in.',
                'success'
            )
        else:
            flash(
                'Thanks for registering! Your initiative has been submitted for review. '
                "We'll email you once an administrator has approved your account.",
                'info'
            )
        return redirect(url_for('login'))

    return render_template('register.html', stakeholder_types=get_stakeholder_types(), custom_fields=custom_fields)

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
        # Anti-flood: cap how many initiatives one member can post per hour.
        if not rate_ok(f'newinit:{current_user.id}', 10, 3600):
            flash('You are submitting too quickly. Please wait a little before adding more initiatives.', 'warning')
            return redirect(url_for('dashboard'))

        title = request.form.get('title')
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
            short_description=None,   # generated by AI in the background thread below
            user_id=current_user.id,
            stakeholder_type=current_user.stakeholder_type,
            country=current_user.country,
            is_published=False  # Held until AI quality score is confirmed (1-2 stay held for admin)
        )
        
        db.session.add(initiative)
        db.session.commit()

        # Score content quality in background; publish + award points if >= 4,
        # otherwise leave unpublished so it lands in the admin approval queue.
        def _score_async(flask_app, initiative_id, title, content, author_id):
            with flask_app.app_context():
                # Tidy the title's casing/punctuation (no rewording)
                try:
                    cleaned = clean_title(title)
                    if cleaned and cleaned != title:
                        ini = Initiative.query.get(initiative_id)
                        if ini:
                            ini.title = cleaned
                            db.session.commit()
                except Exception as e:
                    flask_app.logger.error(f"Title cleanup error (initiative {initiative_id}): {e}")
                # Generate the AI summary (the submitter no longer supplies one)
                try:
                    summary = generate_summary(title, content)
                    if summary:
                        ini = Initiative.query.get(initiative_id)
                        if ini:
                            ini.short_description = summary
                            db.session.commit()
                except Exception as e:
                    flask_app.logger.error(f"Summary generation error (initiative {initiative_id}): {e}")
                try:
                    from utils.ai_services import score_initiative_quality
                    score = score_initiative_quality(title, content, "")
                    record_ai_scoring_result(score is not None)
                    ini = Initiative.query.get(initiative_id)
                    if ini and score is not None:
                        ini.quality_score = score
                        if score >= AUTO_PUBLISH_MIN_SCORE:
                            # Acceptable quality (3+) — auto-publish and reward the author
                            ini.is_published = True
                            db.session.commit()
                            author = User.query.get(author_id)
                            if author:
                                award_points(author, 'initiative_published')
                            if score >= NOTIFY_MIN_SCORE:
                                _enqueue_initiative(flask_app, initiative_id)
                        else:
                            # Scored 1-2 — leave unpublished for admin approval, notify author
                            db.session.commit()
                            author = User.query.get(author_id)
                            if author:
                                try:
                                    send_initiative_pending_email(author, title)
                                except Exception as mail_err:
                                    flask_app.logger.error(
                                        f"Pending email error (initiative {initiative_id}): {mail_err}"
                                    )
                    else:
                        # Scoring UNAVAILABLE — do NOT publish. Hold for admin review
                        # (this is the safeguard against the auto-approve exploit).
                        if ini:
                            db.session.commit()  # stays unpublished, quality_score None
                        author = User.query.get(author_id)
                        if author:
                            try:
                                send_initiative_pending_email(author, title)
                            except Exception as mail_err:
                                flask_app.logger.error(
                                    f"Pending email error (initiative {initiative_id}): {mail_err}"
                                )
                except Exception as e:
                    flask_app.logger.error(f"Quality scoring error (initiative {initiative_id}): {e}")
                    record_ai_scoring_result(False)
                    # On error, leave the initiative unpublished (held for admin) —
                    # never auto-publish unverified content.
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
            args=(app, initiative.id, title, content, current_user.id),
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
        
        flash('Your initiative has been submitted and is being reviewed. It will be published shortly if it meets our quality standards.', 'success')
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

    already_requested = False
    if current_user.is_authenticated and initiative.user_id != current_user.id:
        already_requested = LearnMoreRequest.query.filter(
            LearnMoreRequest.requester_id == current_user.id,
            LearnMoreRequest.initiative_id == initiative.id,
        ).first() is not None

    return render_template('article.html', initiative=initiative,
                           comments=comments, related=related,
                           already_requested=already_requested)

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


# ===================== LEARN MORE REQUEST =====================

@app.route('/initiative/<slug>/learn-more', methods=['POST'])
@login_required
def learn_more_request(slug):
    """Send a 'Learn More' request email to the initiative publisher.

    Rate-limited to 3 requests per user per calendar month across all initiatives,
    with at most one request per initiative (no time bound).
    Returns JSON so the modal can react without a page reload.
    """
    initiative = Initiative.query.filter_by(slug=slug, is_published=True).first_or_404()

    # Don't let authors request their own initiative
    if initiative.user_id == current_user.id:
        return jsonify(success=False, error='You are the publisher of this initiative.'), 400

    # Enforce rate-limits
    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Check 1: already requested this specific initiative (no time bound)
    already_requested = LearnMoreRequest.query.filter(
        LearnMoreRequest.requester_id == current_user.id,
        LearnMoreRequest.initiative_id == initiative.id,
    ).first()

    if already_requested:
        return jsonify(
            success=False,
            error='You have already sent a Learn More request for this initiative.'
        ), 429

    # Check 2: total requests this month across all initiatives >= 3
    monthly_count = LearnMoreRequest.query.filter(
        LearnMoreRequest.requester_id == current_user.id,
        LearnMoreRequest.created_at >= month_start
    ).count()

    if monthly_count >= 3:
        return jsonify(
            success=False,
            error='You have reached your limit of 3 Learn More requests for this month.'
        ), 429

    # Persist the request
    lmr = LearnMoreRequest(
        requester_id=current_user.id,
        initiative_id=initiative.id,
    )
    db.session.add(lmr)
    db.session.commit()

    # Build and send the notification email to the publisher
    publisher = initiative.author
    requester = current_user

    initiative_url = url_for('view_initiative', slug=initiative.slug, _external=True)
    initiative_title = initiative.title

    site_for_email = get_setting('site_name', 'AU ECED-FLN')
    subject = f"[{site_for_email}] Learn More Request: {initiative_title}"
    body = f"""Dear {publisher.name},

A cluster member has expressed interest in learning more about your initiative and would like to connect with you.

Initiative: {initiative_title}
{initiative_url}

--- Contact Details ---
Name:         {requester.name}
Organisation: {requester.organization}
Position:     {getattr(requester, 'position', '') or '(not specified)'}
Email:        {requester.email}
Country:      {requester.country}

Please feel free to reach out to them directly to continue the conversation.

Best regards,
{site_for_email} Secretariat
"""

    try:
        send_member_notification(
            to_email=publisher.email,
            subject=subject,
            body=body,
        )
    except Exception as e:
        app.logger.error(f"Learn-more email error (initiative {initiative.id}): {e}")
        # Don't roll back — the request is logged; the email failure is non-critical.

    return jsonify(success=True)


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
        
        # Handle tags — either regenerate from AI or use manual list
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
        else:
            # Use manual tags from the hidden input
            tag_names = [t.strip().lower() for t in request.form.get('tags', '').split(',') if t.strip()]
            old_tags = set(initiative.tags)
            new_tags_set = set()
            for name in tag_names:
                tag = Tag.query.filter_by(name=name).first()
                if not tag:
                    tag = Tag(name=name, is_vetted=False)
                    db.session.add(tag)
                    db.session.flush()
                new_tags_set.add(tag)
            # Decrement usage_count for removed tags
            for tag in old_tags:
                if tag not in new_tags_set and tag.usage_count > 0:
                    tag.usage_count -= 1
            # Increment usage_count for added tags
            for tag in new_tags_set:
                if tag not in old_tags:
                    tag.usage_count += 1
            initiative.tags = list(new_tags_set)
        
        db.session.commit()
        flash('Initiative updated.', 'success')
        return redirect(url_for('view_initiative', slug=initiative.slug))
    
    return render_template('article_form.html', initiative=initiative)

@app.route('/search')
def search():
    tag_name = request.args.get('tag', '')
    org_name = request.args.get('q', '').strip()  # used by "View Initiatives" on members page
    initiatives = Initiative.query.filter_by(is_published=True)

    if tag_name:
        tag = Tag.query.filter_by(name=tag_name).first()
        if tag:
            initiatives = initiatives.filter(Initiative.tags.contains(tag))

    if org_name:
        # Filter to initiatives whose author belongs to the given organisation
        initiatives = initiatives.join(User, User.id == Initiative.user_id).filter(
            User.organization == org_name
        )

    initiatives = initiatives.order_by(Initiative.created_at.desc()).all()
    tags = Tag.query.order_by(Tag.name).all()  # for dropdown

    return render_template('search.html', initiatives=initiatives, tags=tags,
                           selected_tag=tag_name, org_filter=org_name)

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


@app.route('/certificate/<token>')
def certificate(token):
    """Public, printable contributor certificate page."""
    cert = Certificate.query.filter_by(token=token).first_or_404()
    member = cert.user
    initiatives = (Initiative.query.filter_by(user_id=member.id, is_published=True)
                   .order_by(Initiative.created_at.desc()).all())
    return render_template('certificate.html', cert=cert, member=member, initiatives=initiatives)

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


# ===================== STATS / PARTICIPATION =====================

# Approximate (lat, lng) centroids for the African countries offered in the
# registration dropdown — used to plot the participation bubble map.
AFRICA_CENTROIDS = {
    'Algeria': (28.0, 1.6), 'Angola': (-11.2, 17.9), 'Benin': (9.3, 2.3),
    'Botswana': (-22.3, 24.7), 'Burkina Faso': (12.2, -1.6), 'Burundi': (-3.4, 29.9),
    'Cabo Verde': (16.0, -24.0), 'Cameroon': (7.4, 12.4),
    'Central African Republic': (6.6, 20.9), 'Chad': (15.4, 18.7),
    'Comoros': (-11.6, 43.3), 'Congo (Brazzaville)': (-0.2, 15.8),
    'Congo (DRC)': (-4.0, 21.8), 'Djibouti': (11.8, 42.6), 'Egypt': (26.8, 30.8),
    'Equatorial Guinea': (1.6, 10.3), 'Eritrea': (15.2, 39.8), 'Eswatini': (-26.5, 31.5),
    'Ethiopia': (9.1, 40.5), 'Gabon': (-0.8, 11.6), 'Gambia': (13.4, -15.3),
    'Ghana': (7.9, -1.0), 'Guinea': (9.9, -9.7), 'Guinea-Bissau': (11.8, -15.2),
    'Ivory Coast': (7.5, -5.5), 'Kenya': (0.2, 37.9), 'Lesotho': (-29.6, 28.2),
    'Liberia': (6.4, -9.4), 'Libya': (26.3, 17.2), 'Madagascar': (-18.8, 46.9),
    'Malawi': (-13.3, 34.3), 'Mali': (17.6, -4.0), 'Mauritania': (21.0, -10.9),
    'Mauritius': (-20.3, 57.6), 'Morocco': (31.8, -7.1), 'Mozambique': (-18.7, 35.5),
    'Namibia': (-22.6, 18.5), 'Niger': (17.6, 8.1), 'Nigeria': (9.1, 8.7),
    'Rwanda': (-1.9, 29.9), 'São Tomé and Príncipe': (0.2, 6.6), 'Senegal': (14.5, -14.5),
    'Seychelles': (-4.7, 55.5), 'Sierra Leone': (8.5, -11.8), 'Somalia': (5.2, 46.2),
    'South Africa': (-30.6, 22.9), 'South Sudan': (7.9, 29.7), 'Sudan': (12.9, 30.2),
    'Tanzania': (-6.4, 34.9), 'Togo': (8.6, 0.8), 'Tunisia': (33.9, 9.5),
    'Uganda': (1.4, 32.3), 'Zambia': (-13.1, 27.8), 'Zimbabwe': (-19.0, 29.2),
}

# Common name variants from older/imported data → canonical dropdown name.
_COUNTRY_ALIASES = {
    "cote d'ivoire": 'Ivory Coast', "côte d'ivoire": 'Ivory Coast',
    'drc': 'Congo (DRC)', 'dr congo': 'Congo (DRC)',
    'democratic republic of the congo': 'Congo (DRC)',
    'republic of the congo': 'Congo (Brazzaville)', 'congo': 'Congo (Brazzaville)',
    'congo-brazzaville': 'Congo (Brazzaville)',
    'cape verde': 'Cabo Verde', 'swaziland': 'Eswatini',
    'sao tome and principe': 'São Tomé and Príncipe',
    'the gambia': 'Gambia', 'tanzania, united republic of': 'Tanzania',
}
_CENTROID_LOWER = {k.lower(): k for k in AFRICA_CENTROIDS}


def _normalize_country(raw):
    """Map a free-text country value to a canonical dropdown name, or None."""
    if not raw:
        return None
    key = raw.strip().lower()
    if key in _CENTROID_LOWER:
        return _CENTROID_LOWER[key]
    return _COUNTRY_ALIASES.get(key)


def count_participating_countries():
    """Number of distinct African countries represented by approved members."""
    rows = db.session.query(User.country).filter(User.is_approved == True).all()
    return len({_normalize_country(c) for (c,) in rows if _normalize_country(c)})


@app.route('/stats')
def stats():
    from sqlalchemy import func, distinct

    # ---- Headline counts -------------------------------------------------
    total_members = User.query.filter_by(is_approved=True).count()
    org_count = db.session.query(
        func.count(distinct(func.lower(func.trim(User.organization))))
    ).filter(
        User.is_approved == True, User.organization != None,
        func.trim(User.organization) != ''
    ).scalar() or 0
    headline = {
        'members': total_members,
        'organizations': org_count,
        'initiatives': Initiative.query.filter_by(is_published=True).count(),
    }

    # ---- Participation by country ---------------------------------------
    country_rows = db.session.query(
        User.country, func.count(User.id)
    ).filter(User.is_approved == True).group_by(User.country).all()

    by_country = {}
    other_count = 0
    for raw, count in country_rows:
        canon = _normalize_country(raw)
        if canon:
            by_country[canon] = by_country.get(canon, 0) + count
        else:
            other_count += count

    map_points = [
        {'country': c, 'count': n, 'lat': AFRICA_CENTROIDS[c][0], 'lng': AFRICA_CENTROIDS[c][1]}
        for c, n in by_country.items()
    ]
    map_points.sort(key=lambda p: p['count'], reverse=True)
    country_table = list(map_points)  # same data, used for the summary table
    headline['countries'] = len(by_country)

    # ---- Stakeholder breakdown ------------------------------------------
    stake_rows = db.session.query(
        User.stakeholder_type, func.count(User.id)
    ).filter(User.is_approved == True).group_by(User.stakeholder_type).all()
    stakeholders = sorted(
        [{'type': (t or 'Unspecified'), 'count': n} for t, n in stake_rows],
        key=lambda r: r['count'], reverse=True
    )

    # ---- Member growth over time (cumulative, by month) -----------------
    dates = [d for (d,) in db.session.query(User.created_at)
             .filter(User.is_approved == True, User.created_at != None).all()]
    monthly = {}
    for d in dates:
        monthly[d.strftime('%Y-%m')] = monthly.get(d.strftime('%Y-%m'), 0) + 1
    growth_labels, growth_values, running = [], [], 0
    for ym in sorted(monthly):
        running += monthly[ym]
        growth_labels.append(ym)
        growth_values.append(running)

    return render_template(
        'stats.html',
        headline=headline,
        map_points=map_points,
        country_table=country_table,
        other_count=other_count,
        stakeholders=stakeholders,
        growth_labels=growth_labels, growth_values=growth_values,
    )


@app.route('/admin/analytics')
@login_required
def admin_analytics():
    """Admin-only site-usage analytics: page views, unique visitors, daily trend,
    top pages, and top referrers."""
    if not current_user.is_admin:
        abort(403)
    from sqlalchemy import func, distinct

    now = datetime.utcnow()
    since30 = now - timedelta(days=30)
    analytics = {
        'total_views': db.session.query(func.count(PageView.id)).scalar() or 0,
        'total_visitors': db.session.query(
            func.count(distinct(PageView.visitor_id))).scalar() or 0,
        'views_30d': db.session.query(func.count(PageView.id))
            .filter(PageView.created_at >= since30).scalar() or 0,
        'visitors_30d': db.session.query(func.count(distinct(PageView.visitor_id)))
            .filter(PageView.created_at >= since30).scalar() or 0,
    }

    # Daily views for the last 30 days (zero-filled)
    daily_rows = db.session.query(
        func.date(PageView.created_at), func.count(PageView.id)
    ).filter(PageView.created_at >= since30).group_by(func.date(PageView.created_at)).all()
    daily_map = {str(d): n for d, n in daily_rows}
    daily_labels, daily_values = [], []
    for i in range(29, -1, -1):
        day = (now - timedelta(days=i)).strftime('%Y-%m-%d')
        daily_labels.append(day)
        daily_values.append(daily_map.get(day, 0))

    top_pages = [
        {'path': p, 'views': n} for p, n in db.session.query(
            PageView.path, func.count(PageView.id)
        ).filter(PageView.created_at >= since30)
         .group_by(PageView.path).order_by(func.count(PageView.id).desc()).limit(10).all()
    ]
    top_referrers = [
        {'host': h, 'views': n} for h, n in db.session.query(
            PageView.referrer_host, func.count(PageView.id)
        ).filter(PageView.referrer_host != None)
         .group_by(PageView.referrer_host).order_by(func.count(PageView.id).desc()).limit(8).all()
    ]

    # "Request to Learn More" stats
    learn_more = {
        'total': db.session.query(func.count(LearnMoreRequest.id)).scalar() or 0,
        'last_30d': db.session.query(func.count(LearnMoreRequest.id))
            .filter(LearnMoreRequest.created_at >= since30).scalar() or 0,
        'senders': db.session.query(
            func.count(distinct(LearnMoreRequest.requester_id))).scalar() or 0,
    }
    top_learn_more_articles = [
        {'title': title, 'slug': slug, 'count': n}
        for title, slug, n in db.session.query(
            Initiative.title, Initiative.slug, func.count(LearnMoreRequest.id)
        ).join(LearnMoreRequest, LearnMoreRequest.initiative_id == Initiative.id)
         .group_by(Initiative.id, Initiative.title, Initiative.slug)
         .order_by(func.count(LearnMoreRequest.id).desc()).limit(10).all()
    ]
    top_learn_more_senders = [
        {'name': name, 'organization': org, 'count': n}
        for name, org, n in db.session.query(
            User.name, User.organization, func.count(LearnMoreRequest.id)
        ).join(LearnMoreRequest, LearnMoreRequest.requester_id == User.id)
         .group_by(User.id, User.name, User.organization)
         .order_by(func.count(LearnMoreRequest.id).desc()).limit(10).all()
    ]

    return render_template(
        'admin/analytics.html',
        analytics=analytics,
        daily_labels=daily_labels, daily_values=daily_values,
        top_pages=top_pages, top_referrers=top_referrers,
        learn_more=learn_more,
        top_learn_more_articles=top_learn_more_articles,
        top_learn_more_senders=top_learn_more_senders,
    )

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

import base64

@app.route('/uploads/hero-image')
def custom_hero_image():
    data = get_setting('hero_image_data')
    mimetype = get_setting('hero_image_mimetype') or 'image/png'
    if not data:
        return redirect(url_for('static', filename=DEFAULT_HERO_IMAGE))
    return Response(base64.b64decode(data), mimetype=mimetype)

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
    pending_documents = DocumentLibrary.query.filter_by(is_published=False).count()
    undetected_lang_count = Initiative.query.filter(Initiative.detected_lang.is_(None)).count()

    def _backfill_status(flag_key, status_key):
        status = get_setting(status_key)
        if status:
            return status
        return 'complete' if get_setting(flag_key, 'false') == 'true' else 'pending'
    ai_backfills = [
        {'label': 'Initiative summaries', 'status': _backfill_status('summaries_backfilled', 'summaries_backfill_status')},
        {'label': 'Initiative titles', 'status': _backfill_status('titles_backfilled', 'titles_backfill_status')},
    ]

    return render_template('admin/dashboard.html',
                         ai_backfills=ai_backfills,
                         pending_users=pending_users,
                         pending_initiatives=pending_initiatives,
                         pending_questions=pending_questions,
                         pending_projects=pending_projects,
                         pending_events=pending_events,
                         pending_comments=pending_comments,
                         pending_policy=pending_policy,
                         queue_count=queue_count,
                         policy_queue_count=policy_queue_count,
                         pending_documents=pending_documents,
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
    elif type_filter == 'documents':
        items = DocumentLibrary.query.filter_by(
            is_published=False
        ).order_by(DocumentLibrary.created_at.desc()).all()
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
        document_items = DocumentLibrary.query.filter_by(
            is_published=False
        ).order_by(DocumentLibrary.created_at.desc()).all()
        items = list(users) + list(initiatives) + list(questions) + list(projects) + list(events) + list(comments) + list(policy_items) + list(document_items)
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

@app.route('/admin/send-queue/toggle-test-mode', methods=['POST'])
@login_required
def toggle_send_queue_test_mode():
    """Toggle test mode for the send queue on or off."""
    if not current_user.is_admin:
        abort(403)
    current = get_setting('send_queue_test_mode', 'false')
    new_value = 'false' if current == 'true' else 'true'
    set_setting('send_queue_test_mode', new_value)
    if new_value == 'true':
        flash('Test mode ON — sends will go only to the admin OTP email and items will stay in the queue.', 'warning')
    else:
        flash('Test mode OFF — sends will go to all subscribed members.', 'success')
    return redirect(url_for('admin_send_queue'))


@app.route('/admin/send-queue')
@login_required
def admin_send_queue():
    """View of all approved initiatives, policy developments, documents, and TA needs waiting to be sent."""
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

    # Documents
    document_unsent = (DocumentSendQueue.query
              .filter_by(sent_at=None)
              .order_by(DocumentSendQueue.queued_at.desc())
              .all())
    document_sent = (DocumentSendQueue.query
            .filter(DocumentSendQueue.sent_at.isnot(None))
            .order_by(DocumentSendQueue.sent_at.desc())
            .limit(20).all())

    # Build unified sorted lists
    def _wrap(entries, kind):
        return [{'type': kind, 'entry': e, 'queued_at': e.queued_at} for e in entries]
    def _wrap_sent(entries, kind):
        return [{'type': kind, 'entry': e, 'sent_at': e.sent_at} for e in entries]

    queue_unsent = sorted(
        _wrap(initiative_unsent, 'initiative') +
        _wrap(policy_unsent, 'policy') +
        _wrap(document_unsent, 'document'),
        key=lambda x: x['queued_at'],
        reverse=True
    )
    queue_sent = sorted(
        _wrap_sent(initiative_sent, 'initiative') +
        _wrap_sent(policy_sent, 'policy') +
        _wrap_sent(document_sent, 'document'),
        key=lambda x: x['sent_at'],
        reverse=True
    )[:20]

    test_mode = get_setting('send_queue_test_mode', 'false') == 'true'
    test_email = app.config.get('ADMIN_OTP_EMAIL') or ''

    return render_template('admin/send_queue.html',
                         queue_unsent=queue_unsent,
                         queue_sent=queue_sent,
                         initiative_unsent=initiative_unsent,
                         policy_unsent=policy_unsent,
                         document_unsent=document_unsent,
                         test_mode=test_mode,
                         test_email=test_email)


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
    test_mode = get_setting('send_queue_test_mode', 'false') == 'true'
    initiative_url = url_for('view_initiative', slug=initiative.slug, _external=True)
    ini_data = {
        'title': initiative.title,
        'short_description': initiative.short_description or '',
        'url': initiative_url,
    }
    test_email = app.config.get('ADMIN_OTP_EMAIL') or current_user.email
 
    def _do_send(flask_app, _queue_id, _ini_data, _is_test, _test_email):
        with flask_app.app_context():
            try:
                if _is_test:
                    class _FakeUser:
                        def __init__(self, e): self.email = e
                    send_single_initiative_notification(_ini_data, [_FakeUser(_test_email)])
                else:
                    subscribed_users = User.query.filter_by(
                        is_approved=True, is_subscribed=True
                    ).all()
                    send_single_initiative_notification(_ini_data, subscribed_users)
                    _entry = InitiativeSendQueue.query.get(_queue_id)
                    if _entry:
                        _entry.sent_at = datetime.utcnow()
                        db.session.commit()
            except Exception as e:
                flask_app.logger.error(f"Background send_queue_item error (id={_queue_id}): {e}")
 
    threading.Thread(
        target=_do_send,
        args=(app, queue_id, ini_data, test_mode, test_email),
        daemon=True,
    ).start()
 
    if test_mode:
        flash(
            f'[TEST] "{initiative.title}" is being sent to {test_email} in the background. '
            f'Item stays in queue.',
            'warning',
        )
    else:
        subscribed_count = User.query.filter_by(
            is_approved=True, is_subscribed=True
        ).count()
        flash(
            f'"{initiative.title}" is being sent to {subscribed_count} member(s) in the background.',
            'success',
        )
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
    test_mode = get_setting('send_queue_test_mode', 'false') == 'true'
    try:
        if test_mode:
            test_email = app.config.get('ADMIN_OTP_EMAIL') or current_user.email
            class _FakeUser:
                def __init__(self, email): self.email = email
            send_bulk_initiatives_digest(initiatives_data, [_FakeUser(test_email)])
            flash(f'[TEST] {len(unsent)} initiative(s) sent as digest to {test_email} only. Items stay in queue.', 'warning')
        else:
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
        return redirect(url_for('admin_send_queue'))
 
    policy = entry.policy
    test_mode = get_setting('send_queue_test_mode', 'false') == 'true'
    policy_url = url_for('view_policy', id=policy.id, _external=True)
    policy_data = {
        'title': policy.title or policy.source_url[:100],
        'short_summary': policy.short_summary or '',
        'url': policy_url,
        'country': policy.country or '',
        'published_date': (
            policy.published_date.strftime('%B %d, %Y') if policy.published_date else ''
        ),
    }
    test_email = app.config.get('ADMIN_OTP_EMAIL') or current_user.email
 
    def _do_send(flask_app, _queue_id, _policy_data, _is_test, _test_email):
        with flask_app.app_context():
            try:
                if _is_test:
                    class _FakeUser:
                        def __init__(self, e): self.email = e
                    send_single_policy_notification(_policy_data, [_FakeUser(_test_email)])
                else:
                    subscribed_users = User.query.filter_by(
                        is_approved=True, is_subscribed=True
                    ).all()
                    send_single_policy_notification(_policy_data, subscribed_users)
                    _entry = PolicySendQueue.query.get(_queue_id)
                    if _entry:
                        _entry.sent_at = datetime.utcnow()
                        db.session.commit()
            except Exception as e:
                flask_app.logger.error(
                    f"Background send_policy_queue_item error (id={_queue_id}): {e}"
                )
 
    threading.Thread(
        target=_do_send,
        args=(app, queue_id, policy_data, test_mode, test_email),
        daemon=True,
    ).start()
 
    if test_mode:
        flash(
            f'[TEST] "{policy_data["title"]}" is being sent to {test_email} in the background. '
            f'Item stays in queue.',
            'warning',
        )
    else:
        subscribed_count = User.query.filter_by(
            is_approved=True, is_subscribed=True
        ).count()
        flash(
            f'"{policy_data["title"]}" is being sent to {subscribed_count} member(s) in the background.',
            'success',
        )
    return redirect(url_for('admin_send_queue'))



@app.route('/admin/policy-send-queue/send-all', methods=['POST'])
@login_required
def send_policy_queue_all():
    """Send ALL unsent policy developments in the queue as a single digest email."""
    if not current_user.is_admin:
        abort(403)
    unsent = PolicySendQueue.query.filter_by(sent_at=None).all()
    if not unsent:
        flash('No unsent policy developments in the queue.', 'info')
        return redirect(url_for('admin_send_queue'))
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
    test_mode = get_setting('send_queue_test_mode', 'false') == 'true'
    try:
        if test_mode:
            test_email = app.config.get('ADMIN_OTP_EMAIL') or current_user.email
            class _FakeUser:
                def __init__(self, email): self.email = email
            send_bulk_policies_digest(policies_data, [_FakeUser(test_email)])
            flash(f'[TEST] {len(unsent)} policy development(s) sent as digest to {test_email} only. Items stay in queue.', 'warning')
        else:
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
    return redirect(url_for('admin_send_queue'))


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
    return redirect(url_for('admin_send_queue'))


# ===================== DOCUMENT SEND QUEUE ROUTES =====================

@app.route('/admin/document-send-queue/send/<int:queue_id>', methods=['POST'])
@login_required
def send_document_queue_item(queue_id):
    """Send a single document from the queue to all subscribed members."""
    if not current_user.is_admin:
        abort(403)
    entry = DocumentSendQueue.query.get_or_404(queue_id)
    if entry.sent_at:
        flash('This document has already been sent.', 'warning')
        return redirect(url_for('admin_send_queue'))
 
    doc = entry.document
    test_mode = get_setting('send_queue_test_mode', 'false') == 'true'
    doc_url = url_for('view_document', id=doc.id, _external=True)
    doc_data = {
        'title': doc.title or doc.filename,
        'description': doc.description or '',
        'url': doc_url,
        'year_published': str(doc.year_published) if doc.year_published else '',
        'file_type': doc.file_type or '',
    }
    test_email = app.config.get('ADMIN_OTP_EMAIL') or current_user.email
 
    def _do_send(flask_app, _queue_id, _doc_data, _is_test, _test_email):
        with flask_app.app_context():
            try:
                if _is_test:
                    class _FakeUser:
                        def __init__(self, e): self.email = e
                    send_single_document_notification(_doc_data, [_FakeUser(_test_email)])
                else:
                    subscribed_users = User.query.filter_by(
                        is_approved=True, is_subscribed=True
                    ).all()
                    send_single_document_notification(_doc_data, subscribed_users)
                    _entry = DocumentSendQueue.query.get(_queue_id)
                    if _entry:
                        _entry.sent_at = datetime.utcnow()
                        db.session.commit()
            except Exception as e:
                flask_app.logger.error(
                    f"Background send_document_queue_item error (id={_queue_id}): {e}"
                )
 
    threading.Thread(
        target=_do_send,
        args=(app, queue_id, doc_data, test_mode, test_email),
        daemon=True,
    ).start()
 
    if test_mode:
        flash(
            f'[TEST] "{doc_data["title"]}" is being sent to {test_email} in the background. '
            f'Item stays in queue.',
            'warning',
        )
    else:
        subscribed_count = User.query.filter_by(
            is_approved=True, is_subscribed=True
        ).count()
        flash(
            f'"{doc_data["title"]}" is being sent to {subscribed_count} member(s) in the background.',
            'success',
        )
    return redirect(url_for('admin_send_queue'))


@app.route('/admin/document-send-queue/send-all', methods=['POST'])
@login_required
def send_document_queue_all():
    """Send ALL unsent documents in the queue as a single digest email."""
    if not current_user.is_admin:
        abort(403)
    unsent = DocumentSendQueue.query.filter_by(sent_at=None).all()
    if not unsent:
        flash('No unsent documents in the queue.', 'info')
        return redirect(url_for('admin_send_queue'))
    docs_data = []
    for entry in unsent:
        doc = entry.document
        doc_url = url_for('view_document', id=doc.id, _external=True)
        docs_data.append({
            'title': doc.title or doc.filename,
            'description': doc.description or '',
            'url': doc_url,
            'year_published': str(doc.year_published) if doc.year_published else '',
            'file_type': doc.file_type or '',
        })
    test_mode = get_setting('send_queue_test_mode', 'false') == 'true'
    try:
        if test_mode:
            test_email = app.config.get('ADMIN_OTP_EMAIL') or current_user.email
            class _FakeUser:
                def __init__(self, email): self.email = email
            send_bulk_documents_digest(docs_data, [_FakeUser(test_email)])
            flash(f'[TEST] {len(unsent)} document(s) sent as digest to {test_email} only. Items stay in queue.', 'warning')
        else:
            subscribed_users = User.query.filter_by(is_approved=True, is_subscribed=True).all()
            send_bulk_documents_digest(docs_data, subscribed_users)
            now = datetime.utcnow()
            for entry in unsent:
                entry.sent_at = now
            db.session.commit()
            flash(
                f'{len(unsent)} document(s) sent to {len(subscribed_users)} member(s) as a digest.',
                'success'
            )
    except Exception as e:
        app.logger.error(f"Send all documents queue error: {e}")
        flash('Failed to send digest. Check logs.', 'error')
    return redirect(url_for('admin_send_queue'))


@app.route('/admin/document-send-queue/remove/<int:queue_id>', methods=['POST'])
@login_required
def remove_document_queue_item(queue_id):
    """Remove a document from the send queue without sending."""
    if not current_user.is_admin:
        abort(403)
    entry = DocumentSendQueue.query.get_or_404(queue_id)
    db.session.delete(entry)
    db.session.commit()
    flash('Document removed from send queue.', 'success')
    return redirect(url_for('admin_send_queue'))

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
            email = request.form.get('mail_username')
            name = request.form.get('mail_sender_name', '').strip() or 'Africa Teachers Forum'
            sender = f'{name} <{email}>'
            os.environ['MAIL_USERNAME'] = email
            app.config['MAIL_USERNAME'] = email
            app.config['MAIL_DEFAULT_SENDER'] = sender
            os.environ['MAIL_DEFAULT_SENDER'] = sender
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

@app.route('/admin/appearance', methods=['GET', 'POST'])
@login_required
def admin_appearance():
    """Front-end overrides: site name/tagline, menu show/hide/rename, certificates toggle."""
    if not current_user.is_admin:
        abort(403)
    if request.method == 'POST':
        site_name = (request.form.get('site_name') or '').strip() or DEFAULT_SITE_NAME
        set_setting('site_name', site_name)
        # Keep an env copy so context-free email sends pick up the override too.
        os.environ['SITE_NAME'] = site_name
        site_tagline_val = (request.form.get('site_tagline') or '').strip() or DEFAULT_SITE_TAGLINE
        set_setting('site_tagline', site_tagline_val)
        os.environ['SITE_TAGLINE'] = site_tagline_val
        set_setting('footer_note', (request.form.get('footer_note') or '').strip())
        set_setting('certificates_enabled', 'true' if request.form.get('certificates_enabled') else 'false')

        # Front-page header text
        set_setting('hero_heading', (request.form.get('hero_heading') or '').strip())
        set_setting('hero_text', (request.form.get('hero_text') or '').strip())

        # Front-page header image: reset, or upload a replacement
        if request.form.get('reset_hero_image'):
            set_setting('hero_image_data', '')
            set_setting('hero_image_mimetype', '')
        else:
            file = request.files.get('hero_image')
            if file and file.filename:
                ext = os.path.splitext(file.filename)[1].lower().lstrip('.')
                if ext in {'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'}:
                    mimetype_map = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
                                    'gif': 'image/gif', 'webp': 'image/webp', 'svg': 'image/svg+xml'}
                    data = base64.b64encode(file.read()).decode('ascii')
                    set_setting('hero_image_data', data)
                    set_setting('hero_image_mimetype', mimetype_map.get(ext, 'image/png'))
                else:
                    flash('Header image must be PNG, JPG, GIF, WEBP, or SVG.', 'error')
                    return redirect(url_for('admin_appearance'))

        overrides = {}
        for item in NAV_ITEMS:
            key = item['key']
            entry = {}
            if request.form.get(f'show_{key}') is None:   # checkbox absent => hidden
                entry['hidden'] = True
            label = (request.form.get(f'label_{key}') or '').strip()
            if label and label != item['label']:
                entry['label'] = label
            if entry:
                overrides[key] = entry
        set_setting('menu_overrides', json.dumps(overrides))
        flash('Appearance settings updated.', 'success')
        return redirect(url_for('admin_appearance'))

    overrides = get_menu_overrides()
    nav_config = [{
        'key': item['key'],
        'default_label': item['label'],
        'label': overrides.get(item['key'], {}).get('label', ''),
        'shown': not overrides.get(item['key'], {}).get('hidden', False),
        'external': item.get('external', False),
    } for item in NAV_ITEMS]
    hero_image_data = get_setting('hero_image_data')
    return render_template(
        'admin/appearance.html',
        site_name=get_setting('site_name', DEFAULT_SITE_NAME),
        site_tagline=get_setting('site_tagline', DEFAULT_SITE_TAGLINE),
        footer_note=get_setting('footer_note', ''),
        hero_heading=get_setting('hero_heading', ''),
        hero_text=get_setting('hero_text', ''),
        hero_image=bool(hero_image_data),
        hero_image_url=url_for('custom_hero_image') if hero_image_data else '',
        default_hero_image=DEFAULT_HERO_IMAGE,
        default_hero_heading=DEFAULT_HERO_HEADING,
        default_hero_text=DEFAULT_HERO_TEXT,
        certificates_enabled=is_certificates_enabled(),
        nav_config=nav_config,
    )


@app.route('/admin/stakeholder-types', methods=['GET', 'POST'])
@login_required
def admin_stakeholder_types():
    """CRUD for stakeholder type categories — overrides the hardcoded defaults."""
    if not current_user.is_admin:
        abort(403)

    if request.method == 'POST':
        action = request.form.get('action', '')

        if action == 'add':
            name = request.form.get('name', '').strip()
            if name:
                existing = StakeholderType.query.filter_by(name=name).first()
                if existing:
                    flash(f'Stakeholder type "{name}" already exists.', 'error')
                else:
                    db.session.add(StakeholderType(
                        name=name,
                        is_member_state=bool(request.form.get('is_member_state')),
                        order=StakeholderType.query.count() + 1,
                    ))
                    db.session.commit()
                    flash(f'Stakeholder type "{name}" added.', 'success')
            else:
                flash('Name is required.', 'error')

        elif action == 'edit':
            st_id = request.form.get('id', type=int)
            st = StakeholderType.query.get_or_404(st_id)
            new_name = request.form.get('name', '').strip()
            if new_name and new_name != st.name:
                existing = StakeholderType.query.filter_by(name=new_name).first()
                if existing:
                    flash(f'Stakeholder type "{new_name}" already exists.', 'error')
                else:
                    old_name = st.name
                    st.name = new_name
                    flash(f'Renamed "{old_name}" to "{new_name}".', 'success')
            st.is_member_state = bool(request.form.get('is_member_state'))
            st.is_active = bool(request.form.get('is_active'))
            db.session.commit()
            flash('Stakeholder type updated.', 'success')

        elif action == 'delete':
            st_id = request.form.get('id', type=int)
            st = StakeholderType.query.get_or_404(st_id)
            # Prevent deleting if users are assigned
            users_count = User.query.filter_by(stakeholder_type=st.name).count()
            if users_count > 0:
                flash(f'Cannot delete "{st.name}": {users_count} member(s) use this type.', 'error')
            else:
                db.session.delete(st)
                db.session.commit()
                flash(f'Stakeholder type "{st.name}" deleted.', 'success')

        elif action == 'reorder':
            order_ids = request.form.getlist('order[]')
            for idx, sid in enumerate(order_ids, start=1):
                st = StakeholderType.query.get(int(sid))
                if st:
                    st.order = idx
            db.session.commit()
            flash('Order updated.', 'success')

        return redirect(url_for('admin_stakeholder_types'))

    types = StakeholderType.query.order_by(StakeholderType.order).all()
    return render_template('admin/stakeholder_types.html', types=types,
                         defaults=DEFAULT_STAKEHOLDER_TYPES)


@app.route('/admin/labels', methods=['GET', 'POST'])
@login_required
def admin_labels():
    """Manage all front-end label overrides."""
    if not current_user.is_admin:
        abort(403)

    if request.method == 'POST':
        action = request.form.get('action', '')

        if action == 'update':
            for key in LABEL_DEFAULTS:
                val = request.form.get(f'label_{key}', '').strip()
                existing = Label.query.filter_by(key=key).first()
                if val:
                    if existing:
                        existing.value = val
                    else:
                        db.session.add(Label(key=key, value=val))
                else:
                    if existing:
                        db.session.delete(existing)
            db.session.commit()
            flash('Labels updated.', 'success')

        return redirect(url_for('admin_labels'))

    all_labels = []
    db_labels = {l.key: l for l in Label.query.all()}
    for key, default in LABEL_DEFAULTS.items():
        override = db_labels.get(key)
        all_labels.append({
            'key': key,
            'default': default,
            'value': override.value if override else '',
        })

    return render_template('admin/labels.html', labels=all_labels)

PAGE_TITLES = [
    {'key': 'dashboard', 'default': 'Dashboard'},
    {'key': 'login', 'default': 'Login'},
    {'key': 'register', 'default': 'Join Cluster'},
    {'key': 'explore', 'default': 'Explore Initiatives'},
    {'key': 'members', 'default': 'Participating Organisations'},
    {'key': 'search_members', 'default': 'Search Stakeholders'},
    {'key': 'events', 'default': 'Events'},
    {'key': 'polls', 'default': 'Polls'},
    {'key': 'forum', 'default': 'Q&A Forum'},
    {'key': 'documents', 'default': 'ECED Policy Documents'},
    {'key': 'document_upload', 'default': 'Upload Document'},
    {'key': 'projects', 'default': 'Projects'},
    {'key': 'policy', 'default': 'ECED Policy Developments'},
    {'key': 'verify_otp', 'default': 'Verify OTP'},
    {'key': 'profile_edit', 'default': 'Edit Profile'},
    {'key': 'unsubscribe', 'default': 'Unsubscribe'},
    {'key': 'stats', 'default': 'Participation'},
    {'key': 'leaderboard', 'default': 'Leaderboard'},
    {'key': 'discussions', 'default': 'Discussions'},
    {'key': 'initiative_form', 'default': 'New Initiative'},
    {'key': 'event_form', 'default': 'Submit an Event'},
    {'key': 'project_form', 'default': 'Submit a Project'},
    {'key': 'question_form', 'default': 'New Question'},
    {'key': 'ta_form', 'default': 'Submit TA Need'},
    {'key': 'suffix', 'default': 'AU ECED-FLN Cluster', 'note': 'Appended after dynamic content (initiative title, etc.)'},
]


BUILTIN_FORM_FIELDS = [
    {'field_name': 'name',             'label_key': 'form_full_name',          'field_type': 'text',     'is_required': True},
    {'field_name': 'email',            'label_key': 'form_email',              'field_type': 'email',    'is_required': True},
    {'field_name': 'organization',     'label_key': 'form_organization',       'field_type': 'text',     'is_required': True},
    {'field_name': 'country',          'label_key': 'form_country',            'field_type': 'select',   'is_required': True},
    {'field_name': 'stakeholder_type', 'label_key': 'form_stakeholder_type',   'field_type': 'select',   'is_required': True},
]


@app.route('/admin/forms', methods=['GET', 'POST'])
@login_required
def admin_forms():
    """Unified per-form editor: edit every user-visible string (and manage the
    registration custom fields) in one place. Replaces the old Form Fields page."""
    if not current_user.is_admin:
        abort(403)

    if request.method == 'POST':
        # Scoped save: only upsert the label keys that belong to the submitted
        # form panel — never touch keys from other forms. Blank value clears the
        # override (falls back to the built-in default).
        form_key = request.form.get('form_key')
        definition = next((f for f in FORM_DEFINITIONS if f['key'] == form_key), None)
        if not definition:
            abort(400)
        submitted_keys = [fld[0] for grp in definition['groups'] for fld in grp['fields']]
        for key in submitted_keys:
            val = (request.form.get(f'label_{key}') or '').strip()
            existing = Label.query.filter_by(key=key).first()
            if val:
                if existing:
                    existing.value = val
                else:
                    db.session.add(Label(key=key, value=val))
            elif existing:
                db.session.delete(existing)
        db.session.commit()
        flash(f"“{definition['name']}” text updated.", 'success')
        return redirect(url_for('admin_forms', _anchor=form_key))

    # GET: resolve current values for every field in every form definition.
    db_labels = {l.key: l.value for l in Label.query.all()}
    forms_data = []
    for definition in FORM_DEFINITIONS:
        groups = []
        for grp in definition['groups']:
            fields = []
            for key, friendly, kind in grp['fields']:
                default = LABEL_DEFAULTS.get(key, '')
                override = db_labels.get(key) or ''
                fields.append({
                    'key': key, 'name': friendly, 'kind': kind,
                    'value': override, 'default': default,
                    'overridden': bool(override and override != default),
                })
            groups.append({'label': grp['label'], 'fields': fields})
        forms_data.append({
            'key': definition['key'], 'name': definition['name'],
            'endpoint': definition.get('endpoint'),
            'has_custom_fields': definition.get('has_custom_fields', False),
            'groups': groups,
        })

    custom_fields = RegistrationField.query.order_by(RegistrationField.order).all()
    return render_template('admin/forms.html', forms=forms_data, custom_fields=custom_fields)


@app.route('/admin/fields', methods=['GET', 'POST'])
@login_required
def admin_fields():
    # Superseded by the unified Forms editor; keep the URL working.
    if not current_user.is_admin:
        abort(403)
    return redirect(url_for('admin_forms'))


@app.route('/admin/field/update', methods=['POST'])
@login_required
def admin_update_field():
    if not current_user.is_admin:
        abort(403)
    field_id = request.form.get('field_id')
    label_val = (request.form.get('label') or '').strip()

    if field_id == 'new':
        # Add custom field
        field = RegistrationField(
            field_name=request.form.get('field_name'),
            field_label=label_val or request.form.get('field_name'),
            field_type=request.form.get('field_type', 'text'),
            is_required=request.form.get('is_required') == 'on',
            options=request.form.get('options'),
        )
        db.session.add(field)
        flash('Field added.', 'success')
    elif field_id and field_id.startswith('_builtin_'):
        # Update label for a built-in form field (stored in Label table)
        label_key = field_id.replace('_builtin_', '', 1)
        existing = Label.query.filter_by(key=label_key).first()
        if label_val:
            if existing:
                existing.value = label_val
            else:
                db.session.add(Label(key=label_key, value=label_val))
        else:
            if existing:
                db.session.delete(existing)
        flash('Field label updated.', 'success')
    else:
        # Update custom field
        field = RegistrationField.query.get_or_404(int(field_id))
        field.field_label = label_val or field.field_name
        field.field_type = request.form.get('field_type', field.field_type)
        field.is_required = request.form.get('is_required') == 'on'
        field.options = request.form.get('options')
        flash('Field updated.', 'success')

    db.session.commit()
    return redirect(url_for('admin_forms', _anchor='register'))


@app.route('/admin/field/<int:id>/data')
@login_required
def admin_field_data(id):
    if not current_user.is_admin:
        abort(403)
    field = RegistrationField.query.get_or_404(id)
    return {
        'field_label': field.field_label,
        'field_type': field.field_type,
        'is_required': field.is_required,
        'options': field.options or '',
    }


@app.route('/admin/field/delete/<int:id>', methods=['POST'])
@login_required
def admin_delete_field(id):
    if not current_user.is_admin:
        abort(403)
    field = RegistrationField.query.get_or_404(id)
    db.session.delete(field)
    db.session.commit()
    flash('Field deleted.', 'success')
    return redirect(url_for('admin_forms', _anchor='register'))

@app.route('/admin/page-titles', methods=['GET', 'POST'])
@login_required
def admin_page_titles():
    if not current_user.is_admin:
        abort(403)
    if request.method == 'POST':
        for entry in PAGE_TITLES:
            key = f'page_title_{entry["key"]}'
            submitted = request.form.get(key, '').strip()
            existing = Label.query.filter_by(key=key).first()
            if submitted and submitted != entry['default']:
                if existing:
                    existing.value = submitted
                else:
                    db.session.add(Label(key=key, value=submitted))
            elif existing and (not submitted or submitted == entry['default']):
                db.session.delete(existing)
        db.session.commit()
        flash('Page titles updated.', 'success')
        return redirect(url_for('admin_page_titles'))
    db_labels = {l.key: l.value for l in Label.query.filter(Label.key.like('page_title_%')).all()}
    titles = []
    for entry in PAGE_TITLES:
        key = f'page_title_{entry["key"]}'
        titles.append({**entry, 'key': key, 'value': db_labels.get(key, entry['default'])})
    return render_template('admin/page_titles.html', titles=titles)


@app.route('/admin/email-templates', methods=['GET', 'POST'])
@login_required
def admin_email_templates():
    if not current_user.is_admin:
        abort(403)
    from utils.email_sender import EMAIL_TEMPLATES, TEMPLATE_VARIABLE_DESCRIPTIONS
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'update':
            key = request.form.get('key')
            tmpl = EmailTemplate.query.filter_by(key=key).first()
            subject = request.form.get('subject', '').strip()
            title = request.form.get('title', '').strip()
            body_html = request.form.get('body_html', '').strip()
            if tmpl:
                tmpl.subject = subject
                tmpl.title = title
                tmpl.body_html = body_html
                tmpl.is_confirmed = False
            else:
                tmpl = EmailTemplate(key=key, subject=subject, title=title, body_html=body_html)
                db.session.add(tmpl)
            db.session.commit()
            flash('Email template updated. It needs to be confirmed before use.', 'warning')
        elif action == 'confirm':
            key = request.form.get('key')
            tmpl = EmailTemplate.query.filter_by(key=key).first()
            if tmpl:
                tmpl.is_confirmed = True
                db.session.commit()
                flash(f'Template "{key}" confirmed.', 'success')
        elif action == 'confirm_all':
            EmailTemplate.query.update({'is_confirmed': True})
            db.session.commit()
            flash('All templates confirmed.', 'success')
        elif action == 'reset_all':
            # Restore every template body/subject/title to the built-in default.
            # Leaves them unconfirmed so they get reviewed before use.
            existing = {t.key: t for t in EmailTemplate.query.all()}
            for default in EMAIL_TEMPLATES:
                tmpl = existing.get(default['key'])
                if tmpl:
                    tmpl.subject = default['subject']
                    tmpl.title = default['title']
                    tmpl.body_html = default['body_html']
                    tmpl.is_confirmed = False
                else:
                    db.session.add(EmailTemplate(
                        key=default['key'], subject=default['subject'],
                        title=default['title'], body_html=default['body_html'],
                        is_confirmed=False))
            db.session.commit()
            flash('All templates reset to default. Review them and confirm before use.', 'info')
        elif action == 'reset':
            key = request.form.get('key')
            from utils.email_sender import EMAIL_TEMPLATES, TEMPLATE_VARIABLE_DESCRIPTIONS
            default = next((t for t in EMAIL_TEMPLATES if t['key'] == key), None)
            if default:
                tmpl = EmailTemplate.query.filter_by(key=key).first()
                if tmpl:
                    tmpl.subject = default['subject']
                    tmpl.title = default['title']
                    tmpl.body_html = default['body_html']
                    tmpl.is_confirmed = False
                else:
                    tmpl = EmailTemplate(key=key, subject=default['subject'], title=default['title'], body_html=default['body_html'])
                    db.session.add(tmpl)
                db.session.commit()
                flash(f'Template "{key}" reset to default.', 'info')
        return redirect(url_for('admin_email_templates'))

    db_templates = {t.key: t for t in EmailTemplate.query.all()}
    from utils.email_sender import (EMAIL_TEMPLATES, SITE_WIDE_VARIABLES,
                                     EMAIL_STYLE_CLASS_DESCRIPTIONS)
    templates = []
    for et in EMAIL_TEMPLATES:
        db_t = db_templates.get(et['key'])
        # Split placeholders: site-wide ones are filled automatically everywhere;
        # the rest are specific to this particular email.
        site_vars = [v for v in et['variables'] if v in SITE_WIDE_VARIABLES]
        msg_vars = [v for v in et['variables'] if v not in SITE_WIDE_VARIABLES]
        templates.append({
            'key': et['key'],
            'label': et['label'],
            'description': et['description'],
            'variables': et['variables'],
            'site_vars': site_vars,
            'msg_vars': msg_vars,
            'subject': db_t.subject if db_t else et['subject'],
            'title': db_t.title if db_t else et['title'],
            'body_html': db_t.body_html if db_t else et['body_html'],
            'is_confirmed': db_t.is_confirmed if db_t else False,
            'is_default': db_t is None,
        })

    confirmed_count = sum(1 for t in templates if t['is_confirmed'])
    unconfirmed_count = len(templates) - confirmed_count

    return render_template('admin/email_templates.html',
                           templates=templates,
                           confirmed_count=confirmed_count,
                           unconfirmed_count=unconfirmed_count,
                           variable_descriptions=TEMPLATE_VARIABLE_DESCRIPTIONS,
                           style_classes=EMAIL_STYLE_CLASS_DESCRIPTIONS)


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
        invite_orgs         = request.form.get('invite_orgs') == 'on'
        invite_individuals  = request.form.get('invite_individuals') == 'on'
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
                errors      = []
                seen_emails = set()

                # ── Collect valid rows from CSV (no sending yet) ──────────────
                # For email-only modes we just need email+name.
                # For normal import we need the full set of fields.
                valid_rows  = []   # list of dicts: {email, name, ...row}
                for row_num, row in enumerate(csv_reader, start=2):
                    if invite_orgs:
                        required = ['email', 'name', 'organization']
                    elif invite_individuals or custom_message_mode or event_invite_mode:
                        required = ['email', 'name']
                    else:
                        required = ['email', 'name', 'organization', 'stakeholder_type', 'country']
                    missing = [f for f in required if not row.get(f) or not row.get(f).strip()]
                    if missing:
                        errors.append(f"Row {row_num}: Missing fields {missing}")
                        continue
                    email = row['email'].lower().strip()
                    name  = row.get('name', '').strip()
                    # Deduplicate within the file
                    if email in seen_emails:
                        errors.append(f"Row {row_num}: {email} is a duplicate in this file — skipped")
                        continue
                    seen_emails.add(email)
                    valid_rows.append({'_row_num': row_num, '_email': email, '_name': name, **row})

                # ── NORMAL IMPORT MODE (synchronous — creates DB records) ─────
                if not (invite_orgs or invite_individuals or custom_message_mode or event_invite_mode):
                    imported = 0
                    db_types = get_stakeholder_types()
                    for r in valid_rows:
                        email = r['_email']
                        name  = r['_name']
                        row_num = r['_row_num']
                        if User.query.filter_by(email=email).first():
                            errors.append(f"Row {row_num}: Email already exists")
                            continue
                        if r['stakeholder_type'].strip() not in db_types:
                            errors.append(f"Row {row_num}: Invalid stakeholder_type")
                            continue
                        user = User(
                            email=email,
                            name=name,
                            organization=r['organization'].strip(),
                            stakeholder_type=r['stakeholder_type'].strip(),
                            country=r['country'].strip(),
                            is_approved=True,
                            is_admin=False
                        )
                        db.session.add(user)
                        db.session.flush()
                        if not BlockedEmail.query.filter_by(email=email).first():
                            try:
                                send_import_welcome_email(user)
                            except Exception as e:
                                app.logger.error(f"Import welcome email error for {user.email}: {e}")
                        imported += 1
                    db.session.commit()
                    flash(f'Imported {imported} members. Errors: {len(errors)}', 'info' if errors else 'success')
                    if errors:
                        for err in errors[:5]:
                            flash(err, 'error')
                    return redirect(url_for('admin_import_members'))

                # ── EMAIL MODES: filter recipients then send in background ────
                # Apply per-mode eligibility checks synchronously so we can give
                # accurate counts/errors before handing off to the background thread.
                to_send = []   # list of (email, name) tuples that passed all checks

                if event_invite_mode:
                    if not selected_event:
                        flash('Please select an event for event invitation mode.', 'error')
                        return redirect(request.url)
                    for r in valid_rows:
                        email, name, row_num = r['_email'], r['_name'], r['_row_num']
                        if BlockedEmail.query.filter_by(email=email).first():
                            errors.append(f"Row {row_num}: {email} has unsubscribed — skipped")
                            continue
                        to_send.append((email, name))

                elif custom_message_mode:
                    if not custom_subject or not custom_body:
                        flash('Custom subject and message body are required in Custom Message mode.', 'error')
                        return redirect(request.url)
                    for r in valid_rows:
                        email, name, row_num = r['_email'], r['_name'], r['_row_num']
                        if BlockedEmail.query.filter_by(email=email).first():
                            errors.append(f"Row {row_num}: {email} has unsubscribed — skipped")
                            continue
                        to_send.append((email, name))

                elif invite_orgs:
                    for r in valid_rows:
                        email, name, row_num = r['_email'], r['_name'], r['_row_num']
                        org = r.get('organization', '').strip()
                        if User.query.filter_by(email=email).first():
                            errors.append(f"Row {row_num}: {email} is already a member — skipped")
                            continue
                        if BlockedEmail.query.filter_by(email=email).first():
                            errors.append(f"Row {row_num}: {email} has unsubscribed — skipped")
                            continue
                        to_send.append((email, name, org))

                elif invite_individuals:
                    for r in valid_rows:
                        email, name, row_num = r['_email'], r['_name'], r['_row_num']
                        if User.query.filter_by(email=email).first():
                            errors.append(f"Row {row_num}: {email} is already a member — skipped")
                            continue
                        if BlockedEmail.query.filter_by(email=email).first():
                            errors.append(f"Row {row_num}: {email} has unsubscribed — skipped")
                            continue
                        to_send.append((email, name))

                skipped = len(valid_rows) - len(to_send)

                # ── Fire sending in a background thread in batches of 50 ──────
                BATCH_SIZE  = 50
                BATCH_PAUSE = 1.0   # seconds between batches

                def _send_batch(flask_app, recipients, mode,
                                ev=None, ev_url=None, subj=None, body=None):
                    with flask_app.app_context():
                        flask_app.logger.info(f"Background send started: {len(recipients)} recipients, mode={mode}")
                        for i in range(0, len(recipients), BATCH_SIZE):
                            batch = recipients[i:i + BATCH_SIZE]
                            for item in batch:
                                try:
                                    if mode == 'invite':
                                        em, nm, org = item
                                        send_invitation_email(em, nm, organization=org)
                                    elif mode == 'invite_individuals':
                                        em, nm = item
                                        send_individual_invitation_email(em, nm)
                                    elif mode == 'event':
                                        em, nm = item
                                        from utils.email_sender import send_event_invitation_email
                                        send_event_invitation_email(em, nm, ev, ev_url)
                                    elif mode == 'custom':
                                        em, nm = item
                                        send_custom_bulk_email(em, nm, subj, body)
                                except Exception as ex:
                                    flask_app.logger.error(f"Batch send error for {item[0]}: {ex}")
                            if i + BATCH_SIZE < len(recipients):
                                time.sleep(BATCH_PAUSE)

                if to_send:
                    mode_key = 'event' if event_invite_mode else ('custom' if custom_message_mode else ('invite_individuals' if invite_individuals else 'invite'))
                    t = threading.Thread(
                        target=_send_batch,
                        args=(app, to_send, mode_key),
                        kwargs=dict(
                            ev=selected_event,
                            ev_url=event_invite_url,
                            subj=custom_subject,
                            body=custom_body,
                        ),
                        daemon=True,
                    )
                    t.start()

                label = 'event invitation' if event_invite_mode else ('message' if custom_message_mode else ('individual invitation' if invite_individuals else 'invitation'))
                flash(
                    f'Queued {len(to_send)} {label}(s) for sending in the background '
                    f'({skipped} skipped, {len(errors)} error(s)).',
                    'info' if errors else 'success'
                )
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
        'learn_more_requests_sent', 'learn_more_requests_received',
    ])

    for user in members:
        initiatives_published = Initiative.query.filter_by(user_id=user.id, is_published=True).count()
        initiatives_total     = Initiative.query.filter_by(user_id=user.id).count()
        event_regs            = EventRegistration.query.filter_by(user_id=user.id).count()
        project_parts         = ProjectParticipation.query.filter_by(user_id=user.id).count()
        lm_sent               = LearnMoreRequest.query.filter_by(requester_id=user.id).count()
        # Requests received = learn-more requests on initiatives this user published
        lm_received           = (LearnMoreRequest.query
                                 .join(Initiative, LearnMoreRequest.initiative_id == Initiative.id)
                                 .filter(Initiative.user_id == user.id)
                                 .count())

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
            lm_sent,
            lm_received,
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
                valid_types = get_stakeholder_types()
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

# ===================== ADMIN: UNVERIFIED / SPAM CLEANUP =====================

@app.route('/admin/unverified', methods=['GET', 'POST'])
@login_required
def admin_unverified():
    """Review/clean submissions that were published without a real AI quality
    score (the signature of the auto-approve exploit during an API outage)."""
    if not current_user.is_admin:
        abort(403)

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'approve':
            ini = Initiative.query.get_or_404(request.form.get('id', type=int))
            ini.is_published = True
            author = User.query.get(ini.user_id)
            if author:
                author.is_approved = True
            db.session.commit()
            flash('Initiative published and its author approved.', 'success')
        elif action == 'delete_all':
            unverified = Initiative.query.filter(Initiative.quality_score.is_(None)).all()
            author_ids = {i.user_id for i in unverified}
            n_ini = len(unverified)
            for ini in unverified:
                db.session.delete(ini)   # ORM handles tags + cascades child rows
            db.session.commit()
            # Delete throwaway accounts: authors left with no real content, non-admin.
            deleted_accounts = 0
            for uid in author_ids:
                u = User.query.get(uid)
                if not u or u.is_admin:
                    continue
                remaining = (Initiative.query.filter_by(user_id=uid).count()
                             + Question.query.filter_by(user_id=uid).count()
                             + Recommendation.query.filter_by(user_id=uid).count()
                             + DocumentLibrary.query.filter_by(submitted_by=uid).count())
                if remaining:
                    continue
                Certificate.query.filter_by(user_id=uid).delete()
                Comment.query.filter_by(user_id=uid).delete()
                LearnMoreRequest.query.filter_by(requester_id=uid).delete()
                MemberProject.query.filter_by(user_id=uid).delete()
                ProjectParticipation.query.filter_by(user_id=uid).delete()
                EventRegistration.query.filter_by(user_id=uid).delete()
                Vote.query.filter_by(user_id=uid).delete()
                db.session.delete(u)
                deleted_accounts += 1
            db.session.commit()
            flash(f'Deleted {n_ini} unverified submission(s) and {deleted_accounts} spam account(s).', 'success')
        return redirect(url_for('admin_unverified'))

    items = (Initiative.query.filter(Initiative.quality_score.is_(None))
             .order_by(Initiative.created_at.desc()).all())
    return render_template('admin/unverified.html', items=items)


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
    
    # Block deletion while the member still owns content — reassign it first.
    initiative_count = Initiative.query.filter_by(user_id=id).count()
    question_count = Question.query.filter_by(user_id=id).count()
    recommendation_count = Recommendation.query.filter_by(user_id=id).count()
    document_count = DocumentLibrary.query.filter_by(submitted_by=id).count()

    if initiative_count or question_count or recommendation_count or document_count:
        flash(f'Cannot delete {email}: user still owns {initiative_count} initiative(s), '
              f'{question_count} question(s), {recommendation_count} recommendation(s) and '
              f'{document_count} document(s). Use "Reassign content to me" (the person icon) '
              f'first, then delete.', 'error')
        return redirect(url_for('admin_dashboard'))

    # Remove the member's personal records that cannot be reassigned. (Comments are
    # cascade-deleted at the DB level, but the certificate has a non-nullable FK with
    # an ORM relationship, so it must be removed explicitly before the user.)
    Certificate.query.filter_by(user_id=id).delete()
    Comment.query.filter_by(user_id=id).delete()
    LearnMoreRequest.query.filter_by(requester_id=id).delete()
    MemberProject.query.filter_by(user_id=id).delete()
    ProjectParticipation.query.filter_by(user_id=id).delete()
    EventRegistration.query.filter_by(user_id=id).delete()
    Vote.query.filter_by(user_id=id).delete()

    # Delete the user
    db.session.delete(user)
    db.session.commit()
    
    flash(f'Member {email} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/member/<int:id>/reassign', methods=['POST'])
@login_required
def admin_reassign_member(id):
    """Reassign all of a member's content (initiatives, questions, recommendations,
    projects, events, policy developments, documents) to the current admin. Lets an
    admin take over a member's published work so the member can then be deleted
    without losing that content."""
    if not current_user.is_admin:
        abort(403)
    user = User.query.get_or_404(id)
    if id == current_user.id:
        flash('You already own your own content.', 'info')
        return redirect(request.referrer or url_for('admin_members'))

    admin_id = current_user.id
    moved = 0
    moved += Initiative.query.filter_by(user_id=id).update({'user_id': admin_id})
    moved += Question.query.filter_by(user_id=id).update({'user_id': admin_id})
    moved += Recommendation.query.filter_by(user_id=id).update({'user_id': admin_id})
    moved += Project.query.filter_by(submitted_by=id).update({'submitted_by': admin_id})
    moved += Event.query.filter_by(created_by=id).update({'created_by': admin_id})
    moved += Event.query.filter_by(submitted_by=id).update({'submitted_by': admin_id})
    moved += PolicyDevelopment.query.filter_by(submitted_by=id).update({'submitted_by': admin_id})
    moved += DocumentLibrary.query.filter_by(submitted_by=id).update({'submitted_by': admin_id})
    db.session.commit()

    flash(f'Reassigned {moved} item(s) from {user.email} to you. '
          'This member can now be deleted.', 'success')
    return redirect(request.referrer or url_for('admin_members'))


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

    return render_template('admin/edit_member.html', user=user, stakeholder_types=get_stakeholder_types())

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
    policy.view_count = (policy.view_count or 0) + 1
    db.session.commit()
    return render_template('policy_development_detail.html', policy=policy)


# ===================== DOCUMENT LIBRARY =====================

DOCUMENT_ALLOWED_EXTENSIONS = {
    'pdf', 'doc', 'docx', 'txt', 'xls', 'xlsx', 'ppt', 'pptx'
}


def allowed_document(filename):
    return '.' in filename and            filename.rsplit('.', 1)[1].lower() in DOCUMENT_ALLOWED_EXTENSIONS


@app.route('/documents')
def documents():
    """Public document library — listing of all published documents."""
    page = request.args.get('page', 1, type=int)
    tag_name = request.args.get('tag', '').strip()
    year = request.args.get('year', '', type=int)
    search = request.args.get('q', '').strip()

    query = DocumentLibrary.query.filter_by(is_published=True)

    if tag_name:
        tag = Tag.query.filter_by(name=tag_name).first()
        if tag:
            query = query.filter(DocumentLibrary.tags.contains(tag))

    if year:
        query = query.filter(DocumentLibrary.year_published == year)

    if search:
        query = query.filter(
            db.or_(
                DocumentLibrary.title.ilike(f'%{search}%'),
                DocumentLibrary.description.ilike(f'%{search}%'),
                DocumentLibrary.extracted_text.ilike(f'%{search}%')
            )
        )

    pagination = query.order_by(
        DocumentLibrary.year_published.desc().nullslast(),
        DocumentLibrary.created_at.desc()
    ).paginate(page=page, per_page=12, error_out=False)

    # Get all years and tags for filters
    years = (
        db.session.query(DocumentLibrary.year_published)
        .filter(DocumentLibrary.is_published == True, DocumentLibrary.year_published != None)
        .distinct().order_by(DocumentLibrary.year_published.desc()).all()
    )
    years = [y[0] for y in years if y[0]]

    all_tags = Tag.query.join(document_tags).filter(
        Tag.documents.any(DocumentLibrary.is_published == True)
    ).distinct().order_by(Tag.name).all()

    return render_template('documents.html',
                         documents=pagination.items,
                         pagination=pagination,
                         years=years,
                         tags=all_tags,
                         selected_tag=tag_name,
                         selected_year=year,
                         search=search)


@app.route('/document/<int:id>')
def view_document(id):
    """Public view of a single published document."""
    doc = DocumentLibrary.query.filter_by(id=id, is_published=True).first_or_404()
    doc.view_count = (doc.view_count or 0) + 1
    db.session.commit()
    return render_template('document_detail.html', doc=doc)


@app.route('/document/<int:id>/download')
def download_document(id):
    """Serve a document file. Publicly accessible for published docs."""
    doc = DocumentLibrary.query.get_or_404(id)
    if not doc.is_published and (not current_user.is_authenticated or not current_user.is_admin):
        abort(403)
    folder = os.path.join(app.config['UPLOAD_FOLDER'], 'documents')
    return send_from_directory(
        folder, doc.stored_name,
        as_attachment=True,
        download_name=doc.filename,
    )


@app.route('/document/upload', methods=['GET', 'POST'])
@login_required
def upload_document():
    """Member-facing document upload form."""
    if request.method == 'POST':
        if 'file' not in request.files:
            flash('No file selected.', 'error')
            return redirect(request.url)

        file = request.files['file']
        if file.filename == '':
            flash('No file selected.', 'error')
            return redirect(request.url)

        if not allowed_document(file.filename):
            flash('Invalid file type. Allowed: PDF, DOC, DOCX, TXT, XLS, XLSX, PPT, PPTX.', 'error')
            return redirect(request.url)

        original, stored, file_size, ext = save_document(file)

        doc = DocumentLibrary(
            filename=original,
            stored_name=stored,
            file_size=file_size,
            file_type=ext,
            submitted_by=current_user.id,
            processing_status='pending',
        )
        db.session.add(doc)
        db.session.commit()

        # Start background processing
        t = threading.Thread(
            target=_process_document_async,
            args=(app, doc.id),
            daemon=True
        )
        t.start()

        flash('Document uploaded successfully! It is being processed and will appear after admin review.', 'success')
        return redirect(url_for('documents'))

    return render_template('document_upload.html', doc=None)


# ===================== ADMIN DOCUMENT ROUTES =====================

@app.route('/admin/documents')
@login_required
def admin_documents():
    """Admin view of all documents including pending/failed."""
    if not current_user.is_admin:
        abort(403)
    docs = DocumentLibrary.query.order_by(DocumentLibrary.created_at.desc()).all()
    return render_template('admin/documents.html', documents=docs)


@app.route('/admin/document/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def admin_edit_document(id):
    """Edit a document's metadata."""
    if not current_user.is_admin:
        abort(403)
    doc = DocumentLibrary.query.get_or_404(id)
    if request.method == 'POST':
        doc.title = request.form.get('title', '').strip()[:300] or doc.title
        doc.description = request.form.get('description', '').strip()[:500] or None
        doc.year_published = request.form.get('year_published', type=int) or None
        doc.updated_at = datetime.utcnow()

        # Handle tags
        raw_tags = request.form.get('tags', '')
        if raw_tags:
            doc.tags = []
            for tag_name in [t.strip() for t in raw_tags.split(',') if t.strip()]:
                tag = Tag.query.filter_by(name=tag_name).first()
                if not tag:
                    tag = Tag(name=tag_name, is_vetted=True)
                    db.session.add(tag)
                    db.session.flush()
                doc.tags.append(tag)
                tag.usage_count = (tag.usage_count or 0) + 1

        db.session.commit()
        flash('Document updated.', 'success')
        return redirect(url_for('admin_documents'))

    return render_template('admin/edit_document.html', doc=doc)


@app.route('/admin/document/<int:id>/approve', methods=['POST'])
@login_required
def admin_approve_document(id):
    """Approve and publish a document."""
    if not current_user.is_admin:
        abort(403)
    doc = DocumentLibrary.query.get_or_404(id)
    doc.is_published = True
    doc.updated_at = datetime.utcnow()
    # Add to send queue if not already there
    if not doc.send_queue_entry:
        db.session.add(DocumentSendQueue(document_id=doc.id))
    db.session.commit()
    flash('Document published and added to send queue.', 'success')
    return redirect(request.referrer or url_for('admin_documents'))


@app.route('/admin/document/<int:id>/delete', methods=['POST'])
@login_required
def admin_delete_document(id):
    """Delete a document and its file."""
    if not current_user.is_admin:
        abort(403)
    doc = DocumentLibrary.query.get_or_404(id)

    # Delete file from disk
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], 'documents', doc.stored_name)
    try:
        os.remove(filepath)
    except OSError:
        pass

    db.session.delete(doc)
    db.session.commit()
    flash('Document deleted.', 'success')
    return redirect(request.referrer or url_for('admin_documents'))


@app.route('/admin/document/<int:id>/reprocess', methods=['POST'])
@login_required
def admin_reprocess_document(id):
    """Re-trigger background AI processing for a document."""
    if not current_user.is_admin:
        abort(403)
    doc = DocumentLibrary.query.get_or_404(id)
    doc.processing_status = 'pending'
    doc.processing_error = None
    db.session.commit()

    t = threading.Thread(
        target=_process_document_async,
        args=(app, doc.id),
        daemon=True
    )
    t.start()

    flash('Reprocessing started.', 'info')
    return redirect(request.referrer or url_for('admin_documents'))


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


@app.route('/backfill-status')
def backfill_status():
    """Read-only progress of the one-time AI backfills (no sensitive data)."""
    import json as _json
    try:
        done_ids = _json.loads(get_setting('summaries_done_ids', '[]') or '[]')
    except Exception:
        done_ids = []
    return {
        'summaries': {
            'status': get_setting('summaries_backfill_status') or 'pending',
            'flag': get_setting('summaries_backfilled', 'false'),
            'done_count': len(done_ids),
        },
        'titles': {
            'status': get_setting('titles_backfill_status') or 'pending',
            'flag': get_setting('titles_backfilled', 'false'),
        },
        'quarantine': get_setting('quarantine_status') or 'not run',
        'purge': get_setting('purge_status') or 'not run',
        'ai_scoring_healthy': get_setting('ai_scoring_healthy', 'true'),
    }, 200

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

    # Seed default stakeholder types (idempotent)
    if not StakeholderType.query.first():
        for i, name in enumerate(DEFAULT_STAKEHOLDER_TYPES):
            st = StakeholderType(
                name=name,
                is_member_state=False,
                is_active=True,
                order=i,
            )
            db.session.add(st)

    # Seed default labels (idempotent)
    if not Label.query.first():
        for key, default in LABEL_DEFAULTS.items():
            db.session.add(Label(key=key, value='', category=key.split('_')[0]))

    # Create admin user (idempotent — skip if already exists)
    admin = User.query.filter_by(email=Config.ADMIN_EMAIL).first()
    if not admin:
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

    # Default registration fields (idempotent)
    if not RegistrationField.query.first():
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
