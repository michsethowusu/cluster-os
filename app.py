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
import mistune                     # MARKDOWN CHANGE
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
            
        # Admin uses password login
        if user.is_admin:
            from werkzeug.security import check_password_hash
            password = request.form.get('password')
            if not password:
                return render_template('login.html', show_password=True, email=email)
            if not user.password_hash or not check_password_hash(user.password_hash, password):
                flash('Invalid password.', 'error')
                return redirect(url_for('login'))
            
            # Reset OTP fields just in case
            user.otp = None
            user.otp_expiry = None
            db.session.commit()
            
            login_user(user)
            flash('Welcome back!', 'success')
            return redirect(url_for('admin_dashboard'))
        
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

        # New registrations always go through approval — initiative gives the admin
        # something meaningful to review before granting access.
        user = User(
            email=email,
            name=request.form.get('name'),
            organization=request.form.get('organization'),
            stakeholder_type=request.form.get('stakeholder_type'),
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
            short_description=initiative_short_desc[:300] if initiative_short_desc else None,
            user_id=user.id,
            stakeholder_type=user.stakeholder_type,
            country=user.country,
            is_published=False   # Published only when the user is approved
        )
        db.session.add(initiative)
        db.session.commit()

        # Extract and vet tags in a background thread so the worker is never blocked
        def _process_tags_async(flask_app, initiative_id, content):
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

        t = threading.Thread(
            target=_process_tags_async,
            args=(app, initiative.id, initiative_content),
            daemon=True
        )
        t.start()

        flash(
            'Thank you for registering! Your application is under review. '
            'You will receive an email once your account is approved.',
            'success'
        )
        return redirect(url_for('index'))
    
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
    )

@app.route('/initiative/new', methods=['GET', 'POST'])
@login_required
def new_initiative():
    if request.method == 'POST':
        title = request.form.get('title')
        short_description = request.form.get('short_description')
        content = request.form.get('content')                     # MARKDOWN CHANGE: raw content
        
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
            is_published=False
        )
        
        db.session.add(initiative)
        db.session.commit()

        # Score content quality in background (used to filter digest notifications)
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
                except Exception as e:
                    flask_app.logger.error(f"Quality scoring error (initiative {initiative_id}): {e}")

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
        
        flash('Initiative submitted for approval.', 'success')
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
    db.session.commit()
    
    # Delete related noun phrases
    NounPhrase.query.filter_by(initiative_id=id).delete()
    
    # Delete the initiative
    db.session.delete(initiative)
    db.session.commit()
    
    flash(f'Initiative "{title}" has been deleted.', 'success')
    return redirect(url_for('admin_approvals', type='initiatives'))

@app.route('/initiative/<slug>')
def view_initiative(slug):
    initiative = Initiative.query.filter_by(slug=slug, is_published=True).first_or_404()
    # Increment view count, skip for the initiative's own author to avoid self-inflation
    if not current_user.is_authenticated or current_user.id != initiative.user_id:
        initiative.view_count = (initiative.view_count or 0) + 1
        db.session.commit()
    return render_template('article.html', initiative=initiative)
    
@app.route('/admin/initiatives')
@login_required
def admin_initiatives():
    if not current_user.is_admin:
        abort(403)
    
    filter_type = request.args.get('filter', 'all')
    
    query = Initiative.query
    
    if filter_type == 'published':
        query = query.filter_by(is_published=True)
    elif filter_type == 'pending':
        query = query.filter_by(is_published=False)
    
    initiatives = query.order_by(Initiative.created_at.desc()).all()
    
    return render_template('admin/initiatives.html', 
                         initiatives=initiatives, 
                         current_filter=filter_type)

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
    orgs = db.session.query(
        User.organization,
        User.stakeholder_type,
        db.func.count(User.id).label('member_count')
    ).filter_by(is_approved=True).group_by(User.organization, User.stakeholder_type).all()
    return render_template('members.html', organizations=orgs)

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
                # Zoom's email IS the confirmation — no duplicate platform email needed.
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

@app.route('/event/attachment/<int:att_id>')
def download_attachment(att_id):
    """Serve an event attachment file. Login required."""
    if not current_user.is_authenticated:
        flash('Please log in to download attachments.', 'error')
        return redirect(url_for('login'))
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
    return render_template('admin/dashboard.html',
                         pending_users=pending_users,
                         pending_initiatives=pending_initiatives,
                         pending_questions=pending_questions,
                         pending_projects=pending_projects,
                         pending_events=pending_events)

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
    else:
        users = User.query.filter_by(is_approved=False).all()
        initiatives = Initiative.query.filter_by(is_published=False).all()
        questions = Question.query.filter_by(is_published=False).all()
        projects = Project.query.filter_by(is_published=False).all()
        events = Event.query.filter_by(is_published=False).all()
        items = list(users) + list(initiatives) + list(questions) + list(projects) + list(events)
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

        # Send digest notification only if quality score >= 4 (or not yet scored)
        if item.quality_score is None or item.quality_score >= 4:
            try:
                initiative_url = url_for('view_initiative', slug=item.slug, _external=True)
                subscribed_users = User.query.filter_by(is_approved=True, is_subscribed=True).all()
                send_bulk_initiatives_digest([{
                    'title': item.title,
                    'short_description': item.short_description or '',
                    'url': initiative_url,
                }], subscribed_users)
            except Exception as e:
                app.logger.error(f"Initiative approval digest email error: {e}")

        flash('Initiative published.', 'success')

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

    # ── 1. Approve all pending users ──────────────────────────────────────────
    pending_users = User.query.filter_by(is_approved=False).all()
    approved_user_count = 0
    newly_published = []  # collect ALL initiatives for the digest (users + members)
    for user in pending_users:
        user.is_approved = True
        approved_user_count += 1

        # Publish any initiative this user submitted that is still pending
        reg_initiative = Initiative.query.filter_by(user_id=user.id, is_published=False).first()
        if reg_initiative:
            reg_initiative.is_published = True
            try:
                phrases = extract_noun_phrases(reg_initiative.content)
                update_noun_phrase_db(reg_initiative.id, phrases)
            except Exception as e:
                app.logger.error(f"Noun phrase error on bulk approve (user {user.id}): {e}")
            award_points(user, 'initiative_published', commit=False)

            # Add this new-user initiative to the digest list only if quality score >= 4
            # (score None = not yet scored, include by default to avoid missing content)
            if reg_initiative.quality_score is None or reg_initiative.quality_score >= 4:
                initiative_url = url_for('view_initiative', slug=reg_initiative.slug, _external=True)
                newly_published.append({
                    'title': reg_initiative.title,
                    'short_description': reg_initiative.short_description or '',
                    'url': initiative_url,
                })

        # Individual welcome email to the newly approved member
        try:
            send_approval_email(user.email, reg_initiative.slug if reg_initiative else None)
        except Exception as e:
            app.logger.error(f"Bulk approve – welcome email error for {user.email}: {e}")

    db.session.commit()

    # ── 2. Approve all remaining pending initiatives ───────────────────────────
    # (New-user initiatives were already published in step 1 and added to
    #  newly_published above; this step only catches standalone member initiatives.)
    pending_initiatives = Initiative.query.filter_by(is_published=False).all()

    for initiative in pending_initiatives:
        initiative.is_published = True
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

        # Collect metadata for the digest — only quality score >= 4 (None = unscored, include)
        if initiative.quality_score is None or initiative.quality_score >= 4:
            initiative_url = url_for('view_initiative', slug=initiative.slug, _external=True)
            newly_published.append({
                'title': initiative.title,
                'short_description': initiative.short_description or '',
                'url': initiative_url,
            })

    db.session.commit()
    approved_initiative_count = len(newly_published)

    # ── 3. Send ONE digest email to all members for initiatives ───────────────
    if newly_published:
        try:
            all_approved_users = User.query.filter_by(is_approved=True, is_subscribed=True).all()
            send_bulk_initiatives_digest(newly_published, all_approved_users)
        except Exception as e:
            app.logger.error(f"Bulk approve – digest email error: {e}")

    # ── 4. Flash summary ──────────────────────────────────────────────────────
    parts = []
    if approved_user_count:
        parts.append(f"{approved_user_count} user{'s' if approved_user_count != 1 else ''} approved")
    if approved_initiative_count:
        parts.append(
            f"{approved_initiative_count} "
            f"initiative{'s' if approved_initiative_count != 1 else ''} published"
        )
    if parts:
        flash("Approved: " + " and ".join(parts) + ". Members notified.", 'success')
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
    db.session.commit()
    flash('Item unpublished.', 'success')
    return redirect(url_for('admin_approvals'))

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
                        try:
                            send_custom_bulk_email(email, name, custom_subject, custom_body)
                            invited += 1
                        except Exception as e:
                            app.logger.error(f"Custom message email error for {email}: {e}")
                            errors.append(f"Row {row_num}: Failed to send message to {email}")
                        continue

                    # ── INVITE-ONLY MODE ──────────────────────────────────────
                    if invite_only:
                        # No skip for existing members — send to everyone
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
                    # Always send welcome email to imported members
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
    """Token-based unsubscribe page. Token = hex(email) for simplicity."""
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
        return render_template('unsubscribe.html', confirmed=True, email=email, error=False)

    email = request.args.get('email', '').lower().strip()
    token = request.args.get('token', '')
    if not email or not token or not hmac.compare_digest(token, _make_token(email)):
        return render_template('unsubscribe.html', error=True, email='', confirmed=False)
    return render_template('unsubscribe.html', confirmed=False, error=False, email=email, token=token)


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
