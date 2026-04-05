FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# ADD THIS LINE - Create database tables during build
RUN python -c "from app import app, db; app.app_context().push(); db.create_all()"

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD curl -f http://localhost:3000/health || exit 1

CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:3000", "app:app"]
