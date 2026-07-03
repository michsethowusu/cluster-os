import os
import requests
from flask import current_app

# Shared variable descriptions for the admin UI
TEMPLATE_VARIABLE_DESCRIPTIONS = {
    'site_name': 'The configured site name (from Appearance).',
    'contact_email': 'The sender email address (from Settings).',
    'login_url': 'Absolute URL to the login page.',
    'register_url': 'Absolute URL to the registration page.',
    'user_name': "Recipient's full name.",
    'user_email': "Recipient's email address.",
    'organization': "Recipient's organization name.",
    'otp': 'The one-time password (login code).',
    'initiative_title': 'Title of the initiative.',
    'initiative_url': 'Absolute URL to the initiative page.',
    'initiative_link': 'Pre-built initiative link block (empty if no initiative).',
    'event_title': 'Title of the event.',
    'event_url': 'Absolute URL to the event page.',
    'event_date': 'Formatted event date and time.',
    'project_title': 'Title of the project.',
    'project_url': 'Absolute URL to the project page.',
    'project_deadline': 'Formatted project deadline date.',
    'cert_url': 'Absolute URL to the certificate page.',
    'policy_title': 'Title of the policy development.',
    'policy_url': 'Absolute URL to the policy page.',
    'doc_title': 'Title of the document.',
    'doc_url': 'Absolute URL to the document page.',
    'ta_title': 'Title of the Technical Assistance Need.',
    'ta_url': 'Absolute URL to submit/view a Technical Assistance Need.',
    'admin_url': 'Absolute URL to manage in the admin panel.',
    'activity_items': 'HTML list of activities the user signed up for.',
    'body_content': 'The custom message body (for bulk emails).',
    'items_html': 'HTML content listing multiple items (for digest emails).',
    'subject_line': 'Generated subject line for digest emails.',
    'title_line': 'Generated title for digest emails.',
    'digest_items': 'HTML list items for digest emails.',
}

# These are filled in automatically for EVERY email — the admin never needs to
# supply a value. They are the only placeholders that mean the same thing
# everywhere on the site.
SITE_WIDE_VARIABLES = ['site_name', 'contact_email', 'login_url', 'register_url']

# Email colour palette — kept in step with the site branding (base.html
# --au-green navbar) but restrained for email: headings and body text stay
# black/grey, and colour is reserved for interactive elements (buttons/links).
EMAIL_BRAND = '#007451'        # site navbar green — buttons & links only
EMAIL_HEADING = '#1a1a1a'      # near-black — email heading
EMAIL_TEXT = '#333333'         # body text
EMAIL_PANEL_BG = '#f8f9fa'     # light panel background
EMAIL_BORDER = '#dee2e6'       # neutral grey — panel borders, dividers

# Semantic style classes. Templates are written with readable markup like
#   <p class="cta"><a class="button" href="{{ url }}">Label</a></p>
# and these get expanded to full inline styles at send time (email clients need
# inline CSS). This keeps the editable template bodies clean prose instead of
# walls of inline style="..." attributes.
EMAIL_STYLE_CLASSES = {
    'button': f'display:inline-block;background:{EMAIL_BRAND};color:#ffffff;padding:13px 32px;text-decoration:none;border-radius:5px;font-weight:bold;font-size:0.95em;',
    'cta': 'text-align:center;margin:28px 0;',
    'panel': f'background:{EMAIL_PANEL_BG};border-left:4px solid {EMAIL_BORDER};border-radius:4px;padding:16px 20px;margin:20px 0;',
    'code': 'background:#f4f4f4;padding:20px;text-align:center;font-size:26px;font-weight:bold;letter-spacing:6px;margin:20px 0;border-radius:4px;',
    'muted': 'color:#666;font-size:0.9em;',
}

# Short, admin-facing explanation of each style class, shown in the editor.
EMAIL_STYLE_CLASS_DESCRIPTIONS = {
    'button': 'A blue call-to-action button (put on a link inside a "cta" paragraph).',
    'cta': 'Centres its contents — use for the paragraph that holds a button.',
    'panel': 'A light box with a blue left border — for highlighting event/project details.',
    'code': 'A large, spaced-out box — used to display the login code.',
    'muted': 'Small, grey secondary text — for footnotes and fine print.',
}


def _expand_styles(html):
    """Expand semantic class="..." markers into inline styles for email."""
    for cls, style in EMAIL_STYLE_CLASSES.items():
        html = html.replace(f'class="{cls}"', f'style="{style}"')
        html = html.replace(f"class='{cls}'", f'style="{style}"')
    return html


EMAIL_TEMPLATES = [
    {
        'key': 'otp',
        'label': 'Login OTP',
        'description': 'Sent to users with their one-time login code.',
        'variables': ['otp', 'site_name'],
        'subject': 'Your Login OTP - {{ site_name }}',
        'title': 'Your Login Code',
        'body_html': '<p>Your one-time password (OTP) for login is:</p>\n<div class="code">{{ otp }}</div>\n<p class="muted">This code expires in 10 minutes. If you didn\'t request this, please ignore this email.</p>',
    },
    {
        'key': 'approval',
        'label': 'Registration Approved',
        'description': 'Sent to users when their registration is approved.',
        'variables': ['site_name', 'initiative_link', 'login_url'],
        'subject': 'Welcome to {{ site_name }}',
        'title': 'Welcome to {{ site_name }}',
        'body_html': '<p>Your registration has been approved! You can now log in to submit initiatives and participate in discussions.</p>\n{{ initiative_link }}\n<p class="cta"><a class="button" href="{{ login_url }}">Log In to the Platform</a></p>',
    },
    {
        'key': 'initiative_approved',
        'label': 'Initiative Approved',
        'description': 'Sent when a user\'s initiative is approved and published.',
        'variables': ['user_name', 'initiative_title', 'initiative_url', 'site_name'],
        'subject': 'Your initiative has been published - {{ site_name }}',
        'title': 'Your Initiative Has Been Published',
        'body_html': '<p>Dear {{ user_name }},</p>\n<p>Your initiative <strong>{{ initiative_title }}</strong> has been reviewed and is now live on {{ site_name }}.</p>\n<p class="cta"><a class="button" href="{{ initiative_url }}">View Your Initiative</a></p>',
    },
    {
        'key': 'certificate',
        'label': 'Contributor Certificate',
        'description': 'Sent to contributors when their certificate is ready.',
        'variables': ['user_name', 'site_name', 'cert_url'],
        'subject': 'Your contributor certificate is ready - {{ site_name }}',
        'title': 'Your Contributor Certificate',
        'body_html': '<p>Dear {{ user_name }},</p>\n<p>Thank you for contributing to <strong>{{ site_name }}</strong>! In recognition of your published contribution, we have created a personal contributor certificate in your name.</p>\n<p>You can view, print, or share it any time:</p>\n<p class="cta"><a class="button" href="{{ cert_url }}">View Your Certificate</a></p>\n<p class="muted">This is a live page \u2014 it stays up to date as you contribute more.</p>',
    },
    {
        'key': 'initiative_pending',
        'label': 'Initiative Pending Review',
        'description': 'Sent when an initiative is submitted for review.',
        'variables': ['user_name', 'initiative_title', 'site_name', 'login_url'],
        'subject': 'Your initiative has been submitted for review - {{ site_name }}',
        'title': 'Initiative Submitted for Review',
        'body_html': '<p>Dear {{ user_name }},</p>\n<p>Your initiative <strong>{{ initiative_title }}</strong> has been submitted to {{ site_name }} and is pending review by our team.</p>\n<p>You will receive another email once it has been approved and published.</p>\n<p class="cta"><a class="button" href="{{ login_url }}">Log In to the Platform</a></p>',
    },
    {
        'key': 'import_welcome',
        'label': 'Welcome (Imported Member)',
        'description': 'Sent to members who were added by admin.',
        'variables': ['user_name', 'organization', 'site_name', 'login_url', 'user_email'],
        'subject': "You've been added to the {{ site_name }}",
        'title': 'Welcome to the {{ site_name }}',
        'body_html': '<p>Dear {{ user_name }},</p>\n<p>You have been added to the <strong>{{ site_name }}</strong> as a member representing <strong>{{ organization }}</strong>.</p>\n<p>A platform connecting experts and organisations. As a member you can:</p>\n<ul>\n    <li>Share and explore initiatives from across the continent</li>\n    <li>Participate in the Q&amp;A forum and contribute recommendations</li>\n    <li>Register for events and complete polls</li>\n    <li>Connect with other experts in the network</li>\n</ul>\n<p><strong>To get started, please log in and complete your profile</strong> by adding descriptions of your ongoing projects or areas of expertise. This helps other members find and connect with you based on your areas of expertise.</p>\n<p class="cta"><a class="button" href="{{ login_url }}">Log In to the Platform</a></p>\n<p class="muted">Your registered email address is: {{ user_email }}<br>\nUse this to log in \u2014 you will receive a one-time password (OTP) each time you sign in.</p>',
    },
    {
        'key': 'invitation_org',
        'label': 'Invitation (Organisation)',
        'description': 'Sent to invite organisation representatives.',
        'variables': ['user_name', 'organization', 'site_name', 'register_url', 'contact_email'],
        'subject': 'Invitation to Join {{ site_name }}',
        'title': 'Invitation to Join {{ site_name }}',
        'body_html': '<p>Dear {{ user_name }},</p>\n<p><strong>{{ site_name }}</strong> is pleased to invite experts on behalf of <strong>{{ organization }}</strong> to join its digital collaboration platform.</p>\n<p>A platform connecting experts and organisations. As a member your organisation will be able to:</p>\n<ul>\n    <li>Share and explore initiatives from across the continent</li>\n    <li>Participate in the Q&amp;A forum and contribute recommendations</li>\n    <li>Register for events and engage in polls</li>\n    <li>Connect with other experts and organisations in the network</li>\n</ul>\n<p>We look forward to your participation.</p>\n<p class="cta"><a class="button" href="{{ register_url }}">Register Now</a></p>\n<p class="muted">Should you have any questions, please do not hesitate to contact us at <a href="mailto:{{ contact_email }}">{{ contact_email }}</a>.</p>',
    },
    {
        'key': 'invitation_individual',
        'label': 'Invitation (Individual)',
        'description': 'Sent to invite individuals joining in their own capacity.',
        'variables': ['user_name', 'site_name', 'register_url', 'contact_email'],
        'subject': 'Invitation to Join the {{ site_name }}',
        'title': 'Invitation to Join the {{ site_name }}',
        'body_html': '<p>Dear {{ user_name }},</p>\n<p>We are pleased to invite you to join <strong>{{ site_name }}</strong>.</p>\n<p>A platform connecting experts and organisations. As a member you will be able to:</p>\n<ul>\n    <li>Share and explore initiatives from across the continent</li>\n    <li>Participate in the Q&amp;A forum and contribute recommendations</li>\n    <li>Register for events and engage in polls</li>\n    <li>Connect with other experts and organisations in the network</li>\n</ul>\n<p>We look forward to your participation.</p>\n<p class="cta"><a class="button" href="{{ register_url }}">Register Now</a></p>\n<p class="muted">Should you have any questions, please do not hesitate to contact us at <a href="mailto:{{ contact_email }}">{{ contact_email }}</a>.</p>',
    },
    {
        'key': 'event_invitation',
        'label': 'Event Invitation',
        'description': 'Sent to invite someone to a specific event.',
        'variables': ['user_name', 'event_title', 'event_date', 'event_url', 'site_name'],
        'subject': 'Invitation to {{ event_title }}',
        'title': "You're Invited: {{ event_title }}",
        'body_html': '<p>Dear {{ user_name }},</p>\n<p>You have been invited to attend the following event on {{ site_name }}:</p>\n<div class="panel">\n  <h3>{{ event_title }}</h3>\n  <p><strong>Date:</strong> {{ event_date }}</p>\n  <p class="muted">Join us for this event where we will explore new ideas and share best practices with experts from across the continent.</p>\n</div>\n<p class="cta"><a class="button" href="{{ event_url }}">View Event &amp; Register</a></p>',
    },
    {
        'key': 'project_signup',
        'label': 'Project Signup Confirmation',
        'description': 'Sent to confirm a member has joined a project.',
        'variables': ['user_name', 'project_title', 'project_deadline', 'activity_items', 'project_url', 'site_name'],
        'subject': "You've joined: {{ project_title }} \u2013 {{ site_name }}",
        'title': "You've Joined a Project",
        'body_html': '<p>Dear {{ user_name }},</p>\n<p>You have successfully signed up to participate in the following project:</p>\n<div class="panel">\n  <h3>{{ project_title }}</h3>\n  <p><strong>Deadline:</strong> {{ project_deadline }}</p>\n  <p><strong>Activities you signed up for:</strong></p>\n  <ul>{{ activity_items }}</ul>\n</div>\n<p class="cta"><a class="button" href="{{ project_url }}">View Project</a></p>',
    },
    {
        'key': 'project_signup_admin',
        'label': 'Project Signup (Admin Alert)',
        'description': 'Alert admin when a member signs up for a project.',
        'variables': ['user_name', 'user_email', 'organization', 'project_title', 'project_deadline', 'activity_items', 'admin_url', 'site_name'],
        'subject': '[New Sign-up] {{ user_name }} joined "{{ project_title }}"',
        'title': 'New Project Sign-up',
        'body_html': '<p>A member has just signed up for a project on {{ site_name }}.</p>\n<div class="panel">\n  <p><strong>Member:</strong> {{ user_name }} ({{ user_email }})</p>\n  <p><strong>Organisation:</strong> {{ organization }}</p>\n  <p><strong>Project:</strong> {{ project_title }}</p>\n  <p><strong>Deadline:</strong> {{ project_deadline }}</p>\n</div>\n<p><strong>Activities selected:</strong></p>\n<ul>{{ activity_items }}</ul>\n<p class="cta"><a class="button" href="{{ admin_url }}">Manage Project in Admin</a></p>',
    },
    {
        'key': 'project_notification',
        'label': 'New Project Notification',
        'description': 'Notify all members about a new project.',
        'variables': ['project_title', 'project_deadline', 'project_url', 'site_name'],
        'subject': 'New Project: {{ project_title }}',
        'title': 'New Project on the Platform',
        'body_html': '<p>A new collaborative project has been published on {{ site_name }}:</p>\n<div class="panel">\n  <h3>{{ project_title }}</h3>\n  <p class="muted">A collaborative project bringing together experts to address key challenges and share knowledge.</p>\n  <p><strong>Deadline:</strong> {{ project_deadline }}</p>\n</div>\n<p class="cta"><a class="button" href="{{ project_url }}">View Project &amp; Join</a></p>',
    },
    {
        'key': 'project_approved',
        'label': 'Project Approved',
        'description': 'Sent when a user\'s project is approved and published.',
        'variables': ['user_name', 'project_title', 'project_url', 'site_name'],
        'subject': 'Your project has been published \u2013 {{ site_name }}',
        'title': 'Your Project Has Been Published',
        'body_html': '<p>Dear {{ user_name }},</p>\n<p>Your project <strong>{{ project_title }}</strong> has been reviewed and is now live on {{ site_name }}. Members can now view it and sign up to participate.</p>\n<p class="cta"><a class="button" href="{{ project_url }}">View Your Project</a></p>',
    },
    {
        'key': 'event_approved',
        'label': 'Event Approved',
        'description': 'Sent when a user\'s event is approved and published.',
        'variables': ['user_name', 'event_title', 'event_date', 'event_url', 'site_name'],
        'subject': 'Your event has been published \u2013 {{ site_name }}',
        'title': 'Your Event Has Been Published',
        'body_html': '<p>Dear {{ user_name }},</p>\n<p>Your event <strong>{{ event_title }}</strong> has been reviewed and is now live on {{ site_name }}. All members have been notified and can register.</p>\n<p><strong>Date:</strong> {{ event_date }}</p>\n<p class="cta"><a class="button" href="{{ event_url }}">View Your Event</a></p>',
    },
    {
        'key': 'event_notification',
        'label': 'New Event Notification',
        'description': 'Notify all subscribed members about a new event.',
        'variables': ['event_title', 'event_date', 'event_url', 'site_name'],
        'subject': 'New Event: {{ event_title }}',
        'title': 'New Event: {{ event_title }}',
        'body_html': '<p>A new event has been published on {{ site_name }}:</p>\n<div class="panel">\n  <h3>{{ event_title }}</h3>\n  <p><strong>Date:</strong> {{ event_date }}</p>\n  <p class="muted">Join us for this event where we will explore new ideas and share best practices with experts from across the continent.</p>\n</div>\n<p class="cta"><a class="button" href="{{ event_url }}">Register Now</a></p>',
    },
    {
        'key': 'event_registration',
        'label': 'Event Registration Confirmation',
        'description': 'Sent to confirm event registration.',
        'variables': ['user_name', 'event_title', 'event_date', 'meeting_link_html', 'event_url', 'site_name'],
        'subject': 'Registration Confirmed: {{ event_title }} \u2013 {{ site_name }}',
        'title': 'Event Registration Confirmed',
        'body_html': '<p>Dear {{ user_name }},</p>\n<p>You have successfully registered for the following event:</p>\n<div class="panel">\n  <h3>{{ event_title }}</h3>\n  <p><strong>Date:</strong> {{ event_date }}</p>\n  {{ meeting_link_html }}\n</div>\n<p class="cta"><a class="button" href="{{ event_url }}">View Event Details</a></p>\n<p class="muted">We\'ll send you a reminder closer to the date.</p>',
    },
    {
        'key': 'custom_bulk',
        'label': 'Custom Bulk Email',
        'description': 'Custom notification emails sent by admin.',
        'variables': ['user_name', 'body_content', 'site_name'],
        'subject': '{{ site_name }}',
        'title': '{{ site_name }}',
        'body_html': '<p>Dear {{ user_name }},</p>\n{{ body_content }}',
    },
    {
        'key': 'initiative_single',
        'label': 'Single Initiative Notification',
        'description': 'Notify all subscribed members about a new initiative.',
        'variables': ['initiative_title', 'initiative_url', 'site_name'],
        'subject': '{{ initiative_title }}',
        'title': 'New Initiative Published',
        'body_html': '<p>A new initiative has just been published on the platform:</p>\n<div class="panel">\n  <p><strong>{{ initiative_title }}</strong></p>\n  <p class="muted">Learn more about this initiative and how you can contribute to its success.</p>\n</div>\n<p class="cta"><a class="button" href="{{ initiative_url }}">Read Initiative \u2192</a></p>',
    },
    {
        'key': 'initiative_digest',
        'label': 'Initiatives Digest',
        'description': 'Digest of multiple newly published initiatives.',
        'variables': ['subject_line', 'title_line', 'notification_intro', 'items_html', 'site_name'],
        'subject': '{{ subject_line }}',
        'title': '{{ title_line }}',
        'body_html': '<p>{{ notification_intro }}</p>\n{{ items_html }}\n<p class="muted">You are receiving this because you are a member of {{ site_name }}.</p>',
    },
    {
        'key': 'policy_single',
        'label': 'Single Policy Notification',
        'description': 'Notify all subscribed members about a new policy development.',
        'variables': ['policy_title', 'policy_url', 'site_name'],
        'subject': '{{ policy_title }}',
        'title': 'New Policy Development Published',
        'body_html': '<p>A new policy development has just been published on the platform:</p>\n<p class="cta"><a class="button" href="{{ policy_url }}">Read Full Policy Development \u2192</a></p>',
    },
    {
        'key': 'policy_digest',
        'label': 'Policies Digest',
        'description': 'Digest of multiple newly published policy developments.',
        'variables': ['subject_line', 'title_line', 'notification_intro', 'items_html', 'site_name'],
        'subject': '{{ subject_line }}',
        'title': '{{ title_line }}',
        'body_html': '<p>{{ notification_intro }}</p>\n{{ items_html }}\n<p class="muted">You are receiving this because you are a member of {{ site_name }}.</p>',
    },
    {
        'key': 'document_single',
        'label': 'Single Document Notification',
        'description': 'Notify all subscribed members about a new document.',
        'variables': ['doc_title', 'doc_url', 'site_name'],
        'subject': '{{ doc_title }}',
        'title': 'New Document Published',
        'body_html': '<p>A new document has just been published on the platform:</p>\n<p class="cta"><a class="button" href="{{ doc_url }}">View Document \u2192</a></p>',
    },
    {
        'key': 'document_digest',
        'label': 'Documents Digest',
        'description': 'Digest of multiple newly published documents.',
        'variables': ['subject_line', 'title_line', 'notification_intro', 'items_html', 'site_name'],
        'subject': '{{ subject_line }}',
        'title': '{{ title_line }}',
        'body_html': '<p>{{ notification_intro }}</p>\n{{ items_html }}\n<p class="muted">You are receiving this because you are a member of {{ site_name }}.</p>',
    },
    {
        'key': 'ta_single',
        'label': 'Single TA Need Notification',
        'description': 'Notify all subscribed members about a new Technical Assistance Need.',
        'variables': ['ta_title', 'ta_url', 'site_name'],
        'subject': 'New Technical Assistance Need: {{ ta_title }}',
        'title': 'New Technical Assistance Need Published',
        'body_html': '<p>A Member State has published a new technical assistance need on the platform:</p>\n<p class="cta"><a class="button" href="{{ ta_url }}">View Technical Assistance Need \u2192</a></p>',
    },
    {
        'key': 'ta_digest',
        'label': 'TA Needs Digest',
        'description': 'Digest of multiple Technical Assistance Needs.',
        'variables': ['subject_line', 'title_line', 'notification_intro', 'items_html', 'site_name'],
        'subject': '{{ subject_line }}',
        'title': '{{ title_line }}',
        'body_html': '<p>{{ notification_intro }}</p>\n{{ items_html }}\n<p class="muted">You are receiving this because you are a member of {{ site_name }}.</p>',
    },
    {
        'key': 'ta_invitation',
        'label': 'TA Need Invitation',
        'description': 'Invite a Member State stakeholder to submit their Technical Assistance Need.',
        'variables': ['user_name', 'site_name', 'ta_url'],
        'subject': 'Submit Your Technical Assistance Need \u2013 {{ site_name }}',
        'title': 'Submit Your Technical Assistance Need',
        'body_html': '<p>Dear {{ user_name }},</p>\n<p>As a <strong>Member State</strong> stakeholder on {{ site_name }}, you are invited to submit your <strong>Technical Assistance Need</strong>.</p>\n<p>Member States can describe the specific technical assistance they require. This helps partners and development organisations identify where they can provide support.</p>\n<p class="cta"><a class="button" href="{{ ta_url }}">Submit Your Technical Assistance Need</a></p>\n<p class="muted">You are receiving this as a Member State stakeholder on {{ site_name }}.</p>',
    },
]


# ===================== HELPERS =====================

def _url(path):
    base = os.environ.get('APP_URL', '').rstrip('/')
    return f"{base}{path}"


def _site_name():
    try:
        from app import get_setting
        name = get_setting('site_name', None)
        if name:
            return name
    except Exception:
        pass
    return os.environ.get('SITE_NAME') or 'Cluster Platform'


def _site_tagline():
    try:
        from app import get_setting
        tagline = get_setting('site_tagline', None)
        if tagline:
            return tagline
    except Exception:
        pass
    return os.environ.get('SITE_TAGLINE') or 'A platform connecting experts and organisations.'


def _site_contact_email():
    sender = os.environ.get('MAIL_DEFAULT_SENDER', '')
    if '<' in sender:
        return sender.split('<')[1].replace('>', '').strip()
    if sender:
        return sender.strip()
    return os.environ.get('ADMIN_EMAIL', 'cluster@eced-au.org')


def _unsubscribe_url(email):
    import hmac, hashlib
    secret = os.environ.get('SECRET_KEY', 'fallback-secret')
    token = hmac.new(secret.encode(), email.lower().encode(), hashlib.sha256).hexdigest()
    base = os.environ.get('APP_URL', '').rstrip('/')
    return f"{base}/unsubscribe?email={email}&token={token}"


def _unsubscribe_footer(email):
    unsub_url = _unsubscribe_url(email)
    return f"""
        <p style="color:#aaa;font-size:0.78em;text-align:center;margin:0;">
            You are receiving this because you are a member of {_site_name()}.<br>
            <a href="{unsub_url}" style="color:#aaa;">Unsubscribe from notifications</a>
        </p>"""


def _base_email(title, body_html, footer_html=""):
    site_name = _site_name()
    if not footer_html:
        footer_html = f'<p style="color:#aaa;font-size:0.78em;text-align:center;margin:0;">This email was sent by {site_name}.</p>'

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    /* Links pick up the brand colour; buttons keep their own inline colour,
       which overrides this rule. */
    a {{ color: {EMAIL_BRAND}; }}
  </style>
</head>
<body style="margin:0;padding:0;background:#f4f6f8;font-family:Arial,sans-serif;color:{EMAIL_TEXT};">
  <div style="max-width:600px;margin:24px auto;background:#ffffff;border-radius:6px;
              overflow:hidden;border:1px solid #e0e0e0;">

    <!-- Body -->
    <div style="padding:36px 32px 24px;line-height:1.6;">
      <p style="margin:0 0 24px;font-size:0.75em;color:#aaa;text-transform:uppercase;
                letter-spacing:1px;font-weight:bold;text-align:center;">{site_name}</p>
      <h2 style="margin:0 0 20px;color:{EMAIL_HEADING};font-size:1.25em;font-weight:bold;line-height:1.3;">
        {title}
      </h2>
      {body_html}
    </div>

    <!-- Footer -->
    <div style="background:#f8f9fa;padding:16px 32px;border-top:1px solid #e8e8e8;">
      {footer_html}
    </div>

  </div>
</body>
</html>"""


def _render_template(template_key, context, subject_default='', title_default='', body_default=''):
    from app import EmailTemplate
    tmpl = EmailTemplate.query.filter_by(key=template_key).first()
    if tmpl and tmpl.is_confirmed:
        subject = tmpl.subject
        title = tmpl.title
        body = _expand_styles(tmpl.body_html)
    else:
        print(f"Email template '{template_key}' not confirmed — refusing to send.")
        return None, None, None

    full_context = {
        'site_name': _site_name(),
        'contact_email': _site_contact_email(),
        'login_url': _url('/login'),
        'register_url': _url('/register'),
    }
    full_context.update(context)

    for key, val in full_context.items():
        subject = subject.replace('{{ ' + key + ' }}', str(val))
        title = title.replace('{{ ' + key + ' }}', str(val))
        body = body.replace('{{ ' + key + ' }}', str(val))

    return subject, title, body


# ===================== EMAIL SENDING =====================

def send_email(to_email, subject, html_content, text_content=None):
    api_key = os.environ.get('BREVO_API_KEY')

    if not api_key:
        print("ERROR: BREVO_API_KEY is not set!")
        return False

    sender_raw = os.environ.get('MAIL_DEFAULT_SENDER')

    if not sender_raw:
        print("ERROR: MAIL_DEFAULT_SENDER is not set")
        return False

    if '<' in sender_raw:
        sender_name = sender_raw.split('<')[0].strip()
        sender_email = sender_raw.split('<')[1].replace('>', '').strip()
    else:
        sender_name = _site_name()
        sender_email = sender_raw

    payload = {
        "sender": {"name": sender_name, "email": sender_email},
        "to": [{"email": to_email}],
        "subject": subject,
        "htmlContent": html_content
    }
    if text_content:
        payload["textContent"] = text_content

    try:
        response = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "accept": "application/json",
                "api-key": api_key,
                "content-type": "application/json"
            },
            json=payload,
            timeout=10
        )
        if response.status_code not in (200, 201):
            print(f"Email error: {response.status_code} {response.text}")
            return False
        print(f"Email sent to {to_email}: {subject}")
        return True
    except Exception as e:
        print(f"Email error: {e}")
        return False


# ===================== TRANSACTIONAL EMAILS =====================

def send_otp_email(email, otp):
    context = {'otp': otp}
    defaults = next(t for t in EMAIL_TEMPLATES if t['key'] == 'otp')
    subject, title, body = _render_template('otp', context,
        defaults['subject'], defaults['title'], defaults['body_html'])
    if not subject:
        return False
    html = _base_email(title, body)
    send_email(email, subject, html)


def send_approval_email(email, initiative_slug=None):
    initiative_link = ""
    if initiative_slug:
        link = _url(f'/initiative/{initiative_slug}')
        initiative_link = f"""
            <p>Your initiative has also been published:</p>
            <p style="margin:8px 0 20px;">
                <a href="{link}" style="color:#007451;">View your initiative \u2192</a>
            </p>"""

    context = {'initiative_link': initiative_link}
    defaults = next(t for t in EMAIL_TEMPLATES if t['key'] == 'approval')
    subject, title, body = _render_template('approval', context,
        defaults['subject'], defaults['title'], defaults['body_html'])
    if not subject:
        return False
    html = _base_email(title, body)
    send_email(email, subject, html)


def send_initiative_approved_email(user, initiative_slug, initiative_title):
    context = {
        'user_name': user.name,
        'initiative_title': initiative_title,
        'initiative_url': _url(f'/initiative/{initiative_slug}'),
    }
    defaults = next(t for t in EMAIL_TEMPLATES if t['key'] == 'initiative_approved')
    subject, title, body = _render_template('initiative_approved', context,
        defaults['subject'], defaults['title'], defaults['body_html'])
    if not subject:
        return False
    html = _base_email(title, body)
    send_email(user.email, subject, html)


def send_certificate_email(user, cert_url, site_name):
    context = {
        'user_name': user.name,
        'site_name': site_name,
        'cert_url': cert_url,
    }
    defaults = next(t for t in EMAIL_TEMPLATES if t['key'] == 'certificate')
    subject, title, body = _render_template('certificate', context,
        defaults['subject'], defaults['title'], defaults['body_html'])
    if not subject:
        return False
    html = _base_email(title, body)
    send_email(user.email, subject, html)


def send_initiative_pending_email(user, initiative_title):
    context = {
        'user_name': user.name,
        'initiative_title': initiative_title,
    }
    defaults = next(t for t in EMAIL_TEMPLATES if t['key'] == 'initiative_pending')
    subject, title, body = _render_template('initiative_pending', context,
        defaults['subject'], defaults['title'], defaults['body_html'])
    if not subject:
        return False
    html = _base_email(title, body)
    send_email(user.email, subject, html)


def send_import_welcome_email(user):
    context = {
        'user_name': user.name,
        'user_email': user.email,
        'organization': user.organization or '',
    }
    defaults = next(t for t in EMAIL_TEMPLATES if t['key'] == 'import_welcome')
    subject, title, body = _render_template('import_welcome', context,
        defaults['subject'], defaults['title'], defaults['body_html'])
    if not subject:
        return False
    html = _base_email(title, body, footer_html=_unsubscribe_footer(user.email))
    send_email(user.email, subject, html)


def send_invitation_email(email, name, organization=None):
    user_name = name if name and name.strip() else (organization if organization else 'Colleague')
    context = {
        'user_name': user_name,
        'organization': organization or '',
    }
    defaults = next(t for t in EMAIL_TEMPLATES if t['key'] == 'invitation_org')
    _, title, body = _render_template('invitation_org', context,
        defaults['subject'], defaults['title'], defaults['body_html'])
    if not title:
        return False
    subject = f"Invitation for Experts from {organization} to Join {_site_name()}" if organization else f"Invitation to Join {_site_name()}"
    html = _base_email(title, body, footer_html=_unsubscribe_footer(email))
    send_email(email, subject, html)


def send_individual_invitation_email(email, name):
    user_name = name if name and name.strip() else 'Colleague'
    context = {'user_name': user_name}
    defaults = next(t for t in EMAIL_TEMPLATES if t['key'] == 'invitation_individual')
    subject, title, body = _render_template('invitation_individual', context,
        defaults['subject'], defaults['title'], defaults['body_html'])
    if not subject:
        return False
    html = _base_email(title, body, footer_html=_unsubscribe_footer(email))
    send_email(email, subject, html)


def send_event_invitation_email(email, name, event, event_url):
    event_date = event.start_date.strftime('%B %d, %Y at %H:%M UTC')
    if event.end_date:
        event_date += f" \u2013 {event.end_date.strftime('%B %d, %Y at %H:%M UTC')}"

    context = {
        'user_name': name,
        'event_title': event.title,
        'event_date': event_date,
        'event_url': event_url,
    }
    defaults = next(t for t in EMAIL_TEMPLATES if t['key'] == 'event_invitation')
    subject, title, body = _render_template('event_invitation', context,
        defaults['subject'], defaults['title'], defaults['body_html'])
    if not subject:
        return False
    html = _base_email(title, body, footer_html=_unsubscribe_footer(email))
    send_email(email, subject, html)


def send_project_signup_confirmation(user, project, signed_up_activities):
    project_url = _url(f'/project/{project.id}')
    activity_items = "".join(f"<li>{a.title}</li>" for a in signed_up_activities)

    context = {
        'user_name': user.name,
        'project_title': project.title,
        'project_deadline': project.deadline.strftime("%B %d, %Y"),
        'activity_items': activity_items,
        'project_url': project_url,
    }
    defaults = next(t for t in EMAIL_TEMPLATES if t['key'] == 'project_signup')
    subject, title, body = _render_template('project_signup', context,
        defaults['subject'], defaults['title'], defaults['body_html'])
    if not subject:
        return False
    html = _base_email(title, body)
    send_email(user.email, subject, html)


def send_project_signup_admin_alert(admin_email, user, project, signed_up_activities):
    admin_project_url = _url(f'/admin/project/{project.id}/edit')
    activity_items = "".join(f"<li>{a.title}</li>" for a in signed_up_activities)

    context = {
        'user_name': user.name,
        'user_email': user.email,
        'organization': user.organization or '',
        'project_title': project.title,
        'project_deadline': project.deadline.strftime('%B %d, %Y'),
        'activity_items': activity_items,
        'admin_url': admin_project_url,
    }
    defaults = next(t for t in EMAIL_TEMPLATES if t['key'] == 'project_signup_admin')
    subject, title, body = _render_template('project_signup_admin', context,
        defaults['subject'], defaults['title'], defaults['body_html'])
    if not subject:
        return False
    html = _base_email(title, body,
        footer_html=f'<p style="color:#aaa;font-size:0.78em;text-align:center;margin:0;">Automated alert from {_site_name()}.</p>')
    send_email(admin_email, subject, html)


def send_project_notification(project):
    from app import User
    users = User.query.filter_by(is_approved=True).all()
    project_url = _url(f'/project/{project.id}')

    context = {
        'project_title': project.title,
        'project_deadline': project.deadline.strftime("%B %d, %Y"),
        'project_url': project_url,
    }
    defaults = next(t for t in EMAIL_TEMPLATES if t['key'] == 'project_notification')
    subject, title, body = _render_template('project_notification', context,
        defaults['subject'], defaults['title'], defaults['body_html'])
    if not subject:
        return False
    html = _base_email(title, body)
    for user in users:
        send_email(user.email, subject, html)


def send_project_approved_email(user, project):
    project_url = _url(f'/project/{project.id}')
    context = {
        'user_name': user.name,
        'project_title': project.title,
        'project_url': project_url,
    }
    defaults = next(t for t in EMAIL_TEMPLATES if t['key'] == 'project_approved')
    subject, title, body = _render_template('project_approved', context,
        defaults['subject'], defaults['title'], defaults['body_html'])
    if not subject:
        return False
    html = _base_email(title, body)
    send_email(user.email, subject, html)


def send_event_approved_email(user, event):
    event_url = _url(f'/event/{event.id}')
    event_date = event.start_date.strftime('%B %d, %Y at %H:%M UTC')
    context = {
        'user_name': user.name,
        'event_title': event.title,
        'event_date': event_date,
        'event_url': event_url,
    }
    defaults = next(t for t in EMAIL_TEMPLATES if t['key'] == 'event_approved')
    subject, title, body = _render_template('event_approved', context,
        defaults['subject'], defaults['title'], defaults['body_html'])
    if not subject:
        return False
    html = _base_email(title, body)
    send_email(user.email, subject, html)


def send_member_notification(subject, html):
    from app import User
    users = User.query.filter_by(is_approved=True).all()
    for user in users:
        send_email(user.email, subject, html)


def send_event_notification(event):
    from app import User
    users = User.query.filter_by(is_approved=True, is_subscribed=True).all()

    event_url = _url(f'/event/{event.id}')
    event_date = event.start_date.strftime('%B %d, %Y at %H:%M')

    context = {
        'event_title': event.title,
        'event_date': event_date,
        'event_url': event_url,
    }
    defaults = next(t for t in EMAIL_TEMPLATES if t['key'] == 'event_notification')
    subject, title, body = _render_template('event_notification', context,
        defaults['subject'], defaults['title'], defaults['body_html'])
    if not subject:
        return False

    for user in users:
        html = _base_email(title, body, footer_html=_unsubscribe_footer(user.email))
        send_email(user.email, subject, html)


def send_event_registration_confirmation(user, event):
    event_url = _url(f'/event/{event.id}')
    event_date = event.start_date.strftime('%B %d, %Y at %H:%M UTC')
    if event.end_date:
        event_date += f" - {event.end_date.strftime('%B %d, %Y at %H:%M UTC')}"

    meeting_link_html = ""
    if event.meeting_link:
        meeting_link_html = f'<p style="margin:4px 0;"><strong>Meeting Link:</strong> <a href="{event.meeting_link}" style="color:#007451;">Join here</a></p>'

    context = {
        'user_name': user.name,
        'event_title': event.title,
        'event_date': event_date,
        'meeting_link_html': meeting_link_html,
        'event_url': event_url,
    }
    defaults = next(t for t in EMAIL_TEMPLATES if t['key'] == 'event_registration')
    subject, title, body = _render_template('event_registration', context,
        defaults['subject'], defaults['title'], defaults['body_html'])
    if not subject:
        return False
    html = _base_email(title, body)
    send_email(user.email, subject, html)


def send_custom_bulk_email(to_email, name, subject, body_text):
    body_content = "".join(
        f'<p style="margin:0 0 10px;">{line}</p>' if line.strip() else '<br>'
        for line in body_text.splitlines()
    )
    context = {'user_name': name, 'body_content': body_content}
    defaults = next(t for t in EMAIL_TEMPLATES if t['key'] == 'custom_bulk')
    _, title, body = _render_template('custom_bulk', context,
        defaults['subject'], defaults['title'], defaults['body_html'])
    if not title:
        return False
    html = _base_email(title, body, footer_html=_unsubscribe_footer(to_email))
    send_email(to_email, subject, html)


def send_single_initiative_notification(initiative_data, users):
    if not users or not initiative_data:
        return

    title = initiative_data['title']
    initiative_url = initiative_data['url']

    context = {
        'initiative_title': title,
        'initiative_url': initiative_url,
    }
    defaults = next(t for t in EMAIL_TEMPLATES if t['key'] == 'initiative_single')
    subject, title, body = _render_template('initiative_single', context,
        defaults['subject'], defaults['title'], defaults['body_html'])
    if not subject:
        return

    for user in users:
        html = _base_email(title, body, footer_html=_unsubscribe_footer(user.email))
        send_email(user.email, subject, html)


def send_bulk_initiatives_digest(initiatives_data, users):
    if not users or not initiatives_data:
        return

    count = len(initiatives_data)
    plural = 's' if count != 1 else ''
    subject_line = f"{count} New Initiative{plural} on {_site_name()}"
    title_line = f"{count} New Initiative{plural} Published"
    notification_intro = (
        f"The following {count} new initiative{'s have' if count != 1 else ' has'} "
        f"just been published on {_site_name()}"
    )

    items_html = ""
    for item in initiatives_data:
        desc_block = (
            f'<p style="margin:6px 0 0;color:#555;font-size:0.9em;line-height:1.5;">'
            f'{item["short_description"]}</p>'
            if item.get("short_description") else ""
        )
        items_html += f"""
        <div style="margin-bottom:12px;padding:16px 18px;background:#f8f9fa;
                    border-left:4px solid #dee2e6;border-radius:4px;">
            <a href="{item['url']}"
               style="font-size:1em;font-weight:bold;color:#007451;text-decoration:none;line-height:1.4;">
                {item['title']}
            </a>
            {desc_block}
            <p style="margin:10px 0 0;">
                <a href="{item['url']}"
                   style="font-size:0.85em;color:#007451;text-decoration:none;font-weight:bold;">
                    Read more \u2192
                </a>
            </p>
        </div>"""

    context = {
        'subject_line': subject_line,
        'title_line': title_line,
        'notification_intro': notification_intro,
        'items_html': items_html,
    }
    defaults = next(t for t in EMAIL_TEMPLATES if t['key'] == 'initiative_digest')
    subject, title, body = _render_template('initiative_digest', context,
        defaults['subject'], defaults['title'], defaults['body_html'])
    if not subject:
        return

    for user in users:
        html = _base_email(title, body, footer_html=_unsubscribe_footer(user.email))
        send_email(user.email, subject, html)


# ===================== POLICY DEVELOPMENT EMAILS =====================

def send_single_policy_notification(policy_data, users):
    if not users or not policy_data:
        return

    title = policy_data['title']
    policy_url = policy_data['url']
    summary = policy_data.get('short_summary', '')
    country = policy_data.get('country', '')
    published_date = policy_data.get('published_date', '')

    items_html = ""
    meta_parts = []
    if country:
        meta_parts.append(f"Country: {country}")
    if published_date:
        meta_parts.append(f"Published: {published_date}")
    meta_line = (
        f'<p style="margin:4px 0 12px;color:#777;font-size:0.88em;">{" | ".join(meta_parts)}</p>'
        if meta_parts else ""
    )
    summary_block = (
        f'<p style="color:#555;font-size:0.95em;line-height:1.6;margin:8px 0 0;">{summary}</p>'
        if summary else ""
    )
    items_html += f"""
        <div style="margin-bottom:12px;padding:16px 18px;background:#f8f9fa;
                    border-left:4px solid #dee2e6;border-radius:4px;">
            <p style="margin:0;font-size:1.05em;font-weight:bold;color:#333;">{title}</p>
            {meta_line}
            {summary_block}
            <p style="margin:12px 0 0;">
                <a href="{policy_url}"
                   style="font-size:0.85em;color:#007451;text-decoration:none;font-weight:bold;">
                    Read more \u2192
                </a>
            </p>
        </div>"""

    context = {
        'policy_title': title,
        'items_html': items_html,
    }
    defaults = next(t for t in EMAIL_TEMPLATES if t['key'] == 'policy_single')
    subject, title, body = _render_template('policy_single', context,
        defaults['subject'], defaults['title'], defaults['body_html'])
    if not subject:
        return

    for user in users:
        html = _base_email(title, body, footer_html=_unsubscribe_footer(user.email))
        send_email(user.email, subject, html)


def send_bulk_policies_digest(policies_data, users):
    if not users or not policies_data:
        return

    count = len(policies_data)
    plural = 's' if count != 1 else ''
    subject_line = f"{count} New Policy Development{plural} on {_site_name()}"
    title_line = f"{count} New Policy Development{plural} Published"
    notification_intro = (
        f"The following {count} new policy development{'s have' if count != 1 else ' has'} "
        f"just been published on {_site_name()}"
    )

    items_html = ""
    for item in policies_data:
        meta_parts = []
        if item.get('country'):
            meta_parts.append(f"Country: {item['country']}")
        if item.get('published_date'):
            meta_parts.append(f"Published: {item['published_date']}")
        meta_line = (
            f'<p style="margin:4px 0 8px;color:#777;font-size:0.85em;">{" | ".join(meta_parts)}</p>'
            if meta_parts else ""
        )
        summary_block = (
            f'<p style="margin:6px 0 0;color:#555;font-size:0.9em;line-height:1.5;">'
            f'{item["short_summary"]}</p>'
            if item.get("short_summary") else ""
        )
        items_html += f"""
        <div style="margin-bottom:12px;padding:16px 18px;background:#f8f9fa;
                    border-left:4px solid #dee2e6;border-radius:4px;">
            <a href="{item['url']}"
               style="font-size:1em;font-weight:bold;color:#007451;text-decoration:none;line-height:1.4;">
                {item['title']}
            </a>
            {meta_line}
            {summary_block}
            <p style="margin:10px 0 0;">
                <a href="{item['url']}"
                   style="font-size:0.85em;color:#007451;text-decoration:none;font-weight:bold;">
                    Read more \u2192
                </a>
            </p>
        </div>"""

    context = {
        'subject_line': subject_line,
        'title_line': title_line,
        'notification_intro': notification_intro,
        'items_html': items_html,
    }
    defaults = next(t for t in EMAIL_TEMPLATES if t['key'] == 'policy_digest')
    subject, title, body = _render_template('policy_digest', context,
        defaults['subject'], defaults['title'], defaults['body_html'])
    if not subject:
        return

    for user in users:
        html = _base_email(title, body, footer_html=_unsubscribe_footer(user.email))
        send_email(user.email, subject, html)


# ===================== DOCUMENT LIBRARY EMAILS =====================

def send_single_document_notification(doc_data, users):
    if not users or not doc_data:
        return

    title = doc_data['title']
    doc_url = doc_data['url']
    description = doc_data.get('description', '')
    year = doc_data.get('year_published', '')
    file_type = doc_data.get('file_type', '')

    items_html = ""
    meta_parts = []
    if file_type:
        meta_parts.append(f"Type: {file_type.upper()}")
    if year:
        meta_parts.append(f"Year: {year}")
    meta_line = (
        f'<p style="margin:4px 0 12px;color:#777;font-size:0.88em;">{" | ".join(meta_parts)}</p>'
        if meta_parts else ""
    )
    desc_block = (
        f'<p style="color:#555;font-size:0.95em;line-height:1.6;margin:8px 0 0;">{description}</p>'
        if description else ""
    )
    items_html += f"""
        <div style="margin-bottom:12px;padding:16px 18px;background:#f8f9fa;
                    border-left:4px solid #dee2e6;border-radius:4px;">
            <p style="margin:0;font-size:1.05em;font-weight:bold;color:#333;">{title}</p>
            {meta_line}
            {desc_block}
            <p style="margin:12px 0 0;">
                <a href="{doc_url}"
                   style="font-size:0.85em;color:#007451;text-decoration:none;font-weight:bold;">
                    View document \u2192
                </a>
            </p>
        </div>"""

    context = {
        'doc_title': title,
        'items_html': items_html,
    }
    defaults = next(t for t in EMAIL_TEMPLATES if t['key'] == 'document_single')
    subject, title, body = _render_template('document_single', context,
        defaults['subject'], defaults['title'], defaults['body_html'])
    if not subject:
        return

    for user in users:
        html = _base_email(title, body, footer_html=_unsubscribe_footer(user.email))
        send_email(user.email, subject, html)


def send_bulk_documents_digest(docs_data, users):
    if not users or not docs_data:
        return

    count = len(docs_data)
    plural = 's' if count != 1 else ''
    subject_line = f"{count} New Document{plural} on {_site_name()}"
    title_line = f"{count} New Document{plural} Published"
    notification_intro = (
        f"The following {count} new document{'s have' if count != 1 else ' has'} "
        f"just been published on {_site_name()}"
    )

    items_html = ""
    for item in docs_data:
        meta_parts = []
        if item.get('file_type'):
            meta_parts.append(f"Type: {item['file_type'].upper()}")
        if item.get('year_published'):
            meta_parts.append(f"Year: {item['year_published']}")
        meta_line = (
            f'<p style="margin:4px 0 8px;color:#777;font-size:0.85em;">{" | ".join(meta_parts)}</p>'
            if meta_parts else ""
        )
        desc_block = (
            f'<p style="margin:6px 0 0;color:#555;font-size:0.9em;line-height:1.5;">'
            f'{item["description"]}</p>'
            if item.get("description") else ""
        )
        items_html += f"""
        <div style="margin-bottom:12px;padding:16px 18px;background:#f8f9fa;
                    border-left:4px solid #dee2e6;border-radius:4px;">
            <a href="{item['url']}"
               style="font-size:1em;font-weight:bold;color:#007451;text-decoration:none;line-height:1.4;">
                {item['title']}
            </a>
            {meta_line}
            {desc_block}
            <p style="margin:10px 0 0;">
                <a href="{item['url']}"
                   style="font-size:0.85em;color:#007451;text-decoration:none;font-weight:bold;">
                    View document \u2192
                </a>
            </p>
        </div>"""

    context = {
        'subject_line': subject_line,
        'title_line': title_line,
        'notification_intro': notification_intro,
        'items_html': items_html,
    }
    defaults = next(t for t in EMAIL_TEMPLATES if t['key'] == 'document_digest')
    subject, title, body = _render_template('document_digest', context,
        defaults['subject'], defaults['title'], defaults['body_html'])
    if not subject:
        return

    for user in users:
        html = _base_email(title, body, footer_html=_unsubscribe_footer(user.email))
        send_email(user.email, subject, html)


# ===================== TECHNICAL ASSISTANCE NEED EMAILS =====================

def send_single_ta_notification(ta_data, users):
    if not users or not ta_data:
        return

    title = ta_data['title']
    ta_url = ta_data['url']
    short_description = ta_data.get('short_description', '')
    country = ta_data.get('country', '')
    author = ta_data.get('author', '')

    items_html = ""
    meta_parts = []
    if author:
        meta_parts.append(f"Submitted by: {author}")
    if country:
        meta_parts.append(f"Country: {country}")
    meta_line = (
        f'<p style="margin:4px 0 12px;color:#777;font-size:0.88em;">{" | ".join(meta_parts)}</p>'
        if meta_parts else ""
    )
    desc_block = (
        f'<p style="color:#555;font-size:0.95em;line-height:1.6;margin:8px 0 0;">{short_description}</p>'
        if short_description else ""
    )
    items_html += f"""
        <div style="margin-bottom:12px;padding:16px 18px;background:#f8f9fa;
                    border-left:4px solid #dee2e6;border-radius:4px;">
            <p style="margin:0;font-size:1.05em;font-weight:bold;color:#333;">{title}</p>
            {meta_line}
            {desc_block}
            <p style="margin:12px 0 0;">
                <a href="{ta_url}"
                   style="font-size:0.85em;color:#007451;text-decoration:none;font-weight:bold;">
                    Read more \u2192
                </a>
            </p>
        </div>"""

    context = {
        'ta_title': title,
        'items_html': items_html,
    }
    defaults = next(t for t in EMAIL_TEMPLATES if t['key'] == 'ta_single')
    subject, title, body = _render_template('ta_single', context,
        defaults['subject'], defaults['title'], defaults['body_html'])
    if not subject:
        return

    for user in users:
        html = _base_email(title, body, footer_html=_unsubscribe_footer(user.email))
        send_email(user.email, subject, html)


def send_bulk_ta_digest(ta_data_list, users):
    if not users or not ta_data_list:
        return

    count = len(ta_data_list)
    plural = 's' if count != 1 else ''
    subject_line = f"{count} New Technical Assistance Need{plural} \u2013 {_site_name()}"
    title_line = f"{count} New Technical Assistance Need{plural} Published"
    notification_intro = (
        f"The following {count} new technical assistance need{'s have' if count != 1 else ' has'} "
        f"been published by Member States on {_site_name()}"
    )

    items_html = ""
    for item in ta_data_list:
        meta_parts = []
        if item.get('author'):
            meta_parts.append(f"Submitted by: {item['author']}")
        if item.get('country'):
            meta_parts.append(f"Country: {item['country']}")
        meta_line = (
            f'<p style="margin:4px 0 8px;color:#777;font-size:0.85em;">{" | ".join(meta_parts)}</p>'
            if meta_parts else ""
        )
        desc_block = (
            f'<p style="margin:6px 0 0;color:#555;font-size:0.9em;line-height:1.5;">'
            f'{item["short_description"]}</p>'
            if item.get("short_description") else ""
        )
        items_html += f"""
        <div style="margin-bottom:12px;padding:16px 18px;background:#f8f9fa;
                    border-left:4px solid #dee2e6;border-radius:4px;">
            <a href="{item['url']}"
               style="font-size:1em;font-weight:bold;color:#007451;text-decoration:none;line-height:1.4;">
                {item['title']}
            </a>
            {meta_line}
            {desc_block}
            <p style="margin:10px 0 0;">
                <a href="{item['url']}"
                   style="font-size:0.85em;color:#007451;text-decoration:none;font-weight:bold;">
                    Read more \u2192
                </a>
            </p>
        </div>"""

    context = {
        'subject_line': subject_line,
        'title_line': title_line,
        'notification_intro': notification_intro,
        'items_html': items_html,
    }
    defaults = next(t for t in EMAIL_TEMPLATES if t['key'] == 'ta_digest')
    subject, title, body = _render_template('ta_digest', context,
        defaults['subject'], defaults['title'], defaults['body_html'])
    if not subject:
        return

    for user in users:
        html = _base_email(title, body, footer_html=_unsubscribe_footer(user.email))
        send_email(user.email, subject, html)


def send_ta_invitation_email(email, name, ta_url):
    context = {
        'user_name': name,
        'ta_url': ta_url,
    }
    defaults = next(t for t in EMAIL_TEMPLATES if t['key'] == 'ta_invitation')
    subject, title, body = _render_template('ta_invitation', context,
        defaults['subject'], defaults['title'], defaults['body_html'])
    if not subject:
        return False
    html = _base_email(title, body)
    send_email(email, subject, html)
