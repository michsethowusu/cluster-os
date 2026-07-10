from app import app, db

with app.app_context():
    db.create_all()
    with db.engine.connect() as conn:
        conn.execute(db.text('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS password_hash VARCHAR(256)'))
        conn.execute(db.text('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS points INTEGER DEFAULT 0'))
        conn.execute(db.text('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS is_subscribed BOOLEAN DEFAULT true'))
        conn.execute(db.text('ALTER TABLE initiative ADD COLUMN IF NOT EXISTS view_count INTEGER DEFAULT 0'))
        conn.execute(db.text('ALTER TABLE initiative ADD COLUMN IF NOT EXISTS quality_score INTEGER'))
        conn.execute(db.text('ALTER TABLE initiative ADD COLUMN IF NOT EXISTS detected_lang VARCHAR(10)'))
        conn.execute(db.text('ALTER TABLE project ADD COLUMN IF NOT EXISTS is_published BOOLEAN DEFAULT FALSE'))
        conn.execute(db.text('ALTER TABLE project ADD COLUMN IF NOT EXISTS start_date TIMESTAMP'))
        conn.execute(db.text('ALTER TABLE project ADD COLUMN IF NOT EXISTS submitted_by INTEGER'))
        conn.execute(db.text('ALTER TABLE event ADD COLUMN IF NOT EXISTS is_published BOOLEAN DEFAULT FALSE'))
        conn.execute(db.text('ALTER TABLE event ADD COLUMN IF NOT EXISTS submitted_by INTEGER'))
        conn.execute(db.text('ALTER TABLE event ADD COLUMN IF NOT EXISTS zoom_webinar_id VARCHAR(100)'))
        conn.execute(db.text('ALTER TABLE setting ALTER COLUMN value TYPE TEXT'))
        conn.execute(db.text('ALTER TABLE event ADD COLUMN IF NOT EXISTS zoom_recording_url VARCHAR(500)'))
        conn.execute(db.text('ALTER TABLE event ADD COLUMN IF NOT EXISTS meeting_link VARCHAR(500)'))
        conn.execute(db.text('''
            CREATE TABLE IF NOT EXISTS initiative_send_queue (
                id SERIAL PRIMARY KEY,
                initiative_id INTEGER UNIQUE NOT NULL REFERENCES initiative(id) ON DELETE CASCADE,
                queued_at TIMESTAMP DEFAULT NOW(),
                sent_at TIMESTAMP
            )
        '''))
        conn.execute(db.text('''
            CREATE TABLE IF NOT EXISTS blocked_email (
                id SERIAL PRIMARY KEY,
                email VARCHAR(120) UNIQUE NOT NULL,
                blocked_at TIMESTAMP DEFAULT NOW()
            )
        '''))
        conn.execute(db.text('''
            CREATE TABLE IF NOT EXISTS comment (
                id SERIAL PRIMARY KEY,
                initiative_id INTEGER NOT NULL REFERENCES initiative(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
                content TEXT NOT NULL,
                is_approved BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        '''))
        conn.execute(db.text('''
            CREATE TABLE IF NOT EXISTS policy_development (
                id SERIAL PRIMARY KEY,
                source_url VARCHAR(2000) NOT NULL,
                title VARCHAR(300),
                extracted_text TEXT,
                short_summary VARCHAR(500),
                country VARCHAR(100),
                published_date DATE,
                is_published BOOLEAN DEFAULT FALSE,
                processing_status VARCHAR(50) DEFAULT 'pending',
                processing_error VARCHAR(500),
                submitted_by INTEGER REFERENCES "user"(id),
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        '''))
        conn.execute(db.text('''
            CREATE TABLE IF NOT EXISTS policy_tags (
                policy_id INTEGER NOT NULL REFERENCES policy_development(id) ON DELETE CASCADE,
                tag_id INTEGER NOT NULL REFERENCES tag(id) ON DELETE CASCADE,
                PRIMARY KEY (policy_id, tag_id)
            )
        '''))
        conn.execute(db.text('''
            CREATE TABLE IF NOT EXISTS policy_send_queue (
                id SERIAL PRIMARY KEY,
                policy_id INTEGER UNIQUE NOT NULL REFERENCES policy_development(id) ON DELETE CASCADE,
                queued_at TIMESTAMP DEFAULT NOW(),
                sent_at TIMESTAMP
            )
        '''))
        conn.execute(db.text('''
            CREATE TABLE IF NOT EXISTS document_send_queue (
                id SERIAL PRIMARY KEY,
                document_id INTEGER UNIQUE NOT NULL REFERENCES document_library(id) ON DELETE CASCADE,
                queued_at TIMESTAMP DEFAULT NOW(),
                sent_at TIMESTAMP
            )
        '''))
        conn.execute(db.text('ALTER TABLE policy_development ADD COLUMN IF NOT EXISTS view_count INTEGER DEFAULT 0'))
        conn.execute(db.text('ALTER TABLE document_library ADD COLUMN IF NOT EXISTS view_count INTEGER DEFAULT 0'))
        conn.execute(db.text('''
            CREATE TABLE IF NOT EXISTS learn_more_request (
                id              SERIAL PRIMARY KEY,
                requester_id    INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
                initiative_id   INTEGER NOT NULL REFERENCES initiative(id) ON DELETE CASCADE,
                created_at      TIMESTAMP DEFAULT NOW()
            )
        '''))
        conn.execute(db.text('''
            CREATE INDEX IF NOT EXISTS ix_learn_more_requester_initiative_month
            ON learn_more_request (requester_id, initiative_id, created_at)
        '''))
        conn.execute(db.text('''
            CREATE TABLE IF NOT EXISTS page_view (
                id SERIAL PRIMARY KEY,
                path VARCHAR(300) NOT NULL,
                visitor_id VARCHAR(36),
                is_authenticated BOOLEAN DEFAULT FALSE,
                referrer_host VARCHAR(255),
                created_at TIMESTAMP DEFAULT NOW()
            )
        '''))
        conn.execute(db.text('CREATE INDEX IF NOT EXISTS ix_page_view_created_at ON page_view (created_at)'))
        conn.execute(db.text('CREATE INDEX IF NOT EXISTS ix_page_view_visitor_id ON page_view (visitor_id)'))
        conn.execute(db.text('CREATE INDEX IF NOT EXISTS ix_page_view_path ON page_view (path)'))
        conn.execute(db.text('''
            CREATE TABLE IF NOT EXISTS email_template (
                id SERIAL PRIMARY KEY,
                key VARCHAR(80) UNIQUE NOT NULL,
                subject VARCHAR(500) NOT NULL,
                body_html TEXT NOT NULL,
                is_confirmed BOOLEAN DEFAULT FALSE
            )
        '''))
        conn.execute(db.text('''
            CREATE TABLE IF NOT EXISTS certificate (
                id SERIAL PRIMARY KEY,
                user_id INTEGER UNIQUE NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
                token VARCHAR(32) UNIQUE NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        '''))

        # Merge the "Member State" stakeholder type into "Government" — they
        # referred to the same real-world group and both ended up in use.
        # Idempotent: safe to run on every deploy.
        conn.execute(db.text('''
            INSERT INTO stakeholder_type (name, is_member_state, is_active, "order")
            SELECT 'Government', false, true, 0
            WHERE NOT EXISTS (SELECT 1 FROM stakeholder_type WHERE name = 'Government')
        '''))
        conn.execute(db.text('UPDATE "user" SET stakeholder_type = \'Government\' WHERE stakeholder_type = \'Member State\''))
        conn.execute(db.text('UPDATE initiative SET stakeholder_type = \'Government\' WHERE stakeholder_type = \'Member State\''))
        conn.execute(db.text('DELETE FROM stakeholder_type WHERE name = \'Member State\''))
        conn.execute(db.text('UPDATE stakeholder_type SET is_member_state = false'))

        # Contributor certificates are only for members with a published initiative
        # scoring 3-5. Remove any certificate whose owner no longer qualifies
        # (e.g. issued under the old rule). Idempotent.
        conn.execute(db.text('''
            DELETE FROM certificate
            WHERE user_id NOT IN (
                SELECT user_id FROM initiative
                WHERE is_published = true AND quality_score >= 3
            )
        '''))

        conn.commit()
    print('DB ready.')
