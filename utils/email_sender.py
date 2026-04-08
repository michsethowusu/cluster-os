import os
import requests
from flask import current_app


def _url(path):
    """Build an absolute URL using APP_URL env var — no Flask request context needed."""
    base = os.environ.get('APP_URL', '').rstrip('/')
    return f"{base}{path}"


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


def send_otp_email(email, otp):
    subject = "Your Login OTP - AU ECED-FLN Platform"
    html = f"""
    <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <h2 style="color: #0066cc;">AU ECED-FLN Cluster Platform</h2>
                <p>Your one-time password (OTP) for login is:</p>
                <div style="background: #f4f4f4; padding: 20px; text-align: center; font-size: 24px;
                            font-weight: bold; letter-spacing: 5px; margin: 20px 0;">
                    {otp}
                </div>
                <p>This code will expire in 10 minutes.</p>
                <p>If you didn't request this code, please ignore this email.</p>
            </div>
        </body>
    </html>
    """
    send_email(email, subject, html)


def send_approval_email(email, initiative_slug=None):
    subject = "Welcome to AU ECED-FLN Cluster Platform"

    login_url = _url('/login')

    initiative_link = ""
    if initiative_slug:
        link = _url(f'/initiative/{initiative_slug}')
        initiative_link = f"""
            <p>Your initiative has also been published. You can view it here:</p>
            <p style="text-align: center; margin: 10px 0 30px;">
                <a href="{link}" style="color: #0066cc;">View your initiative →</a>
            </p>"""

    html = f"""
    <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <h2 style="color: #0066cc;">Welcome to AU ECED-FLN Cluster Platform</h2>
                <p>Your registration has been approved! You can now log in to submit initiatives
                and participate in discussions.</p>
                {initiative_link}
                <p style="text-align: center; margin: 30px 0;">
                    <a href="{login_url}" style="display: inline-block; background: #0066cc; color: white;
                    padding: 12px 30px; text-decoration: none; border-radius: 5px; font-weight: bold;">
                        Log In to the Platform
                    </a>
                </p>
                <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
                <p style="color: #999; font-size: 0.85em;">This email was sent by the AU ECED-FLN Cluster Platform.</p>
            </div>
        </body>
    </html>
    """
    send_email(email, subject, html)


def send_initiative_approved_email(user, initiative_slug, initiative_title):
    """Notify a user that their initiative has been approved and published."""
    link = _url(f'/initiative/{initiative_slug}')
    subject = "Your initiative has been published – AU ECED-FLN Platform"
    html = f"""
    <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <h2 style="color: #0066cc;">Your Initiative Has Been Published</h2>
                <p>Dear {user.name},</p>
                <p>Your initiative <strong>{initiative_title}</strong> has been reviewed and is now
                live on the AU ECED-FLN Cluster Platform.</p>
                <p style="text-align: center; margin: 30px 0;">
                    <a href="{link}" style="display: inline-block; background: #0066cc; color: white;
                    padding: 12px 30px; text-decoration: none; border-radius: 5px; font-weight: bold;">
                        View Your Initiative
                    </a>
                </p>
                <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
                <p style="color: #999; font-size: 0.85em;">This email was sent by the AU ECED-FLN Cluster Platform.</p>
            </div>
        </body>
    </html>
    """
    send_email(user.email, subject, html)


def send_initiative_pending_email(user, initiative_title):
    """Notify a user that their imported initiative is pending review."""
    login_url = _url('/login')
    subject = "Your initiative has been submitted for review – AU ECED-FLN Platform"
    html = f"""
    <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <h2 style="color: #0066cc;">Initiative Submitted for Review</h2>
                <p>Dear {user.name},</p>
                <p>Your initiative <strong>{initiative_title}</strong> has been submitted to the
                AU ECED-FLN Cluster Platform and is currently pending review by our team.</p>
                <p>You will receive another email once it has been approved and published.</p>
                <p style="text-align: center; margin: 30px 0;">
                    <a href="{login_url}" style="display: inline-block; background: #0066cc; color: white;
                    padding: 12px 30px; text-decoration: none; border-radius: 5px; font-weight: bold;">
                        Log In to the Platform
                    </a>
                </p>
                <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
                <p style="color: #999; font-size: 0.85em;">This email was sent by the AU ECED-FLN Cluster Platform.</p>
            </div>
        </body>
    </html>
    """
    send_email(user.email, subject, html)


def send_import_welcome_email(user):
    """Send a welcome email to a member who was imported by admin."""
    login_url = _url('/login')
    subject = "You've been added to the AU ECED-FLN Cluster Platform"
    html = f"""
    <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <h2 style="color: #0066cc;">Welcome to the AU ECED-FLN Cluster Platform</h2>
                <p>Dear {user.name},</p>
                <p>You have been added to the <strong>African Union Early Childhood Education and Development &
                Foundational Learning (ECED-FLN) Cluster Platform</strong> as a member representing
                <strong>{user.organization}</strong>.</p>
                <p>This platform connects experts and organizations across Africa working to accelerate
                Early Childhood Education and Foundational Learning. As a member you can:</p>
                <ul>
                    <li>Share and explore ECED-FLN initiatives from across the continent</li>
                    <li>Participate in the Q&amp;A forum and contribute recommendations</li>
                    <li>Register for cluster events and complete polls</li>
                    <li>Connect with other experts in the network</li>
                </ul>
                <p><strong>To get started, please log in and complete your profile</strong> by adding
                descriptions of the ECED-FLN projects you are currently involved in or have worked on.
                This helps other members find and connect with you based on your areas of expertise.</p>
                <p style="text-align: center; margin: 30px 0;">
                    <a href="{login_url}" style="display: inline-block; background: #0066cc; color: white;
                    padding: 12px 30px; text-decoration: none; border-radius: 5px; font-weight: bold;">
                        Log In to the Platform
                    </a>
                </p>
                <p style="color: #666; font-size: 0.9em;">Your registered email address is: {user.email}<br>
                Use this to log in — you will receive a one-time password (OTP) to your email each time you sign in.</p>
                <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
                <p style="color: #999; font-size: 0.85em;">This email was sent by the AU ECED-FLN Cluster Platform.
                If you believe you received this in error, please ignore it.</p>
            </div>
        </body>
    </html>
    """
    send_email(user.email, subject, html)


def send_invitation_email(email, name):
    """Send an invitation email to someone who hasn't registered yet."""
    register_url = _url('/register')
    subject = "You're invited to join the AU ECED-FLN Cluster Platform"
    html = f"""
    <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <h2 style="color: #0066cc;">You've Been Invited to the AU ECED-FLN Cluster Platform</h2>
                <p>Dear {name},</p>
                <p>You have been invited to join the <strong>African Union Early Childhood Education and
                Development &amp; Foundational Learning (ECED-FLN) Cluster Platform</strong> — a network
                connecting experts and organisations across Africa working to accelerate Early Childhood
                Education and Foundational Learning.</p>
                <p>As a member you will be able to:</p>
                <ul>
                    <li>Share and explore ECED-FLN initiatives from across the continent</li>
                    <li>Participate in the Q&amp;A forum and contribute recommendations</li>
                    <li>Register for cluster events and complete polls</li>
                    <li>Connect with other experts in the network</li>
                </ul>
                <p style="text-align: center; margin: 30px 0;">
                    <a href="{register_url}" style="display: inline-block; background: #0066cc; color: white;
                    padding: 12px 30px; text-decoration: none; border-radius: 5px; font-weight: bold;">
                        Register Now
                    </a>
                </p>
                <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
                <p style="color: #999; font-size: 0.85em;">This invitation was sent by the AU ECED-FLN Cluster Platform.
                If you believe you received this in error, please ignore it.</p>
            </div>
        </body>
    </html>
    """
    send_email(email, subject, html)


def send_project_signup_confirmation(user, project, signed_up_activities):
    """
    Confirm to a member that they have successfully joined a project.
    signed_up_activities: list of ProjectActivity objects they signed up for.
    """
    project_url = _url(f'/project/{project.id}')
    activity_items = "".join(
        f"<li>{a.title}</li>" for a in signed_up_activities
    )
    subject = f"You've joined: {project.title} – AU ECED-FLN Platform"
    html = f"""
    <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <h2 style="color: #0066cc;">You've Joined a Project!</h2>
                <p>Dear {user.name},</p>
                <p>You have successfully signed up to participate in the following project on the
                AU ECED-FLN Cluster Platform:</p>
                <h3 style="margin: 16px 0 8px;">{project.title}</h3>
                <p><strong>Deadline:</strong> {project.deadline.strftime('%B %d, %Y')}</p>
                <p><strong>Activities you signed up for:</strong></p>
                <ul style="margin: 8px 0 16px 20px;">
                    {activity_items}
                </ul>
                <p style="text-align: center; margin: 30px 0;">
                    <a href="{project_url}" style="display: inline-block; background: #0066cc; color: white;
                    padding: 12px 30px; text-decoration: none; border-radius: 5px; font-weight: bold;">
                        View Project
                    </a>
                </p>
                <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
                <p style="color: #999; font-size: 0.85em;">This email was sent by the AU ECED-FLN Cluster Platform.</p>
            </div>
        </body>
    </html>
    """
    send_email(user.email, subject, html)


def send_project_signup_admin_alert(admin_email, user, project, signed_up_activities):
    """
    Notify the admin that a member has signed up for a project.
    admin_email: the admin's email address (string).
    signed_up_activities: list of ProjectActivity objects.
    """
    admin_project_url = _url(f'/admin/project/{project.id}/edit')
    activity_items = "".join(
        f"<li>{a.title}</li>" for a in signed_up_activities
    )
    subject = f"[New Sign-up] {user.name} joined \"{project.title}\""
    html = f"""
    <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <h2 style="color: #0066cc;">New Project Sign-up</h2>
                <p>A member has just signed up for a project on the AU ECED-FLN Cluster Platform.</p>
                <table style="width:100%; border-collapse: collapse; margin: 16px 0;">
                    <tr>
                        <td style="padding: 6px 12px 6px 0; font-weight: bold; width: 130px;">Member</td>
                        <td style="padding: 6px 0;">{user.name} ({user.email})</td>
                    </tr>
                    <tr style="background:#f9f9f9;">
                        <td style="padding: 6px 12px 6px 0; font-weight: bold;">Organisation</td>
                        <td style="padding: 6px 0;">{user.organization}</td>
                    </tr>
                    <tr>
                        <td style="padding: 6px 12px 6px 0; font-weight: bold;">Project</td>
                        <td style="padding: 6px 0;">{project.title}</td>
                    </tr>
                    <tr style="background:#f9f9f9;">
                        <td style="padding: 6px 12px 6px 0; font-weight: bold;">Deadline</td>
                        <td style="padding: 6px 0;">{project.deadline.strftime('%B %d, %Y')}</td>
                    </tr>
                </table>
                <p><strong>Activities selected:</strong></p>
                <ul style="margin: 8px 0 16px 20px;">
                    {activity_items}
                </ul>
                <p style="text-align: center; margin: 30px 0;">
                    <a href="{admin_project_url}" style="display: inline-block; background: #0066cc; color: white;
                    padding: 12px 30px; text-decoration: none; border-radius: 5px; font-weight: bold;">
                        Manage Project in Admin
                    </a>
                </p>
                <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
                <p style="color: #999; font-size: 0.85em;">This is an automated alert from the AU ECED-FLN Cluster Platform.</p>
            </div>
        </body>
    </html>
    """
    send_email(admin_email, subject, html)


def send_project_notification(project):
    """Notify all approved members that a new project has been published."""
    from app import User, app as flask_app
    with flask_app.app_context():
        users = User.query.filter_by(is_approved=True).all()
        project_url = _url(f'/project/{project.id}')
        subject = f"New Project: {project.title}"
        html = f"""
        <html>
            <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                    <h2 style="color: #0066cc;">New Project on the Platform</h2>
                    <p>A new collaborative project has been published on the AU ECED-FLN Cluster Platform:</p>
                    <h3 style="margin: 16px 0 8px;">{project.title}</h3>
                    <p style="color: #555;">{project.description[:300]}{'...' if len(project.description) > 300 else ''}</p>
                    <p><strong>Deadline:</strong> {project.deadline.strftime('%B %d, %Y')}</p>
                    <p style="text-align: center; margin: 30px 0;">
                        <a href="{project_url}" style="display: inline-block; background: #0066cc; color: white;
                        padding: 12px 30px; text-decoration: none; border-radius: 5px; font-weight: bold;">
                            View Project &amp; Join
                        </a>
                    </p>
                    <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
                    <p style="color: #999; font-size: 0.85em;">This email was sent by the AU ECED-FLN Cluster Platform.</p>
                </div>
            </body>
        </html>
        """
        for user in users:
            send_email(user.email, subject, html)


def send_project_approved_email(user, project):
    """Notify the submitter that their project has been approved and published."""
    project_url = _url(f'/project/{project.id}')
    subject = "Your project has been published – AU ECED-FLN Platform"
    html = f"""
    <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <h2 style="color: #0066cc;">Your Project Has Been Published</h2>
                <p>Dear {user.name},</p>
                <p>Your project <strong>{project.title}</strong> has been reviewed and is now
                live on the AU ECED-FLN Cluster Platform. Members can now view it and sign up to participate.</p>
                <p style="text-align: center; margin: 30px 0;">
                    <a href="{project_url}" style="display: inline-block; background: #0066cc; color: white;
                    padding: 12px 30px; text-decoration: none; border-radius: 5px; font-weight: bold;">
                        View Your Project
                    </a>
                </p>
                <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
                <p style="color: #999; font-size: 0.85em;">This email was sent by the AU ECED-FLN Cluster Platform.</p>
            </div>
        </body>
    </html>
    """
    send_email(user.email, subject, html)


def send_event_approved_email(user, event):
    """Notify the submitter that their event has been approved and published."""
    event_url = _url(f'/event/{event.id}')
    subject = "Your event has been published – AU ECED-FLN Platform"
    html = f"""
    <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <h2 style="color: #0066cc;">Your Event Has Been Published</h2>
                <p>Dear {user.name},</p>
                <p>Your event <strong>{event.title}</strong> has been reviewed and is now
                live on the AU ECED-FLN Cluster Platform. All members have been notified and can register.</p>
                <p><strong>Date:</strong> {event.start_date.strftime('%B %d, %Y at %H:%M UTC')}</p>
                <p style="text-align: center; margin: 30px 0;">
                    <a href="{event_url}" style="display: inline-block; background: #0066cc; color: white;
                    padding: 12px 30px; text-decoration: none; border-radius: 5px; font-weight: bold;">
                        View Your Event
                    </a>
                </p>
                <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
                <p style="color: #999; font-size: 0.85em;">This email was sent by the AU ECED-FLN Cluster Platform.</p>
            </div>
        </body>
    </html>
    """
    send_email(user.email, subject, html)


def send_member_notification(subject, html):
    """Send an email notification to all approved members."""
    from app import User, app as flask_app
    with flask_app.app_context():
        users = User.query.filter_by(is_approved=True).all()
        for user in users:
            send_email(user.email, subject, html)


def send_bulk_initiatives_digest(initiatives_data):
    """
    Send a single digest email to all approved members listing multiple newly
    approved initiatives.  Each item in initiatives_data is a dict with keys:
      title, short_description, url
    The title itself is hyperlinked — no separate "Read" button per item.
    """
    from app import User, app as flask_app
    with flask_app.app_context():
        users = User.query.filter_by(is_approved=True).all()
        if not users or not initiatives_data:
            return

        # Build one <li> block per initiative
        items_html = ""
        for item in initiatives_data:
            desc_html = (
                f'<p style="margin:4px 0 0;color:#555;font-size:0.95em;">'
                f'{item["short_description"]}</p>'
                if item.get("short_description") else ""
            )
            items_html += f"""
            <li style="margin-bottom:18px;list-style:none;padding:14px 16px;
                        background:#f8f9fa;border-left:4px solid #0066cc;border-radius:4px;">
                <a href="{item['url']}"
                   style="font-size:1.05em;font-weight:bold;color:#0066cc;text-decoration:none;">
                    {item['title']}
                </a>
                {desc_html}
            </li>"""

        count = len(initiatives_data)
        subject = f"New on the Platform: {count} Initiative{'s' if count != 1 else ''} Published"

        html = f"""
        <html>
            <body style="font-family:Arial,sans-serif;line-height:1.6;color:#333;">
                <div style="max-width:600px;margin:0 auto;padding:20px;">
                    <h2 style="color:#0066cc;">New Initiatives on the AU&nbsp;ECED-FLN Platform</h2>
                    <p>The following {count} initiative{'s have' if count != 1 else ' has'} just been
                    published. Click any title to read it on the platform:</p>
                    <ul style="padding:0;margin:20px 0;">
                        {items_html}
                    </ul>
                    <hr style="border:none;border-top:1px solid #eee;margin:20px 0;">
                    <p style="color:#999;font-size:0.85em;">
                        This email was sent by the AU ECED-FLN Cluster Platform.
                    </p>
                </div>
            </body>
        </html>
        """
        for user in users:
            send_email(user.email, subject, html)


def send_event_notification(event):
    """Send email notification about a new event to all approved members."""
    from app import User, app as flask_app
    with flask_app.app_context():
        users = User.query.filter_by(is_approved=True).all()
        subject = f"New Event: {event.title}"
        event_url = _url(f'/event/{event.id}')

        html = f"""
        <html>
            <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                    <h2 style="color: #0066cc;">{event.title}</h2>
                    <p>{event.description[:500]}</p>
                    <p><strong>Date:</strong> {event.start_date.strftime('%B %d, %Y at %H:%M')}</p>
                    <p><a href="{event_url}" style="display: inline-block; background: #0066cc; color: white;
                    padding: 10px 20px; text-decoration: none; border-radius: 5px;">Register Now</a></p>
                </div>
            </body>
        </html>
        """
        for user in users:
            send_email(user.email, subject, html)
            
            
def send_event_registration_confirmation(user, event):
    """Send confirmation email to user after registering for an event."""
    event_url = _url(f'/event/{event.id}')
    subject = f"Registration Confirmed: {event.title} – AU ECED-FLN Platform"
    
    # Format date nicely
    event_date = event.start_date.strftime('%B %d, %Y at %H:%M UTC')
    if event.end_date:
        event_date += f" - {event.end_date.strftime('%B %d, %Y at %H:%M UTC')}"
    
    html = f"""
    <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <h2 style="color: #0066cc;">Event Registration Confirmed</h2>
                <p>Dear {user.name},</p>
                <p>You have successfully registered for the following event:</p>
                <div style="background: #f8f9fa; padding: 16px; border-left: 4px solid #0066cc; margin: 20px 0;">
                    <h3 style="margin: 0 0 8px 0;">{event.title}</h3>
                    <p style="margin: 4px 0;"><strong>Date:</strong> {event_date}</p>
                    {f'<p style=\"margin: 4px 0;\"><strong>Meeting Link:</strong> <a href=\"{event.meeting_link}\">Join here</a></p>' if event.meeting_link else ''}
                </div>
                <p style="text-align: center; margin: 30px 0;">
                    <a href="{event_url}" style="display: inline-block; background: #0066cc; color: white;
                    padding: 12px 30px; text-decoration: none; border-radius: 5px; font-weight: bold;">
                        View Event Details
                    </a>
                </p>
                <p style="color: #666; font-size: 0.9em;">Add this event to your calendar. We'll also send you a reminder closer to the date.</p>
                <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
                <p style="color: #999; font-size: 0.85em;">This email was sent by the AU ECED-FLN Cluster Platform.</p>
            </div>
        </body>
    </html>
    """
    send_email(user.email, subject, html)
