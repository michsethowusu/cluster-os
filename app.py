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
    send_event_registration_confirmation,
    send_custom_bulk_email,
)
from utils.ai_services import generate_title_description, vet_tags_nvidia, rank_members_by_query, clean_tags_for_polls, score_initiative_quality
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
    zoom_webinar_id    = db.Column(db.String(100), nullable=True)
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
    filename    = db.Column(db.String(300), nullable=False)
    stored_name = db.Column(db.String(300), nullable=False)
    label       = db.Column(db.String(200))
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)


class BlockedEmail(db.Model):
    """Emails that have unsubscribed and are NOT in our member DB."""
    id         = db.Column(db.Integer, primary_key=True)
    email      = db.Column(db.String(120), unique=True, nullable=False)
    blocked_at = db.Column(db.DateTime, default=datetime.utcnow)


class InitiativeSendQueue(db.Model):
    """Holds auto-approved initiatives with quality score >= 4 ready to broadcast."""
    id             = db.Column(db.Integer, primary_key=True)
    initiative_id  = db.Column(db.Integer, db.ForeignKey('initiative.id'), nullable=False, unique=True)
    queued_at      = db.Column(db.DateTime, default=datetime.utcnow)
    sent_at        = db.Column(db.DateTime, nullable=True)

    initiative = db.relationship('Initiative', backref='send_queue_entry')


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
    """Add an initiative to the send queue if not already there."""
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

ALLOWED_ATTACHMENT_EXTENSIONS = {
    'pdf', 'doc', 'docx', 'xls', 'xlsx',
    'ppt', 'pptx', 'txt', 'zip', 'png',
    'jpg', 'jpeg', 'gif', 'mp4', 'mp3',
}

def allowed_attachment(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_ATTACHMENT_EXTENSIONS

def save_attachment(file_obj):
    """Save an uploaded attachment. Returns (original_filename, stored_name) tuple."""
    original = secure_filename(file_obj.filename)
    ext      = original.rsplit('.', 1)[1].lower() if '.' in original else 'bin'
    stored   = f"{uuid.uuid4().hex}.{ext}"
    folder   = os.path.join(app.config['UPLOAD_FOLDER'], 'event_attachments')
    os.makedirs(folder, exist_ok=True)
    file_obj.save(os.path.join(folder, stored))
    return original, stored

# ===================== POINTS SYSTEM =====================
POINTS = {
    'recommendation_posted': 10,
    'recommendation_upvote': 5,
    'recommendation_downvote': -2,
    'initiative_published': 20,
    'question_published': 10,
    'event_registered': 5,
    'project_participated': 15,
}

def award_points(user, activity=None, commit=True):
    """Recalculate a user's total points based on their actual database history."""
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
    stats = {
        'total_members': User.query.filter_by(is_approved=True).count(),
        'total_initiatives': Initiative.query.filter_by(is_published=True).count(),
        'stakeholders': {}
    }
    
    for stype in ['Government', 'NGO / Civil Society', 'Development Partner / Donor', 
                  'Academic / Research', 'UN Agency', 'Private Sector']:
        stats['stakeholders'][stype] = User.query.filter_by(
            stakeholder_type=stype, is_approved=True
        ).count()
    
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
            
        if user.is_admin:
            from werkzeug.security import check_password_hash
            password = request.form.get('password')
            if not password:
                return render_template('login.html', show_password=True, email=email)
            if not user.password_hash or not check_password_hash(user.password_hash, password):
                flash('Invalid password.', 'error')
                return redirect(url_for('login'))

            otp = ''.join(random.choices(string.digits, k=6))
            user.otp = otp
            user.otp_expiry = datetime.utcnow() + timedelta(minutes=10)
            db.session.commit()

            otp_dest = app.config.get('ADMIN_OTP_EMAIL') or user.email
            send_otp_email(otp_dest, otp)
            flash(f'Password accepted. OTP sent to {otp_dest}.', 'info')
            return redirect(url_for('verify_otp', email=email))
        
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
        
        initiative_title = request.form.get('initiative_title', '').strip()
        initiative_short_desc = request.form.get('initiative_short_description', '').strip()
        initiative_content = request.form.get('initiative_content', '').strip()

        if not initiative_title:
            flash('Please provide an initiative title.', 'error')
            return redirect(url_for('register'))
        if not initiative_content:
            flash('Please provide initiative content.', 'error')
            return redirect(url_for('register'))

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
            is_published=True
        )
        db.session.add(initiative)
        db.session.commit()

        award_points(user, 'initiative_published')

        try:
            send_approval_email(user.email, initiative.slug)
        except Exception as e:
            app.logger.error(f"Registration welcome email error: {e}")

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
                    flask_app.logger.error(f"Registration initiative quality scoring error: {e}")

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
    
    users = User.query.filter(
        User.is_approved == True,
        User.initiatives.any(Initiative.is_published == True)
    ).all()

    if not users:
        return render_template('search_members.html', query=query, results=[])
    
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
        to_email='cluster@eced-au.org',
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
        content = request.form.get('content')
        
        slug = re.sub(r'[^\w]+', '-', title.lower()).strip('-')
        base_slug = slug
        counter = 1
        while Initiative.query.filter_by(slug=slug).first():
            slug = f"{base_slug}-{counter}"
            counter += 1
        
        initiative = Initiative(
            title=title,
            slug=slug,
            content=content,
            short_description=short_description[:300] if short_description else None,
            user_id=current_user.id,
            stakeholder_type=current_user.stakeholder_type,
            country=current_user.country,
            is_published=True
        )
        
        db.session.add(initiative)
        db.session.commit()

        award_points(current_user, 'initiative_published')

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

        threading.Thread(
            target=_score_async,
            args=(app, initiative.id, title, content, short_description),
            daemon=True
        ).start()

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
    
    initiative.tags = []
    db.session.commit()
    
    NounPhrase.query.filter_by(initiative_id=id).delete()
    
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
    db.session.delete(project)
    db.session.commit()
    flash(f'Project "{title}" has been deleted.', 'success')
    return redirect(url_for('admin_approvals', type='projects'))

@app.route('/initiative/<slug>')
def view_initiative(slug):
    initiative = Initiative.query.filter_by(slug=slug, is_published=True).first_or_404()
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

    # Get all published initiatives ordered by latest comment or creation date
    # We use a subquery to get the latest approved comment date per initiative
    from sqlalchemy import func, case, literal

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

    # For each initiative, attach the approved comment count and last few comments
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
        if not current_user.is_admin:
            pass
        
        initiative.title = request.form.get('title')
        initiative.short_description = request.form.get('short_description')[:300] if request.form.get('short_description') else None
        initiative.content = request.form.get('content')
        initiative.updated_at = datetime.utcnow()
        
        if request.form.get('regenerate_tags'):
            try:
                phrases = extract_noun_phrases(initiative.content)
                vetted_tags = vet_tags_nvidia(phrases)
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
    tag_name = request.args.get('tag', '')
    initiatives = Initiative.query.filter_by(is_published=True)
    
    if tag_name:
        tag = Tag.query.filter_by(name=tag_name).first()
        if tag:
            initiatives = initiatives.filter(Initiative.tags.contains(tag))
    
    initiatives = initiatives.order_by(Initiative.created_at.desc()).all()
    tags = Tag.query.order_by(Tag.name).all()
    
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

# ===================== FORUM ROUTES (kept for data integrity, nav removed) =====================

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
    rec_author = User.query.get(recommendation.user_id)
    if rec_author:
        if vote_type == 1:
            award_points(rec_author, 'recommendation_upvote', commit=False)
        elif vote_type == -1:
            award_points(rec_author, 'recommendation_downvote', commit=False)
    db.session.commit()
    return jsonify({'success': True, 'score': recommendation.score})

# ===================== MEMBERS =====================

@app.route('/members')
def members():
    type_filter = request.args.get('type', '')
    orgs_query = db.session.query(
        User.organization,
        User.stakeholder_type,
        db.func.count(User.id).label('member_count')
    ).filter_by(is_approved=True)
    if type_filter:
        orgs_query = orgs_query.filter(User.stakeholder_type == type_filter)
    orgs = orgs_query.group_by(User.organization, User.stakeholder_type).all()
    return render_template('members.html', organizations=orgs)

@app.route('/leaderboard')
def leaderboard():
    expert_stats = User.query.filter_by(is_approved=True, is_admin=False)\
        .order_by(User.points.desc())\
        .limit(10).all()

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
    event = Event.query.get_or_404(id)
    if not event.is_published and not (current_user.is_authenticated and current_user.is_admin):
        abort(404)
    
    is_registered = False
    if current_user.is_authenticated:
        is_registered = EventRegistration.query.filter_by(
            user_id=current_user.id, event_id=id
        ).first() is not None
    
    return render_template('event_detail.html', event=event, is_registered=is_registered)

@app.route('/event/<int:id>/register', methods=['POST'])
@login_required
def register_event(id):
    event = Event.query.get_or_404(id)
    if not event.is_published:
        abort(404)
    
    existing = EventRegistration.query.filter_by(
        user_id=current_user.id, event_id=id
    ).first()
    if existing:
        flash('You are already registered for this event.', 'info')
        return redirect(url_for('event_detail', id=id))
    
    poll_answers = {}
    for poll in event.polls:
        answer = request.form.get(f'poll_{poll.id}')
        if answer:
            poll_answers[str(poll.id)] = answer
    
    registration = EventRegistration(
        user_id=current_user.id,
        event_id=id,
        poll_answers=poll_answers
    )
    db.session.add(registration)
    db.session.commit()
    award_points(current_user, 'event_registered')

    if event.zoom_webinar_id:
        try:
            register_user_for_webinar(event.zoom_webinar_id, current_user)
        except Exception as e:
            app.logger.error(f"Zoom registration error for user {current_user.id}: {e}")

    try:
        send_event_registration_confirmation(current_user, event)
    except Exception as e:
        app.logger.error(f"Event registration confirmation email error: {e}")

    flash('Successfully registered for the event!', 'success')
    return redirect(url_for('event_detail', id=id))

@app.route('/polls')
def polls():
    upcoming_events = Event.query.filter(
        Event.is_published == True
    ).order_by(Event.start_date.desc()).all()
    return render_template('polls.html', events=upcoming_events)

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
    queue_count = InitiativeSendQueue.query.filter_by(sent_at=None).count()
    return render_template('admin/dashboard.html',
                         pending_users=pending_users,
                         pending_initiatives=pending_initiatives,
                         pending_questions=pending_questions,
                         pending_projects=pending_projects,
                         pending_events=pending_events,
                         pending_comments=pending_comments,
                         queue_count=queue_count)

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
    else:
        users = User.query.filter_by(is_approved=False).all()
        initiatives = Initiative.query.filter_by(is_published=False).all()
        questions = Question.query.filter_by(is_published=False).all()
        projects = Project.query.filter_by(is_published=False).all()
        events = Event.query.filter_by(is_published=False).all()
        comments = Comment.query.filter_by(is_approved=False).all()
        items = list(users) + list(initiatives) + list(questions) + list(projects) + list(events) + list(comments)
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

        if submitter:
            try:
                send_initiative_approved_email(submitter, item.slug, item.title)
            except Exception as e:
                app.logger.error(f"Initiative approved email error: {e}")

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
        item.is_published = True
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

    else:
        abort(400)

    return redirect(request.referrer or url_for('admin_dashboard'))

@app.route('/admin/approve-all', methods=['POST'])
@login_required
def approve_all():
    if not current_user.is_admin:
        abort(403)

    suppress_member_notifications = request.form.get('suppress_member_notifications') == '1'

    pending_users = User.query.filter_by(is_approved=False).all()
    approved_user_count = 0
    published_initiative_count = 0
    for user in pending_users:
        user.is_approved = True
        approved_user_count += 1

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

            if reg_initiative.quality_score is None or reg_initiative.quality_score >= 4:
                try:
                    existing = InitiativeSendQueue.query.filter_by(initiative_id=reg_initiative.id).first()
                    if not existing:
                        db.session.add(InitiativeSendQueue(initiative_id=reg_initiative.id))
                except Exception as e:
                    app.logger.error(f"Queue error on bulk approve (initiative {reg_initiative.id}): {e}")

        try:
            send_approval_email(user.email, reg_initiative.slug if reg_initiative else None)
        except Exception as e:
            app.logger.error(f"Bulk approve – welcome email error for {user.email}: {e}")

    db.session.commit()

    pending_initiatives = Initiative.query.filter_by(is_published=False).all()

    for initiative in pending_initiatives:
        initiative.is_published = True
        published_initiative_count += 1
        author = User.query.get(initiative.user_id)

        try:
            phrases = extract_noun_phrases(initiative.content)
            update_noun_phrase_db(initiative.id, phrases)
        except Exception as e:
            app.logger.error(f"Noun phrase error on bulk approve (initiative {initiative.id}): {e}")

        if author:
            award_points(author, 'initiative_published', commit=False)

        if author:
            try:
                send_initiative_approved_email(author, initiative.slug, initiative.title)
            except Exception as e:
                app.logger.error(f"Bulk approve – author email error for {author.email}: {e}")

        if initiative.quality_score is None or initiative.quality_score >= 4:
            try:
                existing = InitiativeSendQueue.query.filter_by(initiative_id=initiative.id).first()
                if not existing:
                    db.session.add(InitiativeSendQueue(initiative_id=initiative.id))
            except Exception as e:
                app.logger.error(f"Queue error on bulk approve (initiative {initiative.id}): {e}")

    db.session.commit()

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
    db.session.commit()
    flash('Item unpublished.', 'success')
    return redirect(url_for('admin_approvals'))


# ===================== SEND QUEUE ROUTES =====================

@app.route('/admin/send-queue')
@login_required
def admin_send_queue():
    if not current_user.is_admin:
        abort(403)
    unsent = (InitiativeSendQueue.query
              .filter_by(sent_at=None)
              .order_by(InitiativeSendQueue.queued_at.desc())
              .all())
    sent = (InitiativeSendQueue.query
            .filter(InitiativeSendQueue.sent_at.isnot(None))
            .order_by(InitiativeSendQueue.sent_at.desc())
            .limit(20).all())
    return render_template('admin/send_queue.html', unsent=unsent, sent=sent)


@app.route('/admin/send-queue/send/<int:queue_id>', methods=['POST'])
@login_required
def send_queue_item(queue_id):
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
        send_bulk_initiatives_digest([{
            'title': initiative.title,
            'short_description': initiative.short_description or '',
            'url': initiative_url,
        }], subscribed_users)
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
    if not current_user.is_admin:
        abort(403)
    entry = InitiativeSendQueue.query.get_or_404(queue_id)
    db.session.delete(entry)
    db.session.commit()
    flash('Initiative removed from send queue.', 'success')
    return redirect(url_for('admin_send_queue'))

@app.route('/admin/settings', methods=['GET', 'POST'])
@login_required
def admin_settings():
    if not current_user.is_admin:
        abort(403)
    if request.method == 'POST':
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
        auto_approve = 'true' if request.form.get('auto_approve_members') else 'false'
        set_setting('auto_approve_members', auto_approve)
        flash('Settings updated successfully.', 'success')
        return redirect(url_for('admin_settings'))
    
    stats = {
        'users': User.query.count(),
        'initiatives': Initiative.query.filter_by(is_published=True).count(),
        'pending_initiatives': Initiative.query.filter_by(is_published=False).count(),
        'phrases': NounPhrase.query.count(),
        'tags': Tag.query.count(),
        'questions': Question.query.filter_by(is_published=True).count(),
        'pending_questions': Question.query.filter_by(is_published=False).count()
    }
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
_bulk_score_job = {
    'running': False,
    'total': 0,
    'done': 0,
    'errors': 0,
    'last_title': '',
}
_bulk_score_lock = threading.Lock()


def _run_bulk_scoring(flask_app):
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
                with _bulk_score_lock:
                    _bulk_score_job['done'] += 1
                    _bulk_score_job['last_title'] = ini.title
            except Exception as e:
                flask_app.logger.error(f"Bulk score error for initiative {ini.id}: {e}")
                with _bulk_score_lock:
                    _bulk_score_job['errors'] += 1
                    _bulk_score_job['done'] += 1

        with _bulk_score_lock:
            _bulk_score_job['running'] = False


@app.route('/admin/bulk-score', methods=['POST'])
@login_required
def admin_bulk_score():
    if not current_user.is_admin:
        abort(403)
    with _bulk_score_lock:
        if _bulk_score_job['running']:
            flash('A bulk-scoring job is already running.', 'warning')
            return redirect(url_for('admin_initiatives'))
        _bulk_score_job['running'] = True

    t = threading.Thread(target=_run_bulk_scoring, args=(app,), daemon=True)
    t.start()
    flash('Bulk scoring started in the background. Refresh periodically to see results.', 'info')
    return redirect(url_for('admin_initiatives'))


@app.route('/admin/bulk-score/status')
@login_required
def admin_bulk_score_status():
    if not current_user.is_admin:
        abort(403)
    with _bulk_score_lock:
        status = dict(_bulk_score_job)
    return jsonify(status)


# ===================== ADMIN MEMBERS =====================

@app.route('/admin/members')
@login_required
def admin_members():
    if not current_user.is_admin:
        abort(403)
    search_q = request.args.get('q', '').strip()
    query = User.query
    if search_q:
        query = query.filter(
            db.or_(
                User.name.ilike(f'%{search_q}%'),
                User.email.ilike(f'%{search_q}%'),
                User.organization.ilike(f'%{search_q}%'),
            )
        )
    users = query.order_by(User.created_at.desc()).all()
    return render_template('admin/members.html', users=users, search_q=search_q)


@app.route('/admin/member/<int:id>/toggle-admin', methods=['POST'])
@login_required
def toggle_admin(id):
    if not current_user.is_admin:
        abort(403)
    user = User.query.get_or_404(id)
    if user.id == current_user.id:
        flash("You can't remove your own admin status.", 'error')
        return redirect(url_for('admin_members'))
    user.is_admin = not user.is_admin
    db.session.commit()
    flash(f"{'Admin granted to' if user.is_admin else 'Admin removed from'} {user.name}.", 'success')
    return redirect(url_for('admin_members'))


@app.route('/admin/member/<int:id>/toggle-approved', methods=['POST'])
@login_required
def toggle_approved(id):
    if not current_user.is_admin:
        abort(403)
    user = User.query.get_or_404(id)
    user.is_approved = not user.is_approved
    db.session.commit()
    flash(f"{'Approved' if user.is_approved else 'Suspended'} {user.name}.", 'success')
    return redirect(url_for('admin_members'))


@app.route('/admin/member/<int:id>/delete', methods=['POST'])
@login_required
def admin_delete_member(id):
    if not current_user.is_admin:
        abort(403)
    user = User.query.get_or_404(id)
    if user.id == current_user.id:
        flash("You can't delete yourself.", 'error')
        return redirect(url_for('admin_members'))
    db.session.delete(user)
    db.session.commit()
    flash(f'Member {user.name} deleted.', 'success')
    return redirect(url_for('admin_members'))

# ===================== ADMIN PROJECTS =====================

@app.route('/admin/projects')
@login_required
def admin_projects():
    if not current_user.is_admin:
        abort(403)
    projects = Project.query.order_by(Project.created_at.desc()).all()
    return render_template('admin/projects.html', projects=projects)


@app.route('/admin/project/new', methods=['GET', 'POST'])
@login_required
def admin_new_project():
    if not current_user.is_admin:
        abort(403)
    if request.method == 'POST':
        deadline_str = request.form.get('deadline')
        start_date_str = request.form.get('start_date')
        deadline = datetime.strptime(deadline_str, '%Y-%m-%d') if deadline_str else None
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d') if start_date_str else None

        project = Project(
            title=request.form.get('title'),
            description=request.form.get('description'),
            deadline=deadline,
            start_date=start_date,
            is_published=True,
            submitted_by=current_user.id
        )
        db.session.add(project)
        db.session.flush()

        activity_titles = request.form.getlist('activity_title[]')
        activity_descs  = request.form.getlist('activity_description[]')
        activity_ddls   = request.form.getlist('activity_deadline[]')
        for t, d, dl in zip(activity_titles, activity_descs, activity_ddls):
            if t.strip():
                adl = datetime.strptime(dl, '%Y-%m-%d') if dl else None
                db.session.add(ProjectActivity(
                    project_id=project.id,
                    title=t.strip(),
                    description=d.strip(),
                    deadline=adl
                ))

        db.session.commit()

        try:
            subscribed_users = User.query.filter_by(is_approved=True, is_subscribed=True).all()
            project_url = url_for('project_detail', id=project.id, _external=True)
            send_project_notification(project, subscribed_users, project_url)
        except Exception as e:
            app.logger.error(f"Project notification email error: {e}")

        flash('Project created and published.', 'success')
        return redirect(url_for('admin_projects'))
    return render_template('admin/project_form.html', project=None)


@app.route('/admin/project/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def admin_edit_project(id):
    if not current_user.is_admin:
        abort(403)
    project = Project.query.get_or_404(id)
    if request.method == 'POST':
        deadline_str = request.form.get('deadline')
        start_date_str = request.form.get('start_date')
        project.title       = request.form.get('title')
        project.description = request.form.get('description')
        project.deadline    = datetime.strptime(deadline_str, '%Y-%m-%d') if deadline_str else project.deadline
        project.start_date  = datetime.strptime(start_date_str, '%Y-%m-%d') if start_date_str else None
        project.is_published = bool(request.form.get('is_published'))

        ProjectActivity.query.filter_by(project_id=id).delete()
        activity_titles = request.form.getlist('activity_title[]')
        activity_descs  = request.form.getlist('activity_description[]')
        activity_ddls   = request.form.getlist('activity_deadline[]')
        for t, d, dl in zip(activity_titles, activity_descs, activity_ddls):
            if t.strip():
                adl = datetime.strptime(dl, '%Y-%m-%d') if dl else None
                db.session.add(ProjectActivity(
                    project_id=id,
                    title=t.strip(),
                    description=d.strip(),
                    deadline=adl
                ))

        db.session.commit()
        flash('Project updated.', 'success')
        return redirect(url_for('admin_projects'))
    return render_template('admin/project_form.html', project=project)

# ===================== ADMIN EVENTS =====================

@app.route('/admin/events')
@login_required
def admin_events():
    if not current_user.is_admin:
        abort(403)
    events = Event.query.order_by(Event.start_date.desc()).all()
    return render_template('admin/events.html', events=events)


@app.route('/admin/event/new', methods=['GET', 'POST'])
@login_required
def admin_new_event():
    if not current_user.is_admin:
        abort(403)
    if request.method == 'POST':
        start_str = request.form.get('start_date')
        end_str   = request.form.get('end_date')
        start = datetime.strptime(start_str, '%Y-%m-%dT%H:%M') if start_str else None
        end   = datetime.strptime(end_str,   '%Y-%m-%dT%H:%M') if end_str   else None

        event = Event(
            title=request.form.get('title'),
            description=request.form.get('description'),
            start_date=start,
            end_date=end,
            created_by=current_user.id,
            is_published=False,
            submitted_by=current_user.id
        )
        db.session.add(event)
        db.session.flush()

        poll_titles = request.form.getlist('poll_title[]')
        poll_descs  = request.form.getlist('poll_description[]')
        poll_opts   = request.form.getlist('poll_options[]')
        for pt, pd, po in zip(poll_titles, poll_descs, poll_opts):
            if pt.strip():
                options = [o.strip() for o in po.split('\n') if o.strip()]
                db.session.add(Poll(
                    event_id=event.id,
                    title=pt.strip(),
                    description=pd.strip(),
                    options=options
                ))

        files = request.files.getlist('attachments[]')
        labels = request.form.getlist('attachment_labels[]')
        for f, lbl in zip(files, labels):
            if f and f.filename and allowed_attachment(f.filename):
                orig, stored = save_attachment(f)
                db.session.add(EventAttachment(
                    event_id=event.id,
                    filename=orig,
                    stored_name=stored,
                    label=lbl.strip() or orig
                ))

        db.session.commit()
        flash('Event created (pending approval).', 'success')
        return redirect(url_for('admin_events'))
    return render_template('admin/event_form.html', event=None)


@app.route('/admin/event/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def admin_edit_event(id):
    if not current_user.is_admin:
        abort(403)
    event = Event.query.get_or_404(id)
    if request.method == 'POST':
        start_str = request.form.get('start_date')
        end_str   = request.form.get('end_date')
        event.title       = request.form.get('title')
        event.description = request.form.get('description')
        event.start_date  = datetime.strptime(start_str, '%Y-%m-%dT%H:%M') if start_str else event.start_date
        event.end_date    = datetime.strptime(end_str,   '%Y-%m-%dT%H:%M') if end_str   else None
        event.is_published = bool(request.form.get('is_published'))

        for poll in event.polls.all():
            db.session.delete(poll)
        db.session.flush()

        poll_titles = request.form.getlist('poll_title[]')
        poll_descs  = request.form.getlist('poll_description[]')
        poll_opts   = request.form.getlist('poll_options[]')
        for pt, pd, po in zip(poll_titles, poll_descs, poll_opts):
            if pt.strip():
                options = [o.strip() for o in po.split('\n') if o.strip()]
                db.session.add(Poll(
                    event_id=event.id,
                    title=pt.strip(),
                    description=pd.strip(),
                    options=options
                ))

        files = request.files.getlist('attachments[]')
        labels = request.form.getlist('attachment_labels[]')
        for f, lbl in zip(files, labels):
            if f and f.filename and allowed_attachment(f.filename):
                orig, stored = save_attachment(f)
                db.session.add(EventAttachment(
                    event_id=event.id,
                    filename=orig,
                    stored_name=stored,
                    label=lbl.strip() or orig
                ))

        db.session.commit()

        if event.zoom_webinar_id:
            try:
                fetch_recording_url(event.zoom_webinar_id, event)
                db.session.commit()
            except Exception as e:
                app.logger.error(f"Zoom recording fetch error: {e}")

        flash('Event updated.', 'success')
        return redirect(url_for('admin_events'))
    return render_template('admin/event_form.html', event=event)


@app.route('/admin/event/<int:id>/delete', methods=['POST'])
@login_required
def admin_delete_event(id):
    if not current_user.is_admin:
        abort(403)
    event = Event.query.get_or_404(id)
    if event.zoom_webinar_id:
        try:
            delete_zoom_webinar(event.zoom_webinar_id)
        except Exception as e:
            app.logger.error(f"Zoom delete error: {e}")
    db.session.delete(event)
    db.session.commit()
    flash('Event deleted.', 'success')
    return redirect(url_for('admin_events'))


@app.route('/admin/event/attachment/<int:id>/delete', methods=['POST'])
@login_required
def admin_delete_attachment(id):
    if not current_user.is_admin:
        abort(403)
    att = EventAttachment.query.get_or_404(id)
    event_id = att.event_id
    stored = os.path.join(app.config['UPLOAD_FOLDER'], 'event_attachments', att.stored_name)
    if os.path.exists(stored):
        os.remove(stored)
    db.session.delete(att)
    db.session.commit()
    flash('Attachment deleted.', 'success')
    return redirect(url_for('admin_edit_event', id=event_id))


@app.route('/uploads/event_attachments/<filename>')
def serve_attachment(filename):
    folder = os.path.join(app.config['UPLOAD_FOLDER'], 'event_attachments')
    return send_from_directory(folder, filename)

# ===================== PUBLIC PROJECT ROUTES =====================

@app.route('/projects')
def projects():
    now = datetime.utcnow()
    active_projects = Project.query.filter(
        Project.is_published == True,
        Project.is_active == True,
        Project.deadline >= now
    ).order_by(Project.deadline).all()
    
    past_projects = Project.query.filter(
        Project.is_published == True,
        db.or_(Project.is_active == False, Project.deadline < now)
    ).order_by(Project.deadline.desc()).all()
    
    return render_template('projects.html',
                         active_projects=active_projects,
                         past_projects=past_projects,
                         now=now)


@app.route('/project/<int:id>')
def project_detail(id):
    project = Project.query.get_or_404(id)
    if not project.is_published and not (current_user.is_authenticated and current_user.is_admin):
        abort(404)
    
    user_participations = []
    if current_user.is_authenticated:
        user_participations = [
            p.activity_id for p in
            ProjectParticipation.query.filter_by(
                project_id=id, user_id=current_user.id
            ).all()
        ]
    
    return render_template('project_detail.html',
                         project=project,
                         user_participations=user_participations,
                         now=datetime.utcnow())


@app.route('/project/<int:id>/submit', methods=['GET', 'POST'])
@login_required
def submit_project(id=None):
    if request.method == 'POST' and id is None:
        deadline_str = request.form.get('deadline')
        start_date_str = request.form.get('start_date')
        deadline = datetime.strptime(deadline_str, '%Y-%m-%d') if deadline_str else None
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d') if start_date_str else None

        project = Project(
            title=request.form.get('title'),
            description=request.form.get('description'),
            deadline=deadline,
            start_date=start_date,
            is_published=False,
            submitted_by=current_user.id
        )
        db.session.add(project)
        db.session.flush()

        activity_titles = request.form.getlist('activity_title[]')
        activity_descs  = request.form.getlist('activity_description[]')
        activity_ddls   = request.form.getlist('activity_deadline[]')
        for t, d, dl in zip(activity_titles, activity_descs, activity_ddls):
            if t.strip():
                adl = datetime.strptime(dl, '%Y-%m-%d') if dl else None
                db.session.add(ProjectActivity(
                    project_id=project.id,
                    title=t.strip(),
                    description=d.strip(),
                    deadline=adl
                ))

        db.session.commit()

        try:
            admin = User.query.filter_by(is_admin=True).first()
            if admin:
                send_project_approved_email(admin.email, project)
        except Exception as e:
            app.logger.error(f"Project submission admin alert error: {e}")

        flash('Your project has been submitted for review.', 'success')
        return redirect(url_for('dashboard'))
    return render_template('project_form.html')


@app.route('/project/<int:id>/join', methods=['POST'])
@login_required
def join_project(id):
    project = Project.query.get_or_404(id)
    if not project.is_published:
        abort(404)
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

    try:
        send_project_signup_confirmation(current_user, project, signed_up_activities)
    except Exception as e:
        app.logger.error(f"Project signup confirmation email error: {e}")

    try:
        admin = User.query.filter_by(is_admin=True).first()
        if admin:
            send_project_signup_admin_alert(admin.email, current_user, project, signed_up_activities)
    except Exception as e:
        app.logger.error(f"Project signup admin alert email error: {e}")

    return redirect(url_for('dashboard'))

@app.route('/unsubscribe', methods=['GET', 'POST'])
def unsubscribe():
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
            if not BlockedEmail.query.filter_by(email=email).first():
                db.session.add(BlockedEmail(email=email))
                db.session.commit()
        return render_template('unsubscribe.html', confirmed=True, email=email, error=False)

    email = request.args.get('email', '').lower().strip()
    token = request.args.get('token', '')
    if not email or not token or not hmac.compare_digest(token, _make_token(email)):
        return render_template('unsubscribe.html', error=True, email='', confirmed=False)
    return render_template('unsubscribe.html', confirmed=False, error=False, email=email, token=token)


# ===================== IMPORT ROUTES =====================

@app.route('/admin/import/initiatives', methods=['GET', 'POST'])
@login_required
def admin_import_initiatives():
    if not current_user.is_admin:
        abort(403)
    if request.method == 'POST':
        file = request.files.get('csv_file')
        if not file or not file.filename.endswith('.csv'):
            flash('Please upload a valid CSV file.', 'error')
            return redirect(url_for('admin_import_initiatives'))
        
        stream = io.StringIO(file.stream.read().decode('UTF-8'))
        reader = csv.DictReader(stream)
        imported = 0
        errors = []
        
        for row in reader:
            try:
                title = row.get('title', '').strip()
                content = row.get('content', '').strip()
                org = row.get('organization', '').strip()
                email = row.get('email', '').lower().strip()
                
                if not title or not content or not email:
                    errors.append(f"Missing required fields in row: {row}")
                    continue
                
                user = User.query.filter_by(email=email).first()
                if not user:
                    errors.append(f"User not found: {email}")
                    continue
                
                slug = re.sub(r'[^\w]+', '-', title.lower()).strip('-')[:190]
                base_slug = slug
                counter = 1
                while Initiative.query.filter_by(slug=slug).first():
                    slug = f"{base_slug}-{counter}"
                    counter += 1
                
                initiative = Initiative(
                    title=title[:200],
                    slug=slug,
                    content=content,
                    short_description=row.get('short_description', '')[:300],
                    user_id=user.id,
                    stakeholder_type=user.stakeholder_type,
                    country=row.get('country', user.country),
                    is_published=True
                )
                db.session.add(initiative)
                db.session.commit()
                imported += 1
                
            except Exception as e:
                errors.append(f"Error processing row: {str(e)}")
        
        if imported:
            flash(f'Successfully imported {imported} initiatives.', 'success')
        if errors:
            for error in errors[:5]:
                flash(error, 'warning')
        
        return redirect(url_for('admin_import_initiatives'))
    
    return render_template('admin/import_initiatives.html')


@app.route('/admin/import/members', methods=['GET', 'POST'])
@login_required
def admin_import_members():
    if not current_user.is_admin:
        abort(403)
    if request.method == 'POST':
        file = request.files.get('csv_file')
        notify = request.form.get('notify_members') == 'on'
        
        if not file or not file.filename.endswith('.csv'):
            flash('Please upload a valid CSV file.', 'error')
            return redirect(url_for('admin_import_members'))
        
        stream = io.StringIO(file.stream.read().decode('UTF-8'))
        reader = csv.DictReader(stream)
        imported = 0
        skipped = 0
        errors = []
        
        for row in reader:
            try:
                email = row.get('email', '').lower().strip()
                name = row.get('name', '').strip()
                organization = row.get('organization', '').strip()
                
                if not email or not name or not organization:
                    errors.append(f"Missing required fields: {row}")
                    continue
                
                if User.query.filter_by(email=email).first():
                    skipped += 1
                    continue
                
                if BlockedEmail.query.filter_by(email=email).first():
                    skipped += 1
                    continue
                
                user = User(
                    email=email,
                    name=name,
                    organization=organization,
                    stakeholder_type=row.get('stakeholder_type', 'NGO / Civil Society'),
                    country=row.get('country', ''),
                    is_approved=True,
                    is_admin=False
                )
                db.session.add(user)
                db.session.commit()
                imported += 1
                
                if notify:
                    try:
                        send_import_welcome_email(user)
                    except Exception as e:
                        app.logger.error(f"Import welcome email error for {email}: {e}")
                
            except Exception as e:
                errors.append(f"Error processing row: {str(e)}")
        
        flash(f'Import complete: {imported} added, {skipped} skipped.', 'success')
        if errors:
            for error in errors[:5]:
                flash(error, 'warning')
        
        return redirect(url_for('admin_import_members'))
    
    return render_template('admin/import_members.html')


# ===================== CUSTOM EMAIL =====================

@app.route('/admin/email/custom', methods=['GET', 'POST'])
@login_required
def admin_custom_email():
    if not current_user.is_admin:
        abort(403)
    if request.method == 'POST':
        subject = request.form.get('subject', '').strip()
        body_html = request.form.get('body_html', '').strip()
        target = request.form.get('target', 'all')
        
        if not subject or not body_html:
            flash('Subject and body are required.', 'error')
            return redirect(url_for('admin_custom_email'))
        
        if target == 'all':
            recipients = User.query.filter_by(is_approved=True, is_subscribed=True).all()
        elif target == 'admins':
            recipients = User.query.filter_by(is_admin=True).all()
        else:
            recipients = User.query.filter_by(is_approved=True, is_subscribed=True).all()
        
        try:
            send_custom_bulk_email(recipients, subject, body_html)
            flash(f'Email sent to {len(recipients)} recipient(s).', 'success')
        except Exception as e:
            app.logger.error(f"Custom bulk email error: {e}")
            flash('Failed to send email. Check logs.', 'error')
        
        return redirect(url_for('admin_custom_email'))
    
    return render_template('admin/custom_email.html')


# ===================== INVITE =====================

@app.route('/admin/invite', methods=['GET', 'POST'])
@login_required
def admin_invite():
    if not current_user.is_admin:
        abort(403)
    if request.method == 'POST':
        emails_raw = request.form.get('emails', '')
        emails = [e.strip().lower() for e in emails_raw.replace(',', '\n').split('\n') if e.strip()]
        
        sent = 0
        for email in emails:
            try:
                register_url = url_for('register', _external=True)
                send_invitation_email(email, register_url)
                sent += 1
            except Exception as e:
                app.logger.error(f"Invite email error for {email}: {e}")
        
        flash(f'Invitations sent to {sent} address(es).', 'success')
        return redirect(url_for('admin_invite'))
    
    return render_template('admin/invite.html')


# ===================== API ROUTES =====================

@app.route('/health')
def health_check():
    return {"status": "ok", "message": "Application is running"}, 200

@app.route('/api/translate', methods=['POST'])
def api_translate():
    data = request.get_json()
    text = data.get('text', '')
    target_lang = data.get('lang', 'fr')
    try:
        translated = translate_text(text, target_lang)
        return jsonify({'success': True, 'translation': translated})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/stats')
def api_stats():
    stats = {
        'members': User.query.filter_by(is_approved=True).count(),
        'initiatives': Initiative.query.filter_by(is_published=True).count(),
        'organizations': db.session.query(User.organization).distinct().count()
    }
    return jsonify(stats)


@app.route('/api/organisations')
def api_organisations():
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
    if not text:
        return ''
    
    md = mistune.create_markdown()
    html = md(text)
    
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
    
    cleaned_html = bleach.clean(html, tags=allowed_tags, attributes=allowed_attrs, strip=False)
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
