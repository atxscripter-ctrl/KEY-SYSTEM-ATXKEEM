import os
import json
import time
import sqlite3
import secrets
import random
import string
from functools import wraps
from flask import Flask, render_template, request, jsonify, session, g
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.getenv("atxscripter", secrets.token_hex(32))
app.permanent_session_lifetime = 60 * 60 * 24 * 30

# ==================== CONFIG ====================
DATA_DIR = os.getenv("DATA_DIR", "/data")
KEYS_FILE = os.path.join(DATA_DIR, "keys.json")
SCRIPTS_FILE = os.path.join(DATA_DIR, "scripts.json")
PASTES_FILE = os.path.join(DATA_DIR, "pastes.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
USERS_DB = os.path.join(DATA_DIR, "users.db")

TELEGRAM_CHANNEL = "https://t.me/+_eEH_XgASVFhNmNl"
TELEGRAM_PREMIUM = "https://t.me/AntraxdevZ"

# Fallback URL used only on first boot if config.json doesn't have one yet.
DEFAULT_SCRIPT_PAYLOAD_URL = os.getenv(
    "SCRIPT_PAYLOAD_URL",
    "https://pastebin.com/raw/V4jrqcQz"
)

# Bootstrap admin — created/updated on every startup.
ADMIN_USERNAME = "antrax"
ADMIN_PASSWORD = "antraxdevzagodz"

KEY_VALID_HOURS = 12
TASK_COOLDOWN_SECONDS = 30
PASTE_MAX_LENGTH = 100000


# ==================== STORAGE ====================
def ensure_storage():
    os.makedirs(DATA_DIR, exist_ok=True)
    for f in (KEYS_FILE, SCRIPTS_FILE, PASTES_FILE):
        if not os.path.exists(f):
            with open(f, "w") as fp:
                json.dump({}, fp)
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as fp:
            json.dump({"script_payload_url": DEFAULT_SCRIPT_PAYLOAD_URL}, fp, indent=2)
    init_db()


def _load(path):
    ensure_storage()
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def _save(path, data):
    ensure_storage()
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_keys():     return _load(KEYS_FILE)
def save_keys(d):    _save(KEYS_FILE, d)
def load_scripts():  return _load(SCRIPTS_FILE)
def save_scripts(d): _save(SCRIPTS_FILE, d)
def load_pastes():   return _load(PASTES_FILE)
def save_pastes(d):  _save(PASTES_FILE, d)
def load_config():   return _load(CONFIG_FILE)


def save_config(data):
    _save(CONFIG_FILE, data)


def get_script_payload_url():
    cfg = load_config()
    url = (cfg.get("script_payload_url") or "").strip()
    return url or DEFAULT_SCRIPT_PAYLOAD_URL


# ==================== DB ====================
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(USERS_DB)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(USERS_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created INTEGER NOT NULL,
            paste_count INTEGER NOT NULL DEFAULT 0,
            is_admin INTEGER NOT NULL DEFAULT 0
        )
    """)
    # Safe migration: add is_admin if it doesn't exist yet (older DBs).
    try:
        conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()
    seed_admin()


def seed_admin():
    """Ensure the bootstrap admin account exists and has the right password/flag."""
    conn = sqlite3.connect(USERS_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM users WHERE username = ?", (ADMIN_USERNAME,)).fetchone()
    now = int(time.time())
    if row is None:
        conn.execute(
            "INSERT INTO users (username, password_hash, created, paste_count, is_admin) VALUES (?, ?, ?, 0, 1)",
            (ADMIN_USERNAME, generate_password_hash(ADMIN_PASSWORD), now),
        )
    else:
        conn.execute(
            "UPDATE users SET password_hash = ?, is_admin = 1 WHERE username = ?",
            (generate_password_hash(ADMIN_PASSWORD), ADMIN_USERNAME),
        )
    conn.commit()
    conn.close()


def find_user_by_username(username):
    db = get_db()
    return db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def find_user_by_id(uid):
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


def create_user(username, password):
    db = get_db()
    db.execute(
        "INSERT INTO users (username, password_hash, created) VALUES (?, ?, ?)",
        (username, generate_password_hash(password), int(time.time())),
    )
    db.commit()
    return db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def bump_paste_count(uid):
    db = get_db()
    db.execute("UPDATE users SET paste_count = paste_count + 1 WHERE id = ?", (uid,))
    db.commit()


# ==================== AUTH ====================
def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return find_user_by_id(uid)


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("user_id"):
            return jsonify({"success": False, "error": "Login required"}), 401
        return fn(*a, **kw)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        u = current_user()
        if not u:
            return jsonify({"success": False, "error": "Login required"}), 401
        if not u["is_admin"]:
            return jsonify({"success": False, "error": "Admin only"}), 403
        return fn(*a, **kw)
    return wrapper


def valid_username(u):
    if not (3 <= len(u) <= 20):
        return False
    return all(c.isalnum() or c == "_" for c in u)


def user_public(u):
    if not u:
        return None
    return {
        "id": u["id"],
        "username": u["username"],
        "created": u["created"],
        "paste_count": u["paste_count"],
        "is_admin": bool(u["is_admin"]),
    }


# ==================== IDS ====================
def gen_key():
    return "ATX-" + "-".join(secrets.token_hex(2).upper() for _ in range(3))


def gen_script_id():
    return "SC-" + secrets.token_hex(4).upper()


def gen_paste_id(length=8):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ==================== CAPTCHA ====================
def gen_math_challenge():
    ops = [
        ("+", lambda a, b: a + b),
        ("-", lambda a, b: a - b),
        ("x", lambda a, b: a * b),
    ]
    op_sym, op_fn = random.choice(ops)
    if op_sym == "x":
        a, b = random.randint(2, 12), random.randint(2, 12)
    else:
        a = random.randint(10, 50)
        b = random.randint(5, 30)
        if op_sym == "-" and b > a:
            a, b = b, a
    return {"question": f"What is {a} {op_sym} {b}?", "answer": str(op_fn(a, b))}


def gen_text_challenge():
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    code = "".join(random.choices(chars, k=5))
    return {"question": f"Type this code: {code}", "answer": code}


def gen_sequence_challenge():
    start = random.randint(1, 5)
    step = random.randint(2, 4)
    seq = [start + step * i for i in range(4)]
    answer = start + step * 4
    return {"question": f"What comes next? {', '.join(map(str, seq))}, ?", "answer": str(answer)}


def gen_challenge():
    return random.choice([gen_math_challenge, gen_text_challenge, gen_sequence_challenge])()


# ==================== SCRIPT TEMPLATE ====================
def build_script_payload():
    """
    Exact format — admin-editable URL, no extra params.
    loadstring(game:HttpGet("<URL>"))()
    """
    url = get_script_payload_url()
    return f'loadstring(game:HttpGet("{url}"))()'


# ==================== ROUTES ====================
@app.route("/")
def index():
    return render_template(
        "index.html",
        telegram_channel=TELEGRAM_CHANNEL,
        telegram_premium=TELEGRAM_PREMIUM,
    )


# -------- AUTH --------
@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    data = request.get_json() or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()

    if not valid_username(username):
        return jsonify({"success": False, "error": "Username must be 3-20 chars, letters/numbers/underscore only"}), 400
    if len(password) < 6:
        return jsonify({"success": False, "error": "Password must be at least 6 characters"}), 400
    if find_user_by_username(username):
        return jsonify({"success": False, "error": "Username already taken"}), 409

    user = create_user(username, password)
    session.permanent = True
    session["user_id"] = user["id"]
    return jsonify({"success": True, "user": user_public(user)})


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.get_json() or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()

    if not username or not password:
        return jsonify({"success": False, "error": "Username and password required"}), 400

    user = find_user_by_username(username)
    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"success": False, "error": "Invalid credentials"}), 401

    session.permanent = True
    session["user_id"] = user["id"]
    return jsonify({"success": True, "user": user_public(user)})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.pop("user_id", None)
    return jsonify({"success": True})


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    return jsonify({"success": True, "user": user_public(current_user())})


# -------- CAPTCHA --------
@app.route("/api/get-challenge", methods=["POST"])
def get_challenge():
    c = gen_challenge()
    session["captcha_answer"] = c["answer"].upper()
    session["captcha_time"] = time.time()
    session["captcha_solved"] = False
    return jsonify({"success": True, "question": c["question"]})


@app.route("/api/verify-human", methods=["POST"])
def verify_human():
    data = request.get_json() or {}
    answer = data.get("answer", "").strip().upper()
    expected = session.get("captcha_answer", "")
    challenge_time = session.get("captcha_time", 0)

    if not expected:
        return jsonify({"success": False, "error": "No challenge. Get a new one."}), 400
    if time.time() - challenge_time > 300:
        return jsonify({"success": False, "error": "Challenge expired. Get a new one."}), 410
    if answer != expected:
        session.pop("captcha_answer", None)
        return jsonify({"success": False, "error": "Wrong answer. Try again."}), 400

    session["captcha_solved"] = True
    session["captcha_solved_time"] = time.time()
    session.pop("captcha_answer", None)
    return jsonify({"success": True})


# -------- KEY --------
@app.route("/api/generate-key", methods=["POST"])
def generate_key():
    if not session.get("captcha_solved"):
        return jsonify({"success": False, "error": "Complete verification first"}), 403

    elapsed = time.time() - session.get("captcha_solved_time", 0)
    if elapsed < TASK_COOLDOWN_SECONDS:
        return jsonify({"success": False, "error": f"Wait {int(TASK_COOLDOWN_SECONDS - elapsed)}s"}), 429

    keys = load_keys()
    new_key = gen_key()
    now = int(time.time())
    keys[new_key] = {"created": now, "expires": now + KEY_VALID_HOURS * 3600}
    save_keys(keys)

    session.pop("captcha_solved", None)
    session.pop("captcha_solved_time", None)
    return jsonify({"success": True, "key": new_key, "expires_in_hours": KEY_VALID_HOURS})


@app.route("/api/validate-key", methods=["GET", "POST"])
def validate_key():
    if request.method == "GET":
        key = request.args.get("key", "").strip()
    else:
        key = (request.get_json() or {}).get("key", "").strip()

    if not key:
        return jsonify({"valid": False, "error": "No key"}), 400

    keys = load_keys()
    entry = keys.get(key)
    if not entry:
        return jsonify({"valid": False, "error": "Invalid key"}), 404
    if entry.get("expires", 0) < time.time():
        return jsonify({"valid": False, "error": "Key expired"}), 410

    return jsonify({"valid": True, "expires": entry.get("expires")})


# -------- SCRIPT --------
@app.route("/api/generate-script", methods=["POST"])
def generate_script():
    if not session.get("captcha_solved"):
        return jsonify({"success": False, "error": "Complete verification first"}), 403

    elapsed = time.time() - session.get("captcha_solved_time", 0)
    if elapsed < TASK_COOLDOWN_SECONDS:
        return jsonify({"success": False, "error": f"Wait {int(TASK_COOLDOWN_SECONDS - elapsed)}s"}), 429

    scripts = load_scripts()
    script_id = gen_script_id()
    now = int(time.time())
    scripts[script_id] = {"created": now}
    save_scripts(scripts)

    payload = build_script_payload()

    session.pop("captcha_solved", None)
    session.pop("captcha_solved_time", None)
    return jsonify({
        "success": True,
        "script_id": script_id,
        "payload": payload,
    })


# -------- ADMIN --------
@app.route("/api/admin/script-url", methods=["GET"])
@admin_required
def admin_get_script_url():
    return jsonify({
        "success": True,
        "url": get_script_payload_url(),
        "payload_preview": build_script_payload(),
    })


@app.route("/api/admin/script-url", methods=["POST"])
@admin_required
def admin_set_script_url():
    data = request.get_json() or {}
    url = (data.get("url") or "").strip()

    if not url:
        return jsonify({"success": False, "error": "URL required"}), 400
    if len(url) > 500:
        return jsonify({"success": False, "error": "URL too long"}), 400
    if not (url.startswith("http://") or url.startswith("https://")):
        return jsonify({"success": False, "error": "URL must start with http:// or https://"}), 400

    cfg = load_config()
    cfg["script_payload_url"] = url
    save_config(cfg)

    return jsonify({
        "success": True,
        "url": url,
        "payload_preview": build_script_payload(),
    })


# -------- PASTEBIN --------
@app.route("/api/paste/create", methods=["POST"])
@login_required
def paste_create():
    data = request.get_json() or {}
    content = (data.get("content") or "").strip()
    title = (data.get("title") or "").strip()[:120]
    syntax = (data.get("syntax") or "text").strip()[:24]

    if not content:
        return jsonify({"success": False, "error": "Content required"}), 400
    if len(content) > PASTE_MAX_LENGTH:
        return jsonify({"success": False, "error": f"Max {PASTE_MAX_LENGTH} characters"}), 413

    pastes = load_pastes()
    pid = gen_paste_id()
    while pid in pastes:
        pid = gen_paste_id()

    now = int(time.time())
    uid = session["user_id"]
    user = find_user_by_id(uid)

    pastes[pid] = {
        "title": title,
        "content": content,
        "syntax": syntax,
        "created": now,
        "views": 0,
        "user_id": uid,
        "username": user["username"] if user else "anon",
        "ip": request.remote_addr,
    }
    save_pastes(pastes)
    bump_paste_count(uid)

    return jsonify({
        "success": True,
        "id": pid,
        "url": f"/paste/{pid}",
        "raw_url": f"/raw/{pid}",
    })


@app.route("/api/paste/mine", methods=["GET"])
@login_required
def paste_mine():
    uid = session["user_id"]
    pastes = load_pastes()
    mine = [
        {
            "id": pid,
            "title": v.get("title", ""),
            "syntax": v.get("syntax", "text"),
            "created": v.get("created"),
            "views": v.get("views", 0),
        }
        for pid, v in pastes.items()
        if v.get("user_id") == uid
    ]
    mine.sort(key=lambda x: x.get("created", 0), reverse=True)
    return jsonify({"success": True, "pastes": mine})


@app.route("/api/paste/delete/<pid>", methods=["DELETE", "POST"])
@login_required
def paste_delete(pid):
    uid = session["user_id"]
    user = find_user_by_id(uid)
    pastes = load_pastes()
    entry = pastes.get(pid)
    if not entry:
        return jsonify({"success": False, "error": "Not found"}), 404
    if entry.get("user_id") != uid and not (user and user["is_admin"]):
        return jsonify({"success": False, "error": "Not yours"}), 403
    del pastes[pid]
    save_pastes(pastes)
    return jsonify({"success": True})


@app.route("/api/paste/get/<pid>", methods=["GET"])
def paste_get(pid):
    pastes = load_pastes()
    entry = pastes.get(pid)
    if not entry:
        return jsonify({"success": False, "error": "Paste not found"}), 404

    entry["views"] = entry.get("views", 0) + 1
    pastes[pid] = entry
    save_pastes(pastes)

    return jsonify({
        "success": True,
        "id": pid,
        "title": entry.get("title", ""),
        "content": entry.get("content", ""),
        "syntax": entry.get("syntax", "text"),
        "created": entry.get("created"),
        "views": entry["views"],
        "username": entry.get("username", "anon"),
        "raw_url": f"/raw/{pid}",
    })


@app.route("/api/paste/recent", methods=["GET"])
def paste_recent():
    pastes = load_pastes()
    live = [
        {
            "id": pid,
            "title": v.get("title", ""),
            "username": v.get("username", "anon"),
            "created": v.get("created"),
            "views": v.get("views", 0),
        }
        for pid, v in pastes.items()
    ]
    live.sort(key=lambda x: x.get("created", 0), reverse=True)
    return jsonify({"success": True, "pastes": live[:20]})


# -------- RAW --------
@app.route("/api/paste/raw/<pid>", methods=["GET"])
@app.route("/raw/<pid>", methods=["GET"])
def paste_raw(pid):
    pastes = load_pastes()
    entry = pastes.get(pid)
    if not entry:
        return "Not found", 404
    return entry.get("content", ""), 200, {"Content-Type": "text/plain; charset=utf-8"}


# -------- HEALTH --------
@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": int(time.time())})


# ==================== ENTRY ====================
ensure_storage()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)