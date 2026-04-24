import os
import requests
from flask import current_app


def _url(path):
    """Build an absolute URL using APP_URL env var — no Flask request context needed."""
    base = os.environ.get('APP_URL', '').rstrip('/')
    return f"{base}{path}"


def _unsubscribe_url(email):
    """Return just the unsubscribe URL for a given email (no HTML wrapping)."""
    import hmac, hashlib
    secret = os.environ.get('SECRET_KEY', 'fallback-secret')
    token = hmac.new(secret.encode(), email.lower().encode(), hashlib.sha256).hexdigest()
    base = os.environ.get('APP_URL', '').rstrip('/')
    return f"{base}/unsubscribe?email={email}&token={token}"


def _unsubscribe_footer(email):
    """Generate an HTML unsubscribe footer block for a given recipient email."""
    unsub_url = _unsubscribe_url(email)
    return f"""
        <p style="color:#aaa;font-size:0.78em;text-align:center;margin:0;">
            You are receiving this because you are a member of the AU&nbsp;ECED-FLN Cluster Platform.<br>
            <a href="{unsub_url}" style="color:#aaa;">Unsubscribe from notifications</a>
        </p>"""


def _base_email(title, body_html, footer_html=""):
    """
    Shared email shell.
    - Green header bar (#007451) matches the site navbar and footer.
    - White body keeps it clean and readable.
    - Light grey footer for unsubscribe / platform credit.
    """
    if not footer_html:
        footer_html = '<p style="color:#aaa;font-size:0.78em;text-align:center;margin:0;">This email was sent by the AU ECED-FLN Cluster Platform.</p>'

    return f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f4f6f8;font-family:Arial,sans-serif;color:#333;">
  <div style="max-width:600px;margin:24px auto;background:#ffffff;border-radius:6px;
              overflow:hidden;border:1px solid #e0e0e0;">

    <!-- Body -->
    <div style="padding:36px 32px 24px;line-height:1.6;">
      <p style="margin:0 0 24px;font-size:0.75em;color:#007451;text-transform:uppercase;
                letter-spacing:1px;font-weight:bold;">AU ECED-FLN Cluster Platform</p>
      <h2 style="margin:0 0 20px;color:#007451;font-size:1.25em;font-weight:bold;line-height:1.3;">
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


def _btn(url, label):
    """Primary CTA button — blue, centred."""
    return f"""
    <p style="text-align:center;margin:28px 0;">
      <a href="{url}" style="display:inline-block;background:#007451;color:#ffffff;
         padding:13px 32px;text-decoration:none;border-radius:5px;
         font-weight:bold;font-size:0.95em;">{label}</a>
    </p>"""


def _info_box(html_content):
    """Light grey box with a green left accent — for event/project/initiative details."""
    return f"""
    <div style="background:#f8f9fa;border-left:4px solid #007451;
                border-radius:4px;padding:16px 20px;margin:20px 0;">
      {html_content}
    </div>"""


# ===================== EMAIL SENDING =====================

def send_email(to_email, subject, html_content, text_content=None):
    api_key = os.environ.get('BREVO_API_KEY')

    if not api_key:
        print("ERROR: BREVO_API_KEY is not set!")
        return False

    sender_raw = os.environ.get('MAIL_DEFAULT_SENDER', 'AU ECED-FLN Platform <cluster@eced-au.org>')

    if '<' in sender_raw:
        sender_name = sender_raw.split('<')[0].strip()
        sender_email = sender_raw.split('<')[1].replace('>', '').strip()
    else:
        sender_name = 'AU ECED-FLN Platform'
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
    subject = "Your Login OTP - AU ECED-FLN Platform"
    body = f"""
        <p>Your one-time password (OTP) for login is:</p>
        <div style="background:#f4f4f4;padding:20px;text-align:center;font-size:26px;
                    font-weight:bold;letter-spacing:6px;margin:20px 0;border-radius:4px;">
            {otp}
        </div>
        <p style="color:#666;font-size:0.9em;">This code expires in 10 minutes.
        If you didn't request this, please ignore this email.</p>
    """
    html = _base_email("Your Login Code", body)
    send_email(email, subject, html)


def send_approval_email(email, initiative_slug=None):
    subject = "Welcome to AU ECED-FLN Cluster Platform"
    login_url = _url('/login')

    initiative_link = ""
    if initiative_slug:
        link = _url(f'/initiative/{initiative_slug}')
        initiative_link = f"""
            <p>Your initiative has also been published:</p>
            <p style="margin:8px 0 20px;">
                <a href="{link}" style="color:#007451;">View your initiative →</a>
            </p>"""

    body = f"""
        <p>Your registration has been approved! You can now log in to submit initiatives
        and participate in discussions.</p>
        {initiative_link}
        {_btn(login_url, "Log In to the Platform")}
    """
    html = _base_email("Welcome to AU ECED-FLN Cluster Platform", body)
    send_email(email, subject, html)


def send_initiative_approved_email(user, initiative_slug, initiative_title):
    """Notify a user that their initiative has been approved and published."""
    link = _url(f'/initiative/{initiative_slug}')
    subject = "Your initiative has been published – AU ECED-FLN Platform"
    body = f"""
        <p>Dear {user.name},</p>
        <p>Your initiative <strong>{initiative_title}</strong> has been reviewed and is now
        live on the AU ECED-FLN Cluster Platform.</p>
        {_btn(link, "View Your Initiative")}
    """
    html = _base_email("Your Initiative Has Been Published", body)
    send_email(user.email, subject, html)


def send_initiative_pending_email(user, initiative_title):
    """Notify a user that their imported initiative is pending review."""
    login_url = _url('/login')
    subject = "Your initiative has been submitted for review – AU ECED-FLN Platform"
    body = f"""
        <p>Dear {user.name},</p>
        <p>Your initiative <strong>{initiative_title}</strong> has been submitted to the
        AU ECED-FLN Cluster Platform and is pending review by our team.</p>
        <p>You will receive another email once it has been approved and published.</p>
        {_btn(login_url, "Log In to the Platform")}
    """
    html = _base_email("Initiative Submitted for Review", body)
    send_email(user.email, subject, html)


def send_import_welcome_email(user):
    """Send a welcome email to a member who was imported by admin."""
    login_url = _url('/login')
    subject = "You've been added to the AU ECED-FLN Cluster Platform"
    body = f"""
        <p>Dear {user.name},</p>
        <p>You have been added to the <strong>African Union Early Childhood Education and Development &amp;
        Foundational Learning (ECED-FLN) Cluster Platform</strong> as a member representing
        <strong>{user.organization}</strong>.</p>
        <p>This platform connects experts and organizations across Africa working to accelerate
        Early Childhood Education and Foundational Learning. As a member you can:</p>
        <ul style="padding-left:20px;margin:8px 0 16px;">
            <li>Share and explore ECED-FLN initiatives from across the continent</li>
            <li>Participate in the Q&amp;A forum and contribute recommendations</li>
            <li>Register for cluster events and complete polls</li>
            <li>Connect with other experts in the network</li>
        </ul>
        <p><strong>To get started, please log in and complete your profile</strong> by adding
        descriptions of the ECED-FLN projects you are currently involved in or have worked on.
        This helps other members find and connect with you based on your areas of expertise.</p>
        {_btn(login_url, "Log In to the Platform")}
        <p style="color:#666;font-size:0.9em;">Your registered email address is: {user.email}<br>
        Use this to log in — you will receive a one-time password (OTP) each time you sign in.</p>
    """
    html = _base_email("Welcome to the AU ECED-FLN Cluster Platform", body,
                       footer_html=_unsubscribe_footer(user.email))
    send_email(user.email, subject, html)


def send_invitation_email(email, name):
    """Send an invitation email to someone who hasn't registered yet."""
    register_url = _url('/register')
    subject = "You're invited to join the AU ECED-FLN Cluster Platform"
    body = f"""
        <p>Dear {name},</p>
        <p>You have been invited to join the <strong>African Union Early Childhood Education and
        Development &amp; Foundational Learning (ECED-FLN) Cluster Platform</strong> — a network
        connecting experts and organisations across Africa working to accelerate Early Childhood
        Education and Foundational Learning.</p>
        <p>As a member you will be able to:</p>
        <ul style="padding-left:20px;margin:8px 0 16px;">
            <li>Share and explore ECED-FLN initiatives from across the continent</li>
            <li>Participate in the Q&amp;A forum and contribute recommendations</li>
            <li>Register for cluster events and complete polls</li>
            <li>Connect with other experts in the network</li>
        </ul>
        {_btn(register_url, "Register Now")}
    """
    html = _base_email("You're Invited to the AU ECED-FLN Cluster Platform", body,
                       footer_html=_unsubscribe_footer(email))
    send_email(email, subject, html)


def send_event_invitation_email(email, name, event, event_url):
    """Send an invitation email for a specific event."""
    event_date = event.start_date.strftime('%B %d, %Y at %H:%M UTC')
    if event.end_date:
        event_date += f" – {event.end_date.strftime('%B %d, %Y at %H:%M UTC')}"
    subject = f"Invitation to {event.title}"
    excerpt = event.description[:300] + ('...' if len(event.description) > 300 else '')
    body = f"""
        <p>Dear {name},</p>
        <p>You have been invited to attend the following event on the
        <strong>AU ECED-FLN Cluster Platform</strong>:</p>
        {_info_box(f'<h3 style="margin:0 0 8px;">{event.title}</h3><p style="margin:4px 0;"><strong>Date:</strong> {event_date}</p><p style="margin:8px 0 0;color:#555;">{excerpt}</p>')}
        {_btn(event_url, "View Event & Register")}
    """
    html = _base_email(f"You're Invited: {event.title}", body,
                       footer_html=_unsubscribe_footer(email))
    send_email(email, subject, html)


def send_project_signup_confirmation(user, project, signed_up_activities):
    """Confirm to a member that they have successfully joined a project."""
    project_url = _url(f'/project/{project.id}')
    activity_items = "".join(f"<li>{a.title}</li>" for a in signed_up_activities)
    subject = f"You've joined: {project.title} – AU ECED-FLN Platform"
    body = f"""
        <p>Dear {user.name},</p>
        <p>You have successfully signed up to participate in the following project:</p>
        {_info_box(f'<h3 style="margin:0 0 8px;">{project.title}</h3><p style="margin:4px 0;"><strong>Deadline:</strong> {project.deadline.strftime("%B %d, %Y")}</p><p style="margin:12px 0 4px;"><strong>Activities you signed up for:</strong></p><ul style="margin:4px 0 0;padding-left:18px;">{activity_items}</ul>')}
        {_btn(project_url, "View Project")}
    """
    html = _base_email("You've Joined a Project", body)
    send_email(user.email, subject, html)


def send_project_signup_admin_alert(admin_email, user, project, signed_up_activities):
    """Notify the admin that a member has signed up for a project."""
    admin_project_url = _url(f'/admin/project/{project.id}/edit')
    activity_items = "".join(f"<li>{a.title}</li>" for a in signed_up_activities)
    subject = f"[New Sign-up] {user.name} joined \"{project.title}\""
    body = f"""
        <p>A member has just signed up for a project on the AU ECED-FLN Cluster Platform.</p>
        <table style="width:100%;border-collapse:collapse;margin:16px 0;">
            <tr>
                <td style="padding:6px 12px 6px 0;font-weight:bold;width:130px;">Member</td>
                <td style="padding:6px 0;">{user.name} ({user.email})</td>
            </tr>
            <tr style="background:#f9f9f9;">
                <td style="padding:6px 12px 6px 0;font-weight:bold;">Organisation</td>
                <td style="padding:6px 0;">{user.organization}</td>
            </tr>
            <tr>
                <td style="padding:6px 12px 6px 0;font-weight:bold;">Project</td>
                <td style="padding:6px 0;">{project.title}</td>
            </tr>
            <tr style="background:#f9f9f9;">
                <td style="padding:6px 12px 6px 0;font-weight:bold;">Deadline</td>
                <td style="padding:6px 0;">{project.deadline.strftime('%B %d, %Y')}</td>
            </tr>
        </table>
        <p><strong>Activities selected:</strong></p>
        <ul style="margin:8px 0 16px 20px;">{activity_items}</ul>
        {_btn(admin_project_url, "Manage Project in Admin")}
    """
    html = _base_email("New Project Sign-up", body,
                       footer_html='<p style="color:#aaa;font-size:0.78em;text-align:center;margin:0;">Automated alert from the AU ECED-FLN Cluster Platform.</p>')
    send_email(admin_email, subject, html)


def send_project_notification(project):
    """Notify all approved members that a new project has been published."""
    from app import User
    users = User.query.filter_by(is_approved=True).all()
    project_url = _url(f'/project/{project.id}')
    subject = f"New Project: {project.title}"
    excerpt = project.description[:300] + ('...' if len(project.description) > 300 else '')
    body = f"""
        <p>A new collaborative project has been published on the AU ECED-FLN Cluster Platform:</p>
        {_info_box(f'<h3 style="margin:0 0 8px;">{project.title}</h3><p style="margin:4px 0;color:#555;">{excerpt}</p><p style="margin:8px 0 0;"><strong>Deadline:</strong> {project.deadline.strftime("%B %d, %Y")}</p>')}
        {_btn(project_url, "View Project & Join")}
    """
    html = _base_email("New Project on the Platform", body)
    for user in users:
        send_email(user.email, subject, html)


def send_project_approved_email(user, project):
    """Notify the submitter that their project has been approved and published."""
    project_url = _url(f'/project/{project.id}')
    subject = "Your project has been published – AU ECED-FLN Platform"
    body = f"""
        <p>Dear {user.name},</p>
        <p>Your project <strong>{project.title}</strong> has been reviewed and is now
        live on the AU ECED-FLN Cluster Platform. Members can now view it and sign up to participate.</p>
        {_btn(project_url, "View Your Project")}
    """
    html = _base_email("Your Project Has Been Published", body)
    send_email(user.email, subject, html)


def send_event_approved_email(user, event):
    """Notify the submitter that their event has been approved and published."""
    event_url = _url(f'/event/{event.id}')
    subject = "Your event has been published – AU ECED-FLN Platform"
    body = f"""
        <p>Dear {user.name},</p>
        <p>Your event <strong>{event.title}</strong> has been reviewed and is now
        live on the AU ECED-FLN Cluster Platform. All members have been notified and can register.</p>
        <p><strong>Date:</strong> {event.start_date.strftime('%B %d, %Y at %H:%M UTC')}</p>
        {_btn(event_url, "View Your Event")}
    """
    html = _base_email("Your Event Has Been Published", body)
    send_email(user.email, subject, html)


def send_member_notification(subject, html):
    """Send an email notification to all approved members."""
    from app import User
    users = User.query.filter_by(is_approved=True).all()
    for user in users:
        send_email(user.email, subject, html)


def send_event_notification(event):
    """Send email notification about a new event to all approved, subscribed members."""
    from app import User
    users = User.query.filter_by(is_approved=True, is_subscribed=True).all()
    subject = f"New Event: {event.title}"
    event_url = _url(f'/event/{event.id}')
    event_date = event.start_date.strftime('%B %d, %Y at %H:%M')
    excerpt = event.description[:300] + ('...' if len(event.description) > 300 else '')

    for user in users:
        body = f"""
            <p>A new event has been published on the AU ECED-FLN Cluster Platform:</p>
            {_info_box(f'<h3 style="margin:0 0 8px;">{event.title}</h3><p style="margin:4px 0;"><strong>Date:</strong> {event_date}</p><p style="margin:8px 0 0;color:#555;">{excerpt}</p>')}
            {_btn(event_url, "Register Now")}
        """
        html = _base_email(f"New Event: {event.title}", body,
                           footer_html=_unsubscribe_footer(user.email))
        send_email(user.email, subject, html)


def send_event_registration_confirmation(user, event):
    """Send confirmation email to user after registering for an event."""
    event_url = _url(f'/event/{event.id}')
    subject = f"Registration Confirmed: {event.title} – AU ECED-FLN Platform"

    event_date = event.start_date.strftime('%B %d, %Y at %H:%M UTC')
    if event.end_date:
        event_date += f" - {event.end_date.strftime('%B %d, %Y at %H:%M UTC')}"

    meeting_link_html = ""
    if event.meeting_link:
        meeting_link_html = f'<p style="margin:4px 0;"><strong>Meeting Link:</strong> <a href="{event.meeting_link}" style="color:#007451;">Join here</a></p>'

    body = f"""
        <p>Dear {user.name},</p>
        <p>You have successfully registered for the following event:</p>
        {_info_box(f'<h3 style="margin:0 0 8px;">{event.title}</h3><p style="margin:4px 0;"><strong>Date:</strong> {event_date}</p>{meeting_link_html}')}
        {_btn(event_url, "View Event Details")}
        <p style="color:#666;font-size:0.9em;">We'll send you a reminder closer to the date.</p>
    """
    html = _base_email("Event Registration Confirmed", body)
    send_email(user.email, subject, html)


def send_custom_bulk_email(to_email, name, subject, body_text):
    """
    Send a custom notification email using the platform's standard template.
    body_text is rendered as plain paragraphs (newlines become paragraph tags).
    """
    body_html = f"<p>Dear {name},</p>"
    body_html += "".join(
        f'<p style="margin:0 0 10px;">{line}</p>' if line.strip() else '<br>'
        for line in body_text.splitlines()
    )
    html = _base_email("AU ECED-FLN Cluster Platform", body_html,
                       footer_html=_unsubscribe_footer(to_email))
    send_email(to_email, subject, html)


def send_single_initiative_notification(initiative_data, users):
    """
    Send a single-initiative notification email to all subscribed members.
    initiative_data: dict with keys: title, short_description, url
    """
    if not users or not initiative_data:
        return

    title = initiative_data['title']
    url = initiative_data['url']
    desc = initiative_data.get('short_description', '')
    subject = "New Initiative Published – AU ECED-FLN Platform"

    desc_block = (
        f'<p style="color:#555;font-size:0.95em;line-height:1.6;margin:8px 0 0;">{desc}</p>'
        if desc else ""
    )

    for user in users:
        body = f"""
            <p>A new initiative has just been published on the platform:</p>
            {_info_box(f'<p style="margin:0;font-size:1.05em;font-weight:bold;color:#333;">{title}</p>{desc_block}')}
            {_btn(url, "Read Initiative →")}
            <p style="color:#666;font-size:0.88em;margin:0;">
                You are receiving this because you are a member of the AU&nbsp;ECED-FLN Cluster Platform.
            </p>
        """
        html = _base_email("New Initiative Published", body,
                           footer_html=_unsubscribe_footer(user.email))
        send_email(user.email, subject, html)


def send_bulk_initiatives_digest(initiatives_data, users):
    """
    Send a digest email listing multiple newly published initiatives to all subscribed members.
    Each item in initiatives_data is a dict with keys: title, short_description, url
    """
    if not users or not initiatives_data:
        return

    count = len(initiatives_data)
    subject = f"{count} New Initiative{'s' if count != 1 else ''} on the AU ECED-FLN Platform"

    for user in users:
        items_html = ""
        for item in initiatives_data:
            desc_block = (
                f'<p style="margin:6px 0 0;color:#555;font-size:0.9em;line-height:1.5;">'
                f'{item["short_description"]}</p>'
                if item.get("short_description") else ""
            )
            items_html += f"""
            <div style="margin-bottom:12px;padding:16px 18px;background:#f8f9fa;
                        border-left:4px solid #007451;border-radius:4px;">
                <a href="{item['url']}"
                   style="font-size:1em;font-weight:bold;color:#007451;text-decoration:none;line-height:1.4;">
                    {item['title']}
                </a>
                {desc_block}
                <p style="margin:10px 0 0;">
                    <a href="{item['url']}"
                       style="font-size:0.85em;color:#007451;text-decoration:none;font-weight:bold;">
                        Read more →
                    </a>
                </p>
            </div>"""

        body = f"""
            <p>The following initiative{'s have' if count != 1 else ' has'} just been
            published on the AU&nbsp;ECED-FLN Cluster Platform:</p>
            {items_html}
            <p style="color:#666;font-size:0.88em;margin:20px 0 0;">
                You are receiving this because you are a member of the AU&nbsp;ECED-FLN Cluster Platform.
            </p>
        """
        html = _base_email(
            f"{count} New Initiative{'s' if count != 1 else ''} Published",
            body,
            footer_html=_unsubscribe_footer(user.email)
        )
        send_email(user.email, subject, html)
