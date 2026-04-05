import os
from dotenv import load_dotenv
load_dotenv()

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'your-secret-key-here-change-in-production'
    
    # Database — Postgres only, fail loudly if not set
    uri = os.environ.get('POSTGRESQL_URL') or os.environ.get('DATABASE_URL')
    if not uri:
        raise RuntimeError("No database URL set. Define POSTGRESQL_URL in environment variables.")
    if uri.startswith("postgres://"):
        uri = uri.replace("postgres://", "postgresql://", 1)
    SQLALCHEMY_DATABASE_URI = uri
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # Email (Brevo API)
    MAIL_DEFAULT_SENDER = os.environ.get('MAIL_DEFAULT_SENDER')
    
    # For url_for(_external=True) in emails
    APP_URL = os.environ.get('APP_URL', '')
    
    # NVIDIA NIM API
    NVIDIA_API_KEY = os.environ.get('NVIDIA_API_KEY')
    NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
    NVIDIA_MODEL = "meta/llama-3.1-70b-instruct"
    
    # Admin
    ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL') or 'admin@au-eced-fln.org'
    
    # Uploads
    UPLOAD_FOLDER = 'static/uploads'
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024
