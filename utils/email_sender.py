import smtplib
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import url_for, current_app

def send_email(to_email, subject, html_content, text_content=None):
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = os.environ.get('MAIL_DEFAULT_SENDER')
    msg['To'] = to_email
    
    if text_content:
        msg.attach(MIMEText(text_content, 'plain'))
    msg.attach(MIMEText(html_content, 'html'))
    
    try:
        server = smtplib.SMTP(os.environ.get('MAIL_SERVER'), int(os.environ.get('MAIL_PORT')), timeout=10)
        server.starttls()
        server.login(os.environ.get('MAIL_USERNAME'), os.environ.get('MAIL_PASSWORD'))
        server.sendmail(msg['From'], [to_email], msg.as_string())
        server.quit()
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
    
    initiative_link = ""
    if initiative_slug:
        with current_app.app_context():
            link = url_for('view_initiative', slug=initiative_slug, _external=True)
            initiative_link = f"<p>You can view your initiative <a href='{link}'>here</a>.</p>"
    
    html = f"""
    <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <h2 style="color: #0066cc;">Welcome to AU ECED-FLN Cluster Platform</h2>
                <p>Your registration has been approved!</p>
                <p>You can now log in to submit initiatives and participate in discussions.</p>
                {initiative_link}
            </div>
        </body>
    </html>
    """
    send_email(email, subject, html)

def send_import_welcome_email(user):
    """Send a welcome email to a member who was imported by admin."""
    login_url = url_for('login', _external=True)
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

def send_member_notification(subject, html):
    """Send an email notification to all approved members."""
    from app import User, app as flask_app
    with flask_app.app_context():
        users = User.query.filter_by(is_approved=True).all()
        for user in users:
            send_email(user.email, subject, html)

def send_event_notification(event):
    """Send email notification about a new event to all approved members."""
    from app import User, app as flask_app
    with flask_app.app_context():
        users = User.query.filter_by(is_approved=True).all()
        subject = f"New Event: {event.title}"
        event_url = url_for('event_detail', id=event.id, _external=True)
        
        html = f"""
        <html>
            <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                    <h2 style="color: #0066cc;">{event.title}</h2>
                    <p>{event.description[:500]}</p>
                    <p><strong>Date:</strong> {event.start_date.strftime('%B %d, %Y at %H:%M')}</p>
                    <p><a href="{event_url}" style="display: inline-block; background: #0066cc; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">Register Now</a></p>
                </div>
            </body>
        </html>
        """
        for user in users:
            send_email(user.email, subject, html)
