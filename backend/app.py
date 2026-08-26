"""
Retail News Intelligence Tracker — backend.

Talks to Postgres (Supabase) via psycopg2, using the schema created by
master_events_schema_v3_supabase_migration.sql: companies, analysts,
event_types, observation_statuses, event_reasons, store_events,
scraped_articles, article_marks_v3.

Also serves the frontend (../frontend) as static files, so the deployed
app is a single service — no separate frontend host, no CORS to manage.

Required environment variables (see ../.env.example):
    SUPABASE_DB_HOST
    SUPABASE_DB_PORT      (defaults to 6543 — Supabase's pooler port)
    SUPABASE_DB_USER      (e.g. postgres.<project-ref> for the pooler)
    SUPABASE_DB_PASSWORD
    SUPABASE_DB_NAME      (defaults to "postgres")

Run locally:  python app.py         (reads PORT, defaults to 5000)
Run in prod:  gunicorn app:app --bind 0.0.0.0:$PORT
"""

import os
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
import psycopg2.pool
from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.security import check_password_hash, generate_password_hash

COMPLETION_STATUSES = {
    "Add", "Edit", "Already Updated", "Not Relevant",
    "Not Accessible", "Send to the Calling Team",
}

DB_HOST = os.environ.get("SUPABASE_DB_HOST")
DB_PORT = int(os.environ.get("SUPABASE_DB_PORT", "6543"))
DB_USER = os.environ.get("SUPABASE_DB_USER")
DB_PASSWORD = os.environ.get("SUPABASE_DB_PASSWORD")
DB_NAME = os.environ.get("SUPABASE_DB_NAME", "postgres")

if not all([DB_HOST, DB_USER, DB_PASSWORD]):
    raise RuntimeError(
        "Missing Supabase connection settings — set SUPABASE_DB_HOST, "
        "SUPABASE_DB_USER and SUPABASE_DB_PASSWORD (see ../.env.example)."
    )

# A small pool rather than one connection per process: Render/gunicorn can
# run multiple worker threads, and each request borrows+returns a connection.
db_pool = psycopg2.pool.ThreadedConnectionPool(
    1, 5,
    host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
    user=DB_USER, password=DB_PASSWORD,
    sslmode="require", cursor_factory=psycopg2.extras.RealDictCursor,
)

# Idempotent — matches master_events_schema_v3_supabase_migration.sql.
# Kept here too so this app can stand itself up against a brand-new
# Supabase project without a separate manual migration step.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS companies (
    company_id      bigserial PRIMARY KEY,
    company_name    text UNIQUE NOT NULL,
    created_at      timestamptz DEFAULT now()
);

CREATE SEQUENCE IF NOT EXISTS analyst_id_seq START 1;

CREATE TABLE IF NOT EXISTS analysts (
    analyst_id      text PRIMARY KEY DEFAULT lpad(nextval('analyst_id_seq')::text, 4, '0'),
    analyst_name    text NOT NULL,
    email           text UNIQUE,
    role            text NOT NULL DEFAULT 'analyst' CHECK (role IN ('analyst', 'admin')),
    password_hash   text NOT NULL,
    created_at      timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS event_types (
    event_type_id   smallserial PRIMARY KEY,
    name            text UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS observation_statuses (
    status_id       smallserial PRIMARY KEY,
    event_type_id   smallint NOT NULL REFERENCES event_types(event_type_id),
    label           text NOT NULL,
    UNIQUE (event_type_id, label)
);

CREATE TABLE IF NOT EXISTS event_reasons (
    reason_id       smallserial PRIMARY KEY,
    label           text UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS store_events (
    event_id               bigserial PRIMARY KEY,
    source                  text NOT NULL,
    article_link            text NOT NULL,
    published_date          text,
    source_batch            text,
    company_name            text REFERENCES companies(company_name),
    store_name              text,
    event_type_id           smallint REFERENCES event_types(event_type_id),
    observation_status_id   smallint REFERENCES observation_statuses(status_id),
    event_date_raw          text,
    event_date              date,
    reason_id               smallint REFERENCES event_reasons(reason_id),
    address_line1           text,
    city                    text,
    state                   text,
    zip_code                text,
    county                  text,
    comment                 text,
    date_appended           date DEFAULT CURRENT_DATE,
    entered_by              text REFERENCES analysts(analyst_id)
);

CREATE TABLE IF NOT EXISTS scraped_articles (
    id              bigserial PRIMARY KEY,
    source          text NOT NULL,
    link            text,
    title           text,
    published_date  text,
    company_name    text REFERENCES companies(company_name),
    address         text,
    summary         text,
    state           text,
    city            text,
    extra_data      jsonb DEFAULT '{}'::jsonb,
    date_appended   date DEFAULT CURRENT_DATE,
    UNIQUE (source, link)
);

CREATE TABLE IF NOT EXISTS article_marks_v3 (
    article_key       text PRIMARY KEY,
    company_name      text REFERENCES companies(company_name),
    is_done           boolean DEFAULT false,
    marked_by         text REFERENCES analysts(analyst_id),
    marked_at         timestamptz,
    assigned_to       text REFERENCES analysts(analyst_id),
    assigned_by       text REFERENCES analysts(analyst_id),
    assigned_at       timestamptz,
    completion_status text CHECK (completion_status IS NULL OR completion_status IN (
                            'Add', 'Edit', 'Already Updated', 'Not Relevant',
                            'Not Accessible', 'Send to the Calling Team'
                        ))
);
"""


def seed(cur):
    companies = [
        "Aldi", "Whole Foods Market", "TGI Fridays", "Grande Depot",
        "Commissary", "Tango Room", "Muchacho Tex-Mex", "Party City",
        "CVS Pharmacy", "Trader Joe's", "Target",
    ]
    cur.executemany(
        "INSERT INTO companies (company_name) VALUES (%s) ON CONFLICT (company_name) DO NOTHING",
        [(c,) for c in companies],
    )

    cur.executemany(
        """INSERT INTO analysts (analyst_id, analyst_name, email, role, password_hash)
           VALUES (%s,%s,%s,%s,%s) ON CONFLICT (analyst_id) DO NOTHING""",
        [
            ("0000", "Admin", "admin@retailstat.com", "admin", generate_password_hash("admin123")),
            ("0001", "Parth Chudasama", "parthc@retailstat.com", "analyst", generate_password_hash("analyst123")),
            ("0002", "Alex Rivera", "alex.rivera@retailstat.com", "analyst", generate_password_hash("analyst123")),
        ],
    )

    def status_id(label):
        cur.execute("SELECT status_id FROM observation_statuses WHERE label = %s", (label,))
        return cur.fetchone()["status_id"]

    def reason_id(label):
        cur.execute("SELECT reason_id FROM event_reasons WHERE label = %s", (label,))
        return cur.fetchone()["reason_id"]

    events = [
        dict(source="daily_news",
             article_link="https://patch.com/massachusetts/worcester/amp/34548336/longtime-restaurant-chain-closes-central-ma-location",
             published_date="2026-07-28 21:39:11", company_name="TGI Fridays",
             event_type_id=2, observation_status_id=status_id("closed"),
             event_date_raw="Not specified", city="Millbury", state="MA",
             reason_id=reason_id("Restaurant Closing"),
             comment="2026-07-28, According to source - The TGI Fridays at 70 Worcester-Providence Turnpike in Millbury, MA closed after the location's lease term, leaving 3 locations in the state. The chain filed for Chapter 11 bankruptcy in November 2024.",
             entered_by="0001"),
        dict(source="daily_news",
             article_link="https://www.stcloudlive.com/news/grande-depot-announces-permanent-closure-as-of-friday-july-31",
             published_date="2026-07-28 20:10:00", company_name="Grande Depot",
             event_type_id=2, observation_status_id=status_id("permanently closed"),
             event_date_raw="Friday, July 31", reason_id=reason_id("Store Closing"),
             comment="2026-07-28, According to source - Grande Depot announced a permanent closure effective Friday, July 31.",
             entered_by="0002"),
        dict(source="restaurant",
             article_link="https://dallas.culturemap.com/news/restaurants-bars/new-restaurants-opening-frisco/",
             published_date="2026-07-28 21:45:00", company_name="Commissary",
             store_name="Commissary", event_type_id=1, observation_status_id=status_id("planned opening"),
             event_date_raw="Fall 2026", address_line1="3101 Gaylord Pkwy", city="Frisco", state="TX",
             comment="2026-07-28, According to source - Commissary is opening at Hall Park in Frisco in fall 2026, its first expansion north for Headington Companies.",
             entered_by="0001"),
        dict(source="restaurant",
             article_link="https://dallas.culturemap.com/news/restaurants-bars/new-restaurants-opening-frisco/",
             published_date="2026-07-28 21:45:00", company_name="Tango Room",
             store_name="Tango Room", event_type_id=1, observation_status_id=status_id("planned opening"),
             event_date_raw="Fall 2026", address_line1="3101 Gaylord Pkwy", city="Frisco", state="TX",
             comment="2026-07-28, According to source - Tango Room is opening alongside Commissary at Hall Park in Frisco in fall 2026.",
             entered_by="0001"),
        dict(source="restaurant",
             article_link="https://dallas.culturemap.com/news/restaurants-bars/new-restaurants-opening-frisco/",
             published_date="2026-07-28 21:45:00", company_name="Muchacho Tex-Mex",
             store_name="Muchacho Tex-Mex", event_type_id=1, observation_status_id=status_id("set to open"),
             event_date_raw="Fall 2026", city="Frisco", state="TX",
             comment="2026-07-28, According to source - Muchacho Tex-Mex will open in Frisco in fall 2026.",
             entered_by="0002"),
        dict(source="banner",
             article_link="https://example.com/aldi-opens-third-location",
             published_date="2026-07-20 10:00:00", company_name="Aldi",
             event_type_id=1, observation_status_id=status_id("grand opening"),
             event_date_raw="Aug 15, 2026", city="Naperville", state="IL",
             comment="2026-07-20, According to source - Aldi is holding a grand opening for its third Naperville location on Aug 15, 2026.",
             entered_by="0001"),
        dict(source="banner",
             article_link="https://example.com/whole-foods-remodel-uptown",
             published_date="2026-07-18 09:30:00", company_name="Whole Foods Market",
             event_type_id=3, observation_status_id=status_id("under renovation"),
             event_date_raw="Not specified", city="Denver", state="CO",
             comment="2026-07-18, According to source - The Whole Foods Market in Uptown Denver is under renovation.",
             entered_by="0002"),
        dict(source="ct_scoop",
             article_link="https://ctscoop.example.com/party-city-hartford-closing",
             published_date="2026-07-22 12:00:00", company_name="Party City",
             event_type_id=2, observation_status_id=status_id("closing soon"),
             event_date_raw="End of August 2026", city="Hartford", state="CT",
             reason_id=reason_id("Chain Closing"),
             comment="2026-07-22, According to source - The Party City in Hartford is closing soon as part of a wider chain contraction.",
             entered_by="0001"),
        dict(source="daily_news_bankruptcy",
             article_link="https://example.com/cvs-pharmacy-closures-2026",
             published_date="2026-07-25 08:00:00", company_name="CVS Pharmacy",
             event_type_id=2, observation_status_id=status_id("set to close"),
             event_date_raw="Q4 2026", reason_id=reason_id("Mass Closing"),
             comment="2026-07-25, According to source - CVS Pharmacy is set to close a number of locations in Q4 2026 as part of a mass closing plan.",
             entered_by="0002"),
        dict(source="businessdebut",
             article_link="https://businessdebut.example.com/trader-joes-new-store-austin",
             published_date="2026-07-15 14:00:00", company_name="Trader Joe's",
             event_type_id=1, observation_status_id=status_id("opening soon"),
             event_date_raw="September 2026", city="Austin", state="TX",
             comment="2026-07-15, According to source - Trader Joe's is opening a new store in Austin in September 2026.",
             entered_by="0001"),
    ]

    for e in events:
        cols = ", ".join(e.keys())
        placeholders = ", ".join(["%s"] * len(e))
        cur.execute(
            f"INSERT INTO store_events ({cols}) VALUES ({placeholders})",
            list(e.values()),
        )

    raw_rows = [
        dict(source="banner", link="https://example.com/aldi-opens-third-location",
             title="Aldi opens 3rd Naperville location", published_date="2026-07-20",
             company_name="Aldi", summary="Grand opening announced for Aug 15, 2026.",
             extra_data='{"analyst": "J. Smith", "industry": "Grocery", "type": "Opening"}'),
        dict(source="warn", link=None, title=None, published_date="2026-07-25",
             company_name="CVS Pharmacy", state="CA", city="Fresno",
             extra_data='{"layoff_date": "2026-08-01", "employees_affected": 45, "closure_type": "Facility Closing"}'),
        dict(source="bizjournals", link="https://bizjournals.example.com/target-new-format",
             title="Target tests new small-format store", published_date="2026-07-10",
             company_name="Target", summary="Og description text...",
             extra_data='{"full_text": "...", "jsonld_name": "Target"}'),
        dict(source="company_website", link="https://traderjoes.example.com/coming-soon/austin",
             title="Coming Soon - Austin", published_date="2026-07-15",
             company_name="Trader Joe's",
             extra_data='{"opening_date": "2026-09-01", "is_new": true}'),
        dict(source="ct_scoop", link="https://ctscoop.example.com/party-city-hartford-closing",
             title="Party City Hartford closing soon", published_date="2026-07-22",
             company_name="Party City", summary="Chain contraction continues in CT."),
        dict(source="restaurant", link="https://dallas.culturemap.com/news/restaurants-bars/new-restaurants-opening-frisco/",
             title="8 high-profile Dallas restaurants expanding to Frisco", published_date="2026-07-28",
             company_name="Commissary", summary="Eight Dallas restaurants are expanding to Frisco."),
    ]
    for r in raw_rows:
        cols = ", ".join(r.keys())
        placeholders = ", ".join(["%s"] * len(r))
        cur.execute(
            f"""INSERT INTO scraped_articles ({cols}) VALUES ({placeholders})
                ON CONFLICT (source, link) DO NOTHING""",
            list(r.values()),
        )

    now = datetime.now(timezone.utc)
    cur.executemany(
        """INSERT INTO article_marks_v3 (article_key, company_name, is_done, marked_by, marked_at)
           VALUES (%s,%s,%s,%s,%s) ON CONFLICT (article_key) DO NOTHING""",
        [
            ("https://www.stcloudlive.com/news/grande-depot-announces-permanent-closure-as-of-friday-july-31::Grande Depot",
             "Grande Depot", True, "0002", now),
            ("https://example.com/aldi-opens-third-location::Aldi", "Aldi", True, "0001", now),
        ],
    )


def init_db():
    conn = db_pool.getconn()
    try:
        cur = conn.cursor()
        cur.execute(SCHEMA_SQL)
        cur.execute("SELECT count(*) AS c FROM companies")
        already_seeded = cur.fetchone()["c"] > 0
        if not already_seeded:
            seed(cur)
        conn.commit()
    finally:
        db_pool.putconn(conn)


FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")

app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="")


def get_db():
    if "db" not in g:
        g.db = db_pool.getconn()
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        if exception is not None:
            db.rollback()
        db_pool.putconn(db)


@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/api/summary")
def summary():
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """SELECT et.name AS event_type, COUNT(*) AS cnt
           FROM store_events se JOIN event_types et ON se.event_type_id = et.event_type_id
           GROUP BY et.name"""
    )
    by_type = cur.fetchall()
    cur.execute("SELECT source, COUNT(*) AS cnt FROM store_events GROUP BY source")
    by_source = cur.fetchall()
    cur.execute("SELECT COUNT(*) AS c FROM store_events")
    total_events = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM scraped_articles")
    total_raw = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM companies")
    total_companies = cur.fetchone()["c"]
    return jsonify({
        "total_events": total_events,
        "total_raw_articles": total_raw,
        "total_companies": total_companies,
        "by_event_type": {r["event_type"]: r["cnt"] for r in by_type},
        "by_source": {r["source"]: r["cnt"] for r in by_source},
    })


@app.route("/api/store_events")
def list_store_events():
    source = request.args.get("source")
    db = get_db()
    cur = db.cursor()
    query = """
        SELECT se.*, et.name AS event_type_name, os.label AS status_label, er.label AS reason_label
        FROM store_events se
        LEFT JOIN event_types et ON se.event_type_id = et.event_type_id
        LEFT JOIN observation_statuses os ON se.observation_status_id = os.status_id
        LEFT JOIN event_reasons er ON se.reason_id = er.reason_id
    """
    params = []
    if source:
        query += " WHERE se.source = %s"
        params.append(source)
    query += " ORDER BY se.event_id DESC"
    cur.execute(query, params)
    return jsonify([dict(r) for r in cur.fetchall()])


@app.route("/api/scraped_articles")
def list_scraped_articles():
    source = request.args.get("source")
    db = get_db()
    cur = db.cursor()
    query = "SELECT * FROM scraped_articles"
    params = []
    if source:
        query += " WHERE source = %s"
        params.append(source)
    query += " ORDER BY id DESC"
    cur.execute(query, params)
    return jsonify([dict(r) for r in cur.fetchall()])


@app.route("/api/companies")
def list_companies():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM companies ORDER BY company_name")
    return jsonify([dict(r) for r in cur.fetchall()])


@app.route("/api/analysts")
def list_analysts():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT analyst_id, analyst_name, email, role, created_at FROM analysts ORDER BY analyst_id")
    return jsonify([dict(r) for r in cur.fetchall()])


@app.route("/api/login", methods=["POST", "OPTIONS"])
def login():
    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(force=True) or {}
    identifier = (data.get("identifier") or "").strip()
    password = data.get("password") or ""
    if not identifier or not password:
        return jsonify({"error": "identifier and password are required"}), 400

    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT * FROM analysts WHERE analyst_id = %s OR lower(email) = lower(%s)",
        (identifier, identifier),
    )
    row = cur.fetchone()
    if not row or not check_password_hash(row["password_hash"], password):
        return jsonify({"error": "invalid credentials"}), 401

    return jsonify({
        "analyst_id": row["analyst_id"],
        "analyst_name": row["analyst_name"],
        "email": row["email"],
        "role": row["role"],
    })


@app.route("/api/article_marks", methods=["GET", "POST", "OPTIONS"])
def article_marks():
    if request.method == "OPTIONS":
        return ("", 204)

    db = get_db()
    cur = db.cursor()
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        article_key = data.get("article_key")
        if not article_key:
            return jsonify({"error": "article_key is required"}), 400
        company_name = data.get("company_name")
        completion_status = (data.get("completion_status") or "").strip() or None

        if completion_status:
            if completion_status not in COMPLETION_STATUSES:
                return jsonify({"error": f"invalid completion_status: {completion_status}"}), 400
            is_done = True
            marked_by = data.get("marked_by")
            marked_at = datetime.now(timezone.utc)
        else:
            # Clearing a status back to blank ("reset"): an admin can reset
            # anyone's status; an analyst can only reset a status they
            # themselves set.
            actor_id = data.get("actor_analyst_id")
            cur.execute("SELECT * FROM analysts WHERE analyst_id = %s", (actor_id,))
            actor = cur.fetchone()
            if not actor:
                return jsonify({"error": "unknown actor_analyst_id"}), 401
            if actor["role"] != "admin":
                cur.execute("SELECT marked_by FROM article_marks_v3 WHERE article_key = %s", (article_key,))
                existing = cur.fetchone()
                if not existing or existing["marked_by"] != actor_id:
                    return jsonify({"error": "you can only uncheck a status you set yourself"}), 403
            is_done = False
            marked_by = None
            marked_at = None

        cur.execute(
            """INSERT INTO article_marks_v3
               (article_key, company_name, is_done, marked_by, marked_at, completion_status)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (article_key) DO UPDATE SET
                   company_name = EXCLUDED.company_name,
                   is_done = EXCLUDED.is_done,
                   marked_by = EXCLUDED.marked_by,
                   marked_at = EXCLUDED.marked_at,
                   completion_status = EXCLUDED.completion_status""",
            (article_key, company_name, is_done, marked_by, marked_at, completion_status),
        )
        db.commit()
        cur.execute("SELECT * FROM article_marks_v3 WHERE article_key = %s", (article_key,))
        return jsonify(dict(cur.fetchone()))

    cur.execute("SELECT * FROM article_marks_v3")
    return jsonify([dict(r) for r in cur.fetchall()])


@app.route("/api/article_assignments", methods=["POST", "OPTIONS"])
def article_assignments():
    if request.method == "OPTIONS":
        return ("", 204)

    db = get_db()
    cur = db.cursor()
    data = request.get_json(force=True) or {}
    article_key = data.get("article_key")
    actor_id = data.get("actor_analyst_id")
    assigned_to = data.get("assigned_to") or None
    company_name = data.get("company_name")

    if not article_key or not actor_id:
        return jsonify({"error": "article_key and actor_analyst_id are required"}), 400

    cur.execute("SELECT * FROM analysts WHERE analyst_id = %s", (actor_id,))
    actor = cur.fetchone()
    if not actor:
        return jsonify({"error": "unknown actor_analyst_id"}), 401

    # Non-admins may only assign an article to themselves, or unassign an
    # article that is currently assigned to them.
    if actor["role"] != "admin":
        if assigned_to not in (None, actor_id):
            return jsonify({"error": "analysts may only assign articles to themselves"}), 403
        if assigned_to is None:
            cur.execute("SELECT assigned_to FROM article_marks_v3 WHERE article_key = %s", (article_key,))
            current = cur.fetchone()
            if current and current["assigned_to"] not in (None, actor_id):
                return jsonify({"error": "analysts may only unassign their own assignments"}), 403

    assigned_at = datetime.now(timezone.utc)
    cur.execute(
        """INSERT INTO article_marks_v3 (article_key, company_name, assigned_to, assigned_by, assigned_at)
           VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT (article_key) DO UPDATE SET
               company_name = EXCLUDED.company_name,
               assigned_to = EXCLUDED.assigned_to,
               assigned_by = EXCLUDED.assigned_by,
               assigned_at = EXCLUDED.assigned_at""",
        (article_key, company_name, assigned_to, actor_id, assigned_at),
    )
    db.commit()
    cur.execute("SELECT * FROM article_marks_v3 WHERE article_key = %s", (article_key,))
    return jsonify(dict(cur.fetchone()))


@app.route("/api/analyst_activity")
def analyst_activity():
    """Per-analyst workload: how many store_events rows they entered (extracted),
    how many articles they've marked done, and how many are currently assigned
    to them but still open."""
    db = get_db()
    cur = db.cursor()

    cur.execute(
        "SELECT entered_by, COUNT(*) AS cnt FROM store_events WHERE entered_by IS NOT NULL GROUP BY entered_by"
    )
    entered = {r["entered_by"]: r["cnt"] for r in cur.fetchall()}

    cur.execute(
        """SELECT marked_by, COUNT(*) AS cnt FROM article_marks_v3
           WHERE is_done = true AND marked_by IS NOT NULL GROUP BY marked_by"""
    )
    completed = {r["marked_by"]: r["cnt"] for r in cur.fetchall()}

    cur.execute(
        """SELECT assigned_to, COUNT(*) AS cnt FROM article_marks_v3
           WHERE assigned_to IS NOT NULL AND is_done = false GROUP BY assigned_to"""
    )
    assigned_open = {r["assigned_to"]: r["cnt"] for r in cur.fetchall()}

    cur.execute("SELECT analyst_id, analyst_name, role FROM analysts ORDER BY analyst_id")
    analysts = cur.fetchall()

    rows = [
        {
            "analyst_id": a["analyst_id"],
            "analyst_name": a["analyst_name"],
            "role": a["role"],
            "entered_count": entered.get(a["analyst_id"], 0),
            "completed_count": completed.get(a["analyst_id"], 0),
            "assigned_open_count": assigned_open.get(a["analyst_id"], 0),
        }
        for a in analysts
    ]
    return jsonify(rows)


@app.route("/api/store_events/bulk", methods=["POST", "OPTIONS"])
def bulk_add_store_events():
    """Add one or more store_events rows from a CSV upload or a quick
    "just the URL" add. Only article_link is required per row — everything
    else (company_name, event_type, status, event_date, location) is
    optional and gets attached to a bare-URL row later as it's researched.
    Duplicate (article_link, company_name) pairs already on file are
    skipped rather than double-entered."""
    if request.method == "OPTIONS":
        return ("", 204)

    db = get_db()
    cur = db.cursor()
    data = request.get_json(force=True) or {}
    source = (data.get("source") or "").strip()
    actor_id = data.get("actor_analyst_id")
    incoming_rows = data.get("rows") or []

    if not source:
        return jsonify({"error": "source is required"}), 400
    if not isinstance(incoming_rows, list) or not incoming_rows:
        return jsonify({"error": "rows must be a non-empty list"}), 400

    inserted = 0
    skipped_duplicate = 0
    skipped_invalid = 0
    row_errors = []

    for i, raw in enumerate(incoming_rows):
        article_link = (raw.get("article_link") or "").strip()
        if not article_link:
            skipped_invalid += 1
            row_errors.append(f"row {i + 1}: article_link is required")
            continue

        company_name = (raw.get("company_name") or "").strip() or None

        cur.execute(
            "SELECT 1 FROM store_events WHERE article_link = %s AND company_name IS NOT DISTINCT FROM %s",
            (article_link, company_name),
        )
        if cur.fetchone():
            skipped_duplicate += 1
            continue

        if company_name:
            cur.execute(
                "INSERT INTO companies (company_name) VALUES (%s) ON CONFLICT (company_name) DO NOTHING",
                (company_name,),
            )

        event_type_id = None
        status_id = None
        event_type_name = (raw.get("event_type") or "").strip()
        status_label = (raw.get("status") or "").strip()
        if event_type_name:
            cur.execute("SELECT event_type_id FROM event_types WHERE lower(name) = lower(%s)", (event_type_name,))
            et = cur.fetchone()
            if et:
                event_type_id = et["event_type_id"]
                if status_label:
                    cur.execute(
                        """SELECT status_id FROM observation_statuses
                           WHERE event_type_id = %s AND lower(label) = lower(%s)""",
                        (event_type_id, status_label),
                    )
                    st = cur.fetchone()
                    if st:
                        status_id = st["status_id"]

        location = (raw.get("location") or "").strip()
        city, state = None, None
        if location:
            parts = [p.strip() for p in location.rsplit(",", 1)]
            if len(parts) == 2 and parts[1]:
                city, state = parts
            else:
                city = location

        event_date_raw = (raw.get("event_date") or "").strip() or None

        cur.execute(
            """INSERT INTO store_events
               (source, article_link, company_name, event_type_id, observation_status_id,
                event_date_raw, city, state, entered_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (source, article_link, company_name, event_type_id, status_id,
             event_date_raw, city, state, actor_id),
        )
        inserted += 1

    db.commit()
    return jsonify({
        "inserted": inserted,
        "skipped_duplicate": skipped_duplicate,
        "skipped_invalid": skipped_invalid,
        "errors": row_errors,
    })


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
