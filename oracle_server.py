"""
Sychos Hub — Oracle Server v5.0
Zentraler Server: Login & Register (50 Start-Credits), Credit-System (2-4 dynamisch),
KI-Chat, Chat-Verwaltung (inkl. Umbenennen), Admin-API (Online-User, Credits vergeben).
Serviert zudem das Frontend:  /        -> Sychos Hub   /admin/  -> Admin-Panel
Nur Python-Stdlib. Start:  python oracle_server.py
"""
import os, sys, json, time, uuid, threading, mimetypes, zlib, base64
import sqlite3, urllib.request, urllib.parse, hashlib, hmac, re, math
from http.server import HTTPServer, BaseHTTPRequestHandler
try:
    from http.server import ThreadingHTTPServer as HTTPServerCls
except ImportError:
    HTTPServerCls = HTTPServer
from urllib.parse import urlparse, unquote

# pythonw (versteckter Always-On-Modus) hat kein stdout/stderr -> auf devnull
if getattr(sys, "stdout", None) is None:
    sys.stdout = open(os.devnull, "w")
if getattr(sys, "stderr", None) is None:
    sys.stderr = open(os.devnull, "w")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST = os.environ.get("ORACLE_HOST", "0.0.0.0")
PORT = int(os.environ.get("ORACLE_PORT", "7777"))
DB_PATH = os.environ.get("ORACLE_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "sychos.db"))
# Provider-Keys: fest im Server hinterlegt (kein manuelles Eintragen noetig).
# Env GEMINI_API_KEY / GROQ_API_KEY / CLINE_API_KEY koennen sie bei Bedarf ueberschreiben.
# NIEMALS im Frontend, in Logs oder Fehlermeldungen ausgeben.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AQ.Ab8RN6JC_fpFIoudZSPJyi0oLHbEpFZXt-PdJPBDobUt76TtOQ")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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")
CLINE_API_KEY = os.environ.get("CLINE_API_KEY", "sk_44b7a08f5bbad92391cf22535ff37373db50457d90979e1df3e5d992fb835381")
# Echtes Zahlen: Stripe Secret Key (leer = Test-Modus, keine echte Abbuchung)
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")

# ── Merchant of Record (Paddle / Lemon Squeezy): der Anbieter ist der offizielle
#    Verkaeufer und fuehrt ALLE Steuern weltweit ab (USt / VAT / Sales-Tax). ──
def _kv(s):
    return dict(p.split("=", 1) for p in (s or "").split(",") if "=" in p)

PADDLE_CLIENT_TOKEN = os.environ.get("PADDLE_CLIENT_TOKEN", "live_c3597782b58fb3e865fe78fd68e")
PADDLE_WEBHOOK_SECRET = os.environ.get("PADDLE_WEBHOOK_SECRET",
    "pdl_ntfset_01m381wq8ew7525s3my66ef7v6_W6SoMQT9Deyt3EDkh8KFh0Tzflqw62w/")
PADDLE_PRICES = _kv(os.environ.get("PADDLE_PRICES",
    "basic=pri_01m3818vm4r281eddpyg4a09g1,"
    "pro=pri_01m381a3nb4yfs4vqy4q7c77t9,"
    "max=pri_01m3817vz42r9xn2azg3vc5hkw,"
    "business=pri_01m3816q322wk6jve5jx5vrg9j,"
    "test=pri_01m381373a8gpxz6b2sdxnagxx"))                    # plan=pri_xxx
LEMONSQUEEZY_STORE = os.environ.get("LEMONSQUEEZY_STORE", "")               # Shop-Subdomain
LEMONSQUEEZY_WEBHOOK_SECRET = os.environ.get("LEMONSQUEEZY_WEBHOOK_SECRET", "")
LEMONSQUEEZY_VARIANTS = _kv(os.environ.get("LEMONSQUEEZY_VARIANTS", ""))    # plan=variant_id
PADDLE_LINKS = _kv(os.environ.get("PADDLE_LINKS", ""))    # plan=Payment-Link-URL (direkt zu Paddle)

START_CREDITS = 50.0
START_TOKENS = 25000.0   # Start-Bonus-Tokens fuer neue Accounts
# Passive Gutschrift: alle 7 Stunden bekommen alle User +25 Credits
CREDIT_REGEN_SECONDS = 7 * 3600
CREDIT_REGEN_AMOUNT = 25.0
ONLINE_WINDOW = 300
MAX_MSG_LEN = 8000
MAX_TITLE_LEN = 80
SEND_RATE_LIMIT = 20
LOGIN_RATE_LIMIT = 120     # Login-Versuche pro Minute pro IP (grosszuegig – kein "kann nicht mehr anmelden")
REGISTER_RATE_LIMIT = 12   # Registrierungen pro Minute pro IP (eigener Bucket!)
PWFAIL_LIMIT = 8           # echte Fehlversuche pro Konto ...
PWFAIL_WINDOW = 600        # ... innerhalb 10 Minuten -> dann kurz gesperrt
MAX_ACCOUNTS_PER_IP = int(os.environ.get("MAX_ACCOUNTS_PER_IP", "10"))

# Zentrale Credit-Preise je KI-Stufe (serverseitig verbindlich)
STRENGTHS = {
    "low":    {"name": "Low",    "max_tokens": 512,  "temp": 0.3, "cost": 1},
    "medium": {"name": "Medium", "max_tokens": 2048, "temp": 0.7, "cost": 2},
    "high":   {"name": "High",   "max_tokens": 4096, "temp": 1.0, "cost": 4},
    "extra":  {"name": "Extra",  "max_tokens": 8192, "temp": 1.3, "cost": 6},
}

# KI-Modelle: Anbieter + Kostenfaktor (Preis = Stufen-Cost x Faktor)
MODELS = {
    "gemini-3.6-flash": {"name": "Gemini 3.6 Flash", "provider": "gemini", "factor": 1.0, "vision": True},
    "gemini-2.5-flash": {"name": "Gemini 2.5 Flash", "provider": "gemini", "factor": 1.0, "vision": True},
    "gemini-2.5-lite":  {"name": "Gemini 2.5 Lite (schnell)", "provider": "gemini", "factor": 0.6, "vision": True},
    "gemini-3.6-pro":   {"name": "Gemini 3.6 Pro", "provider": "gemini", "factor": 1.5, "paid": True, "vision": True},
    "gpt-oss-120b":     {"name": "GPT-OSS 120B", "provider": "groq", "factor": 1.0,
                         "api_model": "openai/gpt-oss-120b"},
    "gpt-oss-20b":      {"name": "GPT-OSS 20B", "provider": "groq", "factor": 0.7,
                         "api_model": "openai/gpt-oss-20b"},
    "qwen3-27b":        {"name": "Qwen3 27B", "provider": "groq", "factor": 0.9,
                         "api_model": "qwen/qwen3.8-27b"},
    "llama-3.3-70b":    {"name": "Llama 3.3 70B", "provider": "groq", "factor": 0.8, "vision": True,
                         "api_model": "llama-3.3-70b-versatile"},
    "llama-3.1-8b":     {"name": "Llama 3.1 8B (schnell)", "provider": "groq", "factor": 0.4,
                         "api_model": "llama-3.1-8b-instant"},
    "qwen3-32b":        {"name": "Qwen3 32B", "provider": "groq", "factor": 0.8, "vision": True,
                         "api_model": "qwen/qwen3-32b"},
    "deepseek-r1-70b":  {"name": "DeepSeek R1 70B", "provider": "groq", "factor": 0.9,
                         "api_model": "deepseek-r1-distill-llama-70b"},
    "kimi-k2":          {"name": "Kimi K2", "provider": "groq", "factor": 1.0, "paid": True, "vision": True,
                         "api_model": "moonshotai/kimi-k2-instruct"},
    "nemotron-ultra":   {"name": "Nemotron Ultra 550B", "provider": "cline", "factor": 0.6,
                         "api_model": "nvidia/nemotron-3-ultra-550b-a55b:free"},
    "nemotron-super":   {"name": "Nemotron Super 120B", "provider": "cline", "factor": 0.5,
                         "api_model": "nvidia/nemotron-3-super-120b-a12b:free"},
    "qwen3-27b-free":   {"name": "Qwen3.8 27B", "provider": "cline", "factor": 0.4,
                         "api_model": "qwen/qwen3.8-27b:free"},
    "gemma-4-31b":      {"name": "Gemma 4 31B", "provider": "cline", "factor": 0.3, "vision": True,
                         "api_model": "google/gemma-4-31b-it:free"},
    "north-mini-code":  {"name": "North Mini Code", "provider": "cline", "factor": 0.3,
                         "api_model": "cohere/north-mini-code:free"},
    "llama-3.3-70b-free": {"name": "Llama 3.3 70B (free)", "provider": "cline", "factor": 0.5, "vision": True,
                         "api_model": "meta-llama/llama-3.3-70b-instruct:free"},
    "deepseek-v3-free": {"name": "DeepSeek V3 (free)", "provider": "cline", "factor": 0.5,
                         "api_model": "deepseek/deepseek-chat-v3-0324:free"},
    "deepseek-r1-free": {"name": "DeepSeek R1 (free)", "provider": "cline", "factor": 0.6,
                         "api_model": "deepseek/deepseek-r1-0528:free"},
    "qwen3-235b-free":  {"name": "Qwen3 235B (free)", "provider": "cline", "factor": 0.7, "paid": True, "vision": True,
                         "api_model": "qwen/qwen3-235b-a22b:free"},
    "mistral-small-free": {"name": "Mistral Small 3.1 (free)", "provider": "cline", "factor": 0.4, "vision": True,
                         "api_model": "mistralai/mistral-small-3.1-24b-instruct:free"},
    "phi-4-free":       {"name": "Phi-4 (free)", "provider": "cline", "factor": 0.3,
                         "api_model": "microsoft/phi-4:free"},
    "gemma-3-27b-free": {"name": "Gemma 3 27B (free)", "provider": "cline", "factor": 0.3, "vision": True,
                         "api_model": "google/gemma-3-27b-it:free"},
    "mimo-v26-pro":     {"name": "MiMo v2.6 Pro", "provider": "cline", "factor": 0.7, "paid": True, "vision": True,
                         "api_model": "cline-pass/mimo-v2.6-pro"},
    "mimo-v26-flash":   {"name": "MiMo v2.6 Flash", "provider": "cline", "factor": 0.5, "paid": True, "vision": True,
                         "api_model": "cline-pass/mimo-v2.6-flash"},
    "kimi-k3":          {"name": "Kimi K3", "provider": "cline", "factor": 1.0, "paid": True, "vision": True,
                         "api_model": "cline-pass/kimi-k3"},
    "deepseek-v4-pro":  {"name": "DeepSeek V4 Pro", "provider": "cline", "factor": 0.8, "paid": True,
                         "api_model": "cline-pass/deepseek-v4-pro"},
    "glm-53":           {"name": "GLM 5.3", "provider": "cline", "factor": 0.8, "paid": True, "vision": True,
                         "api_model": "cline-pass/glm-5.3"},
}

# ── Token- & Plan-System (wie Cline): Nutzung in Tokens, Limits in 3 Fenstern ──
TOKEN_WINDOWS = {
    "5h":    {"seconds": 5 * 3600,       "label": "5 Stunden"},
    "week":  {"seconds": 7 * 24 * 3600,  "label": "Woche"},
    "month": {"seconds": 30 * 24 * 3600, "label": "Monat"},
}
PLANS = {
    "free":     {"name": "Free",     "price": 0.0,    "desc": "Zum Ausprobieren",
                 "t5": 40000,    "tweek": 250000,    "tmonth": 750000},
    "test":     {"name": "Test",     "price": 0.10,   "desc": "Zahlung testen – echte Abbuchung",
                 "t5": 5000,     "tweek": 25000,     "tmonth": 75000},
    "basic":    {"name": "Basic",    "price": 6.99,   "desc": "Fuer den Einstieg",
                 "t5": 150000,   "tweek": 1200000,   "tmonth": 4000000},
    "pro":      {"name": "Pro",      "price": 12.99,  "desc": "Fuer jeden Tag",
                 "t5": 400000,   "tweek": 3500000,   "tmonth": 12000000},
    "max":      {"name": "Max",      "price": 39.99,  "desc": "Viel los",
                 "t5": 1200000,  "tweek": 10000000,  "tmonth": 40000000},
    "ultra":    {"name": "Ultra",    "price": 99.99,  "desc": "Fast ohne Grenzen",
                 "t5": 3000000,  "tweek": 25000000,  "tmonth": 100000000},
    "business": {"name": "Business", "price": 199.99, "desc": "Fuer Teams & Firmen",
                 "t5": 8000000,  "tweek": 60000000,  "tmonth": 250000000},
}
PLAN_ORDER = ["free", "test", "basic", "pro", "max", "ultra", "business"]
PLAN_DAYS = 30   # ein gekaufter Plan gilt 30 Tage (monatlich kuendbar)
PROMO_CODES = {"release": 0.20}   # Rabattcode "Release" = -20 %

def effective_plan(user):
    """Aktiver Plan: gekaufter Plan (bis plan_until) > Admin-Paid (= Pro) > free."""
    if not user:
        return "free"
    p = user.get("plan") or "free"
    if p != "free":
        until = user.get("plan_until", 0) or 0
        if until and time.time() > until:
            p = "free"
        else:
            return p
    paid_until = user.get("paid_until", 0) or 0
    if user.get("is_paid") and (not paid_until or time.time() <= paid_until):
        return "pro"
    return "free"

def plan_limits(plan):
    p = PLANS.get(plan, PLANS["free"])
    return {"5h": p["t5"], "week": p["tweek"], "month": p["tmonth"]}

def usage_state(uid, plan):
    """Drei Fenster (5h / Woche / Monat): Verbrauch, Limit und Reset-Zeit."""
    now = time.time()
    lim = plan_limits(plan)
    out = {}
    db = get_db()
    brow = db.execute("SELECT bonus_tokens FROM users WHERE uid=?", (uid,)).fetchone()
    bonus = (brow["bonus_tokens"] if brow else 0) or 0
    for win, info in TOKEN_WINDOWS.items():
        row = db.execute("SELECT start, tokens FROM usage WHERE uid=? AND win=?", (uid, win)).fetchone()
        start = (row["start"] if row else 0) or 0
        used = (row["tokens"] if row else 0) or 0
        if not start or now - start >= info["seconds"]:
            start, used = now, 0.0
            db.execute("INSERT OR REPLACE INTO usage (uid,win,start,tokens) VALUES (?,?,?,?)",
                       (uid, win, start, used))
        out[win] = {"used": used, "limit": lim[win] + bonus,
                    "remaining": max(0.0, lim[win] + bonus - used),
                    "resets_at": start + info["seconds"]}
    db.commit(); db.close()
    return out

def add_usage(uid, tokens):
    """Verbrauchte Tokens in alle drei Fenster eintragen (Fenster laufen automatisch ab)."""
    now = time.time()
    db = get_db()
    for win, info in TOKEN_WINDOWS.items():
        row = db.execute("SELECT start, tokens FROM usage WHERE uid=? AND win=?", (uid, win)).fetchone()
        start = (row["start"] if row else 0) or 0
        used = (row["tokens"] if row else 0) or 0
        if not start or now - start >= info["seconds"]:
            start, used = now, 0.0
        db.execute("INSERT OR REPLACE INTO usage (uid,win,start,tokens) VALUES (?,?,?,?)",
                   (uid, win, start, used + max(0.0, float(tokens))))
    db.commit(); db.close()

def est_tokens(history):
    """Grobe Vorab-Schaetzung (Input + Bilder + Reserve fuer die Antwort)."""
    def _clen(c):
        if isinstance(c, str):
            return len(c)
        n = 0
        for p in c or []:
            n += len(p.get("text") or "") + (1800 if p.get("type") == "image" else 0)
        return n
    chars = sum(_clen(m.get("content")) for m in history)
    return int(chars / 4) + 900

def limit_block(uid, plan, est):
    """None = ok, sonst (win, state) des ersten ueberzogenen Fensters."""
    st = usage_state(uid, plan)
    for win in ("5h", "week", "month"):
        if st[win]["used"] + est > st[win]["limit"]:
            return win, st[win], st
    return None

def fmt_wait(seconds):
    seconds = max(0, int(seconds))
    h, m = seconds // 3600, (seconds % 3600) // 60
    if h:
        return "%d Std %d Min" % (h, m)
    return "%d Min" % max(1, m)

def strength_cost(strength, model_id):
    s = STRENGTHS.get(strength, STRENGTHS["medium"])
    f = MODELS.get(model_id, {}).get("factor", 1.0)
    return max(1, int(round(s["cost"] * f)))

request_times = []
LOAD_LOCK = threading.Lock()

def note_request():
    with LOAD_LOCK:
        now = time.time()
        recent = [t for t in request_times if now - t < 60]
        recent.append(now)
        request_times[:] = recent

def load_factor():
    """Auslastungs-Aufschlag: viele Anfragen pro Minute -> hoehere Kosten."""
    with LOAD_LOCK:
        n = len(request_times)
    if n >= 240:
        return 2.5
    if n >= 120:
        return 2.0
    if n >= 60:
        return 1.5
    if n >= 30:
        return 1.25
    return 1.0

def message_cost(strength, model_id):
    """Endgueltiger Credit-Preis inkl. Auslastungs-Aufschlag."""
    return max(1, int(round(strength_cost(strength, model_id) * load_factor())))

RATE = {}
RATE_LOCK = threading.Lock()

def rate_ok(key, limit, window=60):
    now = time.time()
    with RATE_LOCK:
        lst = [t for t in RATE.get(key, []) if now - t < window]
        if len(lst) >= limit:
            RATE[key] = lst
            return False
        lst.append(now)
        RATE[key] = lst
        return True

START_TIME = time.time()
# ═══════════════════════════════════════════════════════════
#  DATABASE
# ═══════════════════════════════════════════════════════════
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def hash_pw(pw):
    salt = os.urandom(16).hex()
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100000)
    return "pbkdf2$%s$%s" % (salt, dk.hex())

def verify_pw(pw, stored):
    if stored.startswith("pbkdf2$"):
        _, salt, dk = stored.split("$", 2)
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100000).hex()
        return hmac.compare_digest(calc, dk)
    return hmac.compare_digest(hashlib.sha256(pw.encode()).hexdigest(), stored)

def new_token():
    return uuid.uuid4().hex + os.urandom(16).hex()

def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            uid TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT DEFAULT '',
            credits REAL DEFAULT 50.0,
            is_banned INTEGER DEFAULT 0,
            ban_reason TEXT DEFAULT '',
            ban_until REAL DEFAULT 0,
            is_admin INTEGER DEFAULT 0,
            is_paid INTEGER DEFAULT 0,
            reg_ip TEXT DEFAULT '',
            created_at REAL DEFAULT 0,
            last_login REAL DEFAULT 0,
            last_seen REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS chats (
            id TEXT PRIMARY KEY,
            uid TEXT NOT NULL,
            title TEXT DEFAULT 'Neuer Chat',
            model TEXT DEFAULT 'gemini-3.6-flash',
            created_at REAL DEFAULT 0,
            FOREIGN KEY (uid) REFERENCES users(uid)
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            model TEXT DEFAULT '',
            tokens INTEGER DEFAULT 0,
            cost REAL DEFAULT 0,
            created_at REAL DEFAULT 0,
            FOREIGN KEY (chat_id) REFERENCES chats(id)
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            uid TEXT NOT NULL,
            created_at REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS user_keys (
            uid TEXT NOT NULL,
            provider TEXT NOT NULL,
            api_key TEXT NOT NULL,
            created_at REAL DEFAULT 0,
            PRIMARY KEY (uid, provider)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    row = db.execute("SELECT uid FROM users WHERE is_admin=1").fetchone()
    if not row:
        uid = str(uuid.uuid4())
        # Admin-Passwort: Env ORACLE_ADMIN_PASSWORD, sonst "Lenamax5745"
        pw = os.environ.get("ORACLE_ADMIN_PASSWORD", "Lenamax5745")
        db.execute("INSERT INTO users (uid,email,password_hash,display_name,credits,is_admin,created_at) VALUES (?,?,?,?,?,?,?)",
                   (uid, "admin@sychos.net", hash_pw(pw), "Sychos", 99999, 1, time.time()))
        db.commit()
    try:
        db.execute("ALTER TABLE messages ADD COLUMN images TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    db.execute("""
        CREATE TABLE IF NOT EXISTS usage (
            uid TEXT NOT NULL,
            win TEXT NOT NULL,
            start REAL DEFAULT 0,
            tokens REAL DEFAULT 0,
            PRIMARY KEY (uid, win)
        );""")
    db.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT NOT NULL,
            plan TEXT NOT NULL,
            code TEXT DEFAULT '',
            price REAL DEFAULT 0,
            created_at REAL DEFAULT 0
        );""")
    db.execute("""
        CREATE TABLE IF NOT EXISTS warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at REAL DEFAULT 0,
            read INTEGER DEFAULT 0
        );""")
    for col in ("last_login REAL DEFAULT 0", "last_seen REAL DEFAULT 0",
                "ban_reason TEXT DEFAULT ''", "ban_until REAL DEFAULT 0",
                "is_paid INTEGER DEFAULT 0", "reg_ip TEXT DEFAULT ''",
                "paid_until REAL DEFAULT 0", "last_credit REAL DEFAULT 0",
                "plan TEXT DEFAULT 'free'", "plan_until REAL DEFAULT 0",
                "bonus_tokens REAL DEFAULT 0",
                "totp_secret TEXT DEFAULT ''", "totp_on INTEGER DEFAULT 0"):
        try:
            db.execute("ALTER TABLE users ADD COLUMN " + col)
            db.commit()
        except Exception:
            pass
    db.close()

def touch_user(uid):
    """Markiert einen User als aktiv (online)."""
    db = get_db()
    db.execute("UPDATE users SET last_seen=? WHERE uid=?", (time.time(), uid))
    db.commit(); db.close()

# ═══════════════════════════════════════════════════════════
#  KI-PROVIDER
# ═══════════════════════════════════════════════════════════
def mask_key(k):
    """Key unsichtbar machen (fuer Statusanzeigen)."""
    if not k:
        return ""
    return "*" * 8 + k[-4:]

def get_setting(db, key):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""

def resolve_key(db, uid, provider):
    """Key-Auflösung: Admin-Settings (DB) > Env > fest im Code hinterlegter Default."""
    db_key = get_setting(db, provider + "_api_key")
    if db_key:
        return db_key
    return {"gemini": GEMINI_API_KEY, "groq": GROQ_API_KEY, "cline": CLINE_API_KEY}.get(provider, "")

def web_search(query):
    # Kostenlose Websuche (DuckDuckGo + Wikipedia) fuer KI-Kontext. Kein Key noetig.
    out = []
    try:
        url = "https://api.duckduckgo.com/?q=" + urllib.parse.quote(query) + "&format=json&no_html=1&skip_disambig=1"
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read())
        if d.get("AbstractText"):
            out.append("- " + d["AbstractText"][:400] + " (Quelle: " + d.get("AbstractURL", "") + ")")
        for t in (d.get("RelatedTopics") or [])[:5]:
            if isinstance(t, dict) and t.get("Text"):
                out.append("- " + t["Text"][:300])
    except Exception:
        pass
    if len(out) < 2:
        try:
            url = ("https://de.wikipedia.org/w/api.php?action=query&list=search&srsearch="
                   + urllib.parse.quote(query) + "&format=json&utf8=1&srlimit=5")
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=10) as resp:
                d = json.loads(resp.read())
            for r in (d.get("query", {}).get("search") or []):
                snip = re.sub("<[^>]+>", "", r.get("snippet", ""))
                out.append("- " + r.get("title", "") + ": " + snip[:300])
        except Exception:
            pass
    return out

def totp_code(secret, counter=None):
    """6-stelliger TOTP-Code (RFC 6238, SHA-1, 30s) – nur Stdlib."""
    try:
        key = base64.b32decode(secret.upper() + "=" * ((8 - len(secret) % 8) % 8))
    except Exception:
        return ""
    if counter is None:
        counter = int(time.time()) // 30
    h = hmac.new(key, int(counter).to_bytes(8, "big"), hashlib.sha1).digest()
    o = h[-1] & 15
    return "%06d" % ((int.from_bytes(h[o:o + 4], "big") & 0x7FFFFFFF) % 1000000)

def totp_ok(secret, code):
    code = (code or "").replace(" ", "")
    if not secret or len(code) != 6 or not code.isdigit():
        return False
    now = int(time.time()) // 30
    return any(hmac.compare_digest(totp_code(secret, now + off), code) for off in (-1, 0, 1))

def make_pdf(title, text):
    """Minimaler PDF-Writer (A4, Helvetica) – ohne externe Bibliotheken."""
    def esc(s):
        s = str(s).encode("latin-1", "replace").decode("latin-1")
        return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    t = re.sub(r"```[\s\S]*?```", " [Code] ", text or "")
    t = t.replace("**", "").replace("__", "").replace("`", "").replace("#", "")
    lines = []
    for para in ([title or "Sychos Export", ""] + t.split("\n")):
        para = (para or " ").rstrip() or " "
        while len(para) > 92:
            cut = para.rfind(" ", 0, 92)
            cut = cut if cut > 40 else 92
            lines.append(para[:cut])
            para = para[cut:].lstrip()
        lines.append(para)
    per_page = 46
    pages = [lines[i:i + per_page] for i in range(0, len(lines), per_page)] or [[""]]
    page_ids, content_ids, num = [], [], 4
    for _ in pages:
        page_ids.append(num); num += 1
        content_ids.append(num); num += 1
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    def add(n, body):
        offsets[n] = len(out)
        out.extend(("%d 0 obj\n" % n).encode())
        out.extend(body)
        out.extend(b"\nendobj\n")
    add(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    add(2, ("<< /Type /Pages /Kids [%s] /Count %d >>" %
            (" ".join("%d 0 R" % i for i in page_ids), len(pages))).encode())
    add(3, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, plines in enumerate(pages):
        body = ["BT /F1 11 Tf 50 792 Td 14 TL"]
        for ln in plines:
            body.append("(%s) Tj T*" % esc(ln))
        body.append("ET")
        stream = "\n".join(body).encode("latin-1", "replace")
        add(page_ids[i], ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                          "/Contents %d 0 R /Resources << /Font << /F1 3 0 R >> >> >>"
                          % content_ids[i]).encode())
        add(content_ids[i], b"<< /Length " + str(len(stream)).encode() +
            b" >>\nstream\n" + stream + b"\nendstream")
    xref = len(out)
    out.extend(("xref\n0 %d\n0000000000 65535 f \n" % num).encode())
    for i in range(1, num):
        out.extend(("%010d 00000 n \n" % offsets[i]).encode())
    out.extend(("trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (num, xref)).encode())
    return bytes(out)

def gemini_parts(content):
    """History-Inhalt -> Gemini 'parts' (Text + inline Bilder)."""
    if isinstance(content, str):
        return [{"text": content}]
    out = []
    for p in content or []:
        if p.get("type") == "image":
            out.append({"inline_data": {"mime_type": p.get("mime", "image/png"),
                                        "data": p.get("data", "")}})
        else:
            out.append({"text": p.get("text", "")})
    return out or [{"text": ""}]

def oai_content(content):
    """History-Inhalt -> OpenAI-Style 'content' (Text + image_url)."""
    if isinstance(content, str):
        return content
    out = []
    for p in content or []:
        if p.get("type") == "image":
            out.append({"type": "image_url", "image_url": {
                "url": "data:%s;base64,%s" % (p.get("mime", "image/png"), p.get("data", ""))}})
        else:
            out.append({"type": "text", "text": p.get("text", "")})
    return out or ""

def call_gemini(messages, api_key, strength="medium", model="gemini-3.6-flash"):
    """Echter Gemini-Aufruf. Kein Demo-/Mock-Fallback."""
    if not api_key:
        return {"ok": False, "error_type": "missing_key",
                "error": "Kein Gemini-API-Key hinterlegt. Bitte in den Einstellungen einen Key hinterlegen."}
    s = STRENGTHS.get(strength, STRENGTHS["medium"])
    contents = []
    for m in messages:
        role = "user" if m["role"] == "user" else "model"
        contents.append({"role": role, "parts": gemini_parts(m["content"])})
    payload = json.dumps({
        "contents": contents,
        "generationConfig": {"maxOutputTokens": s["max_tokens"], "temperature": s["temp"]},
    }).encode()
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           + model + ":generateContent?key=" + api_key)
    req = urllib.request.Request(url, data=payload,
        headers={"Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            cand = data.get("candidates") or [{}]
            parts = (cand[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts)
            if not text.strip():
                return {"ok": False, "error_type": "api",
                        "error": "Gemini hat eine leere Antwort geliefert."}
            tokens = data.get("usageMetadata", {}).get("totalTokenCount", 0)
            return {"ok": True, "text": text, "tokens": tokens}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"ok": False, "error_type": "invalid_key",
                    "error": "Gemini-API-Key ist ungueltig oder gesperrt. Bitte Key pruefen."}
        if e.code == 429:
            return {"ok": False, "error_type": "rate_limit",
                    "error": "Gemini-Rate-Limit erreicht. Bitte kurz warten und erneut versuchen."}
        return {"ok": False, "error_type": "api",
                "error": "Gemini-Fehler (HTTP %d). Bitte spaeter erneut versuchen." % e.code}
    except Exception:
        return {"ok": False, "error_type": "api",
                "error": "Gemini nicht erreichbar. Bitte spaeter erneut versuchen."}

def call_groq(messages, api_key, strength="medium", model="llama-3.3-70b-versatile"):
    """Echter Groq-Aufruf (OpenAI-kompatibel). Kein Demo-/Mock-Fallback."""
    if not api_key:
        return {"ok": False, "error_type": "missing_key",
                "error": "Kein Groq-API-Key hinterlegt. Bitte in den Einstellungen einen Key hinterlegen."}
    s = STRENGTHS.get(strength, STRENGTHS["medium"])
    msgs = [{"role": ("user" if m["role"] == "user" else "assistant"), "content": oai_content(m["content"])}
            for m in messages]
    effort = {"low": "low", "medium": "medium", "high": "high", "extra": "high"}.get(strength, "medium")
    payload = json.dumps({
        "model": model, "messages": msgs,
        "max_tokens": s["max_tokens"] + 512, "temperature": s["temp"],
        "reasoning_effort": effort,
    }).encode()
    req = urllib.request.Request("https://api.groq.com/openai/v1/chat/completions",
        data=payload, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "application/json",
        })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            msg = (data["choices"][0].get("message") or {})
            text = msg.get("content") or ""
            if not text.strip():
                return {"ok": False, "error_type": "api",
                        "error": "Groq hat eine leere Antwort geliefert. Bitte erneut versuchen."}
            tokens = data.get("usage", {}).get("total_tokens", 0)
            return {"ok": True, "text": text, "tokens": tokens}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"ok": False, "error_type": "invalid_key",
                    "error": "Groq-API-Key ist ungueltig oder gesperrt. Bitte Key pruefen."}
        if e.code == 429:
            return {"ok": False, "error_type": "rate_limit",
                    "error": "Groq-Rate-Limit erreicht. Bitte kurz warten und erneut versuchen."}
        return {"ok": False, "error_type": "api",
                "error": "Groq-Fehler (HTTP %d). Bitte spaeter erneut versuchen." % e.code}
    except Exception:
        return {"ok": False, "error_type": "api",
                "error": "Groq nicht erreichbar. Bitte spaeter erneut versuchen."}

def call_cline(messages, api_key, strength="medium", model="anthropic/claude-sonnet-4.6"):
    """Echter Cline-Aufruf (OpenAI-kompatibel, api.cline.bot)."""
    if not api_key:
        return {"ok": False, "error_type": "missing_key",
                "error": "Kein Cline-API-Key hinterlegt (Admin-Panel -> API-Keys)."}
    s = STRENGTHS.get(strength, STRENGTHS["medium"])
    msgs = [{"role": ("user" if m["role"] == "user" else "assistant"), "content": oai_content(m["content"])}
            for m in messages]
    # Free-Modelle lehnen "temperature" ab (HTTP 500) – nur Basis-Parameter senden
    payload = json.dumps({
        "model": model, "messages": msgs,
        "max_tokens": s["max_tokens"] + 512,
    }).encode()
    def _do_call():
        req2 = urllib.request.Request("https://api.cline.bot/api/v1/chat/completions",
            data=payload, method="POST", headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + api_key,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                "HTTP-Referer": "https://sychos.hub",
                "X-Title": "Sychos",
            })
        with urllib.request.urlopen(req2, timeout=90) as resp:
            data = json.loads(resp.read())
        ch = data.get("choices") or (data.get("data") or {}).get("choices") or []
        msg = (ch[0].get("message") or {}) if ch else {}
        text = msg.get("content") or ""
        return text, data.get("usage", {}).get("total_tokens", 0)

    try:
        text, tokens = _do_call()
        if not text.strip():
            return {"ok": False, "error_type": "api",
                    "error": "Cline hat eine leere Antwort geliefert."}
        return {"ok": True, "text": text, "tokens": tokens}
    except urllib.error.HTTPError as e:
        if e.code == 500:
            # Free-Tier-Flakiness: ein automatischer Wiederholungsversuch
            try:
                time.sleep(1.5)
                text, tokens = _do_call()
                if text.strip():
                    return {"ok": True, "text": text, "tokens": tokens}
            except Exception:
                pass
            return {"ok": False, "error_type": "api",
                    "error": "Free-Modell voruebergehend ueberlastet. Bitte erneut versuchen."}
        if e.code == 402:
            return {"ok": False, "error_type": "insufficient_credits",
                    "error": "Cline-Guthaben aufgebraucht (Cline Credits). Bitte unter app.cline.bot aufladen."}
        if e.code in (401, 403):
            return {"ok": False, "error_type": "invalid_key",
                    "error": "Cline-API-Key ist ungueltig oder gesperrt."}
        if e.code == 429:
            return {"ok": False, "error_type": "rate_limit",
                    "error": "Cline-Rate-Limit erreicht. Bitte kurz warten."}
        return {"ok": False, "error_type": "api",
                "error": "Cline-Fehler (HTTP %d). Bitte spaeter erneut versuchen." % e.code}
    except Exception:
        return {"ok": False, "error_type": "api", "error": "Cline nicht erreichbar."}


# ═══════════════════════════════════════════════════════════
#  STREAMING — Token-fuer-Token (SSE) fuer echtes Live-Tippen
# ═══════════════════════════════════════════════════════════
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

def _http_err(provider, e):
    if provider == "Cline" and e.code == 402:
        return {"error_type": "insufficient_credits",
                "error": "Cline-Guthaben aufgebraucht (Cline Credits). Bitte unter app.cline.bot aufladen."}
    if e.code in (401, 403):
        return {"error_type": "invalid_key",
                "error": provider + "-API-Key ist ungueltig oder gesperrt."}
    if e.code == 429:
        return {"error_type": "rate_limit",
                "error": provider + "-Rate-Limit erreicht. Bitte kurz warten."}
    return {"error_type": "api",
            "error": provider + "-Fehler (HTTP %d). Bitte spaeter erneut versuchen." % e.code}

def stream_gemini(messages, api_key, strength, model):
    if not api_key:
        yield {"type": "error", "error_type": "missing_key",
               "error": "Kein Gemini-API-Key hinterlegt (Admin-Panel -> API-Keys)."}
        return
    s = STRENGTHS.get(strength, STRENGTHS["medium"])
    contents = [{"role": ("user" if m["role"] == "user" else "model"), "parts": gemini_parts(m["content"])}
                for m in messages]
    payload = json.dumps({"contents": contents,
        "generationConfig": {"maxOutputTokens": s["max_tokens"], "temperature": s["temp"]}}).encode()
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           + model + ":streamGenerateContent?alt=sse&key=" + api_key)
    req = urllib.request.Request(url, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": UA}, method="POST")
    got = False
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                blob = line[5:].strip()
                if not blob or blob == "[DONE]":
                    continue
                try:
                    d = json.loads(blob)
                except Exception:
                    continue
                cand = (d.get("candidates") or [{}])[0]
                parts = (cand.get("content") or {}).get("parts") or []
                txt = "".join(p.get("text", "") for p in parts)
                if txt:
                    got = True
                    yield {"type": "delta", "text": txt}
        if got:
            yield {"type": "end"}
            return
    except urllib.error.HTTPError as e:
        yield {"type": "error", **_http_err("Gemini", e)}
        return
    except Exception:
        pass
    r = call_gemini(messages, api_key, strength, model)   # Fallback ohne Stream
    if r["ok"]:
        yield {"type": "delta", "text": r["text"]}
        yield {"type": "end", "tokens": r.get("tokens", 0)}
    else:
        yield {"type": "error", "error_type": r.get("error_type", "api"), "error": r["error"]}

def stream_openai_style(messages, api_key, strength, model, provider, base_url):
    name = "Cline" if provider == "cline" else "Groq"
    if not api_key:
        yield {"type": "error", "error_type": "missing_key",
               "error": "Kein %s-API-Key hinterlegt (Admin-Panel -> API-Keys)." % name}
        return
    s = STRENGTHS.get(strength, STRENGTHS["medium"])
    msgs = [{"role": ("user" if m["role"] == "user" else "assistant"), "content": oai_content(m["content"])}
            for m in messages]
    body = {"model": model, "messages": msgs, "max_tokens": s["max_tokens"] + 512, "stream": True}
    if provider != "cline":
        body["temperature"] = s["temp"]
        body["reasoning_effort"] = {"low": "low", "medium": "medium", "high": "high", "extra": "high"}.get(strength, "medium")
    payload = json.dumps(body).encode()
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + api_key,
               "User-Agent": UA, "Accept": "text/event-stream"}
    if provider == "cline":
        headers["HTTP-Referer"] = "https://sychos.hub"
        headers["X-Title"] = "Sychos"

    def once():
        req = urllib.request.Request(base_url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                blob = line[5:].strip()
                if blob == "[DONE]":
                    break
                try:
                    d = json.loads(blob)
                except Exception:
                    continue
                ch = d.get("choices") or []
                delta = ((ch[0].get("delta") or {}) if ch else {}).get("content") or ""
                if delta:
                    yield delta

    got = False
    try:
        for delta in once():
            got = True
            yield {"type": "delta", "text": delta}
        if got:
            yield {"type": "end"}
            return
    except urllib.error.HTTPError as e:
        yield {"type": "error", **_http_err(name, e)}
        return
    except Exception:
        pass
    r = call_cline(messages, api_key, strength, model) if provider == "cline" \
        else call_groq(messages, api_key, strength, model)
    if r["ok"]:
        yield {"type": "delta", "text": r["text"]}
        yield {"type": "end", "tokens": r.get("tokens", 0)}
    else:
        yield {"type": "error", "error_type": r.get("error_type", "api"), "error": r["error"]}

def stream_provider(messages, api_key, strength, model):
    info = MODELS.get(model, MODELS["gemini-3.6-flash"])
    prov = info["provider"]
    api_model = info.get("api_model", model)
    if prov == "groq":
        gen = stream_openai_style(messages, api_key, strength, api_model, "groq",
                                  "https://api.groq.com/openai/v1/chat/completions")
    elif prov == "cline":
        gen = stream_openai_style(messages, api_key, strength, api_model, "cline",
                                  "https://api.cline.bot/api/v1/chat/completions")
    else:
        gen = stream_gemini(messages, api_key, strength, model)
    for ev in gen:
        yield ev


def call_provider(messages, api_key, strength, model):
    info = MODELS.get(model, MODELS["gemini-3.6-flash"])
    prov = info["provider"]
    api_model = info.get("api_model", model)
    if prov == "groq":
        return call_groq(messages, api_key, strength, api_model)
    if prov == "cline":
        return call_cline(messages, api_key, strength, api_model)
    return call_gemini(messages, api_key, strength, model)

# ═══════════════════════════════════════════════════════════
#  EINGEBETTETES FRONTEND (Single-File-Modus)
# ═══════════════════════════════════════════════════════════
WEB_ASSETS = {
    "index.html": ("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" "8L8j7G6ZAf3/A9kR7Ik=", "text/html; charset=utf-8"),
    "css/style.css": ("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", "text/css; charset=utf-8"),
    "js/app.js": ("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", "application/javascript; charset=utf-8"),
    "admin/index.html": ("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", "text/html; charset=utf-8"),
    "404.html": ("eNqVVtuO2zYQfS/Qf5iqSLDBWrLstTde35qgaVH0oVl00wLpGy2NJNYUKZCULxsE6Ef0c/rWP+mXdEhJvm2KoLuQRHLODOdyOPT8qzdvv333/v47KGwpll9+" "MXdfEEzmiyDFwK8gS923RMsgKZg2aBfBL+++DyfBYV2yEhfBhuO2UtoGkChpURJuy1NbLFLc8ARDP+kBl9xyJkKTMIGLQRRf2kmUUJrEBZZ4Yitlen0JtQ4T" "eoUT5NfxJL6LUw+23ApcjuIR/PPHn/CwTwpl5v1mlcSCyzVoFIuAk3oAhcZsEWRs46aR2eQB2H1FO/GS5dinhetdKYJz1UojoSUmtjNQWFuZab+fkUsmypXK" "BbKKmyhR5f9VNpZZnnhNSLQyRmmec3mw8vkd+4kxw28yVnKxX7ytbcbtdJsX9tU4jme39LykZxLHz1vIvajN9Y9szbRl1w9MmgY9ItSJxvOUm0qw/cJsWRU0" "wRi7F2gKRHsR5Ymgddg51ffLEY083M9oABDJLNxqVsEHNwOolCHSKDl11igdG5zBY8hlirspDGZQICcPaRjHm2LW6LTuTSETuJsBEzyXIbdYmikkxBPUM/i9" "NpZn+7ClzlFQsTTlMp/CcFTtvMGPnV8J0yl8AIs7G3qjR62S7RqWT2F8G1duV0nEaT3nBiG6NZDUK56EK3zkqK+iYS+a9KKb3uDF7GQPlWIXu6to2FRmChum" "r8LQL6Wk4AfbNniq4OxExfBHJNcEK6urwZC86cFwuNm6N01ImaqDYZe56K7VFWgpltBULPEJCKN4hOUM/CGbgtXEh4ppirhVoP1Xa25DnxBjtVrTtsNq1/pK" "ZsjTBtpACpaq7RRi+ndJOsGFuVDbDnySukzwZI0aRoa6R+YaCB5r8mqN+0xTOzAHXJu5+FkP7m7c65ZexI1nVDflArN7T5uPDe5u5DDjM2k0HrfyQ1EsyzvL" "B25x6bO4EipZu/prOplkekJhUXSOAQcm0QwGHZsAVkqnSAkd0LJRgqdP89BiQs1SXhNrb+KD9mdJ0VR/MHAunLHk5YEll5WOblydfY18lTOlyynUVYU6YQYP" "FLis6/nJKAaUxicEHA4d/0aOfjcN+5pchStlrSrdyXWenpui43++Z1lbvIxwFI2d5oW54eTMHEsckQzZu+gKOavIxtCB/7MXXLLMH2SKUavylDHxDE7S5ofU" "qvC9j90dbrDqgn8neKkkOgxtNu93jXDe767flUr37pvyjcuoMYugbZGBb5gXApe+RvBUpNzNThfivE+CT2OI6cHygVobE7BBTRVAeQYvBssHpFMIkieFhRyz" "mnoxYUjQQqrlG46UqJ8VFQ1wxym7qG2rcVVioV8AL9srOfwJ7WM0X+lWnf5+5SgEevS2pnMAhiPU5Qolk9L665xC0bBlmowiFGQ+mverT4fUMiA42J+zTrqy" "EugJK00NR+8PF1Sw/K3Wf/+VrOGxLuGHejXvs0+rH1RYWnJJiq/dN7xnEsVR6ZjBbnT8dvXt+19i/wLI3AeU", "text/html; charset=utf-8"),
    "favicon.svg": ("eNp1Uk1vgzAMvU/af7DSy3ZICCEEOsEOu+y0H8FC+NAYQSEt9N/PoaXSJk0iefbzs7GtFPO5hfV7GOeSdN5PL1G0LAtbEmZdGwnOeYQKAufeLG92LQkHDkri" "R14fHwCK2jTzZqE99KOp3Lur6t6MHvq6JJi6xphF4HKFVZQkRm+DWyKmzt5OYJtmNn6TBZ9qO1hXkkMe57rJSfSPPP4j5+pT1fIuL6LffV37ju6NF85oD0tf" "+64kOBd0pm87f7UdjhwrAk0/DKH0J6/j6la6mCrfAQ75ISUIrmnCUjyC5kxSyXIaJ+gqqiDG+8gkZBjIGPqCHZFMQELKBCgM5YjhiBBmKUYEy/CWGOPIZBSL" "IHekcrsFy4HTFNmEKozif8m+Hri1O9rRhN04+2VKcnLD06F93gl6mzi9E2FNuppK4uxprPchde/0YEDjIiRq9QUxLAZhX8teOWQU4b0g/gAsK5qT", "image/svg+xml"),
}

def get_embedded(rel):
    """Eingebettete Frontend-Datei (gzip+base64), falls keine auf Disk liegt."""
    if not WEB_ASSETS:
        return None
    key = rel.replace("\\", "/").lstrip("/")
    if key in ("", "index.html"):
        key = "index.html"
    item = WEB_ASSETS.get(key)
    if item is None:
        return None
    return zlib.decompress(base64.b64decode(item[0])), item[1]


# ═══════════════════════════════════════════════════════════
#  HTTP HANDLER
# ═══════════════════════════════════════════════════════════
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        try:
            ts = time.strftime("%H:%M:%S")
            sys.stderr.write(f"  [{ts}] {fmt % args}\n")
        except Exception:
            pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With, Accept")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")   # nie alte Frontends im Cache

    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        ln = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(ln) if ln else b""
        self._raw_body = raw                    # fuer Webhook-Signaturpruefung
        try:
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def _get_user(self):
        """Auth per Session-Token (Bearer). Gibt User-Dict oder None zurueck."""
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        token = auth[7:].strip()
        db = get_db()
        row = db.execute("SELECT u.* FROM sessions s JOIN users u ON u.uid=s.uid WHERE s.token=?",
                         (token,)).fetchone()
        user = dict(row) if row else None
        if user:
            # Zeitlich begrenzte Sperre automatisch aufheben
            if user["is_banned"] and user.get("ban_until", 0) and time.time() > user["ban_until"]:
                db.execute("UPDATE users SET is_banned=0, ban_reason='', ban_until=0 WHERE uid=?",
                           (user["uid"],))
                db.commit()
                user["is_banned"] = 0
                user["ban_reason"] = ""
                user["ban_until"] = 0
            # Zeitlich begrenztes Paid laeuft automatisch ab
            if user.get("is_paid") and user.get("paid_until", 0) and time.time() > user["paid_until"]:
                db.execute("UPDATE users SET is_paid=0, paid_until=0 WHERE uid=?", (user["uid"],))
                db.commit()
                user["is_paid"] = 0
                user["paid_until"] = 0
            # Abgelaufene Plaene laufen automatisch aus (zurueck auf Free)
            if user.get("plan", "free") != "free" and user.get("plan_until", 0) \
                    and time.time() > user["plan_until"]:
                db.execute("UPDATE users SET plan='free', plan_until=0 WHERE uid=?", (user["uid"],))
                db.commit()
                user["plan"] = "free"
                user["plan_until"] = 0
            db.execute("UPDATE users SET last_seen=? WHERE uid=?", (time.time(), user["uid"]))
            db.commit()
        db.close()
        return user

    def _ban_info(self, user):
        """Sperr-Details fuer das Frontend-Modal."""
        until = user.get("ban_until", 0) or 0
        return {"banned": True,
                "ban_reason": user.get("ban_reason", ""),
                "ban_until": until,
                "ban_permanent": not bool(until)}

    def _require_user(self, allow_banned=False):
        """Auth + serverseitige Ban-Pruefung. Gibt (user, error) zurueck."""
        user = self._get_user()
        if not user:
            return None, (401, {"ok": False, "error": "Nicht angemeldet"})
        if user["is_banned"] and not allow_banned:
            d = {"ok": False, "error": "Account gesperrt"}
            d.update(self._ban_info(user))
            return None, (403, d)
        return user, None

    def _check_admin(self):
        """Nur angemeldete Admin-User (Session-Token). Kein statischer Admin-Key mehr."""
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        tok = auth[7:].strip()
        if not tok:
            return False
        db = get_db()
        row = db.execute("SELECT u.is_admin FROM sessions s JOIN users u ON u.uid=s.uid WHERE s.token=?",
                         (tok,)).fetchone()
        db.close()
        return bool(row and row["is_admin"])

    def _serve_static(self, path):
        """Liefert Frontend-Dateien aus dem Projekt-Root aus."""
        if path in ("/", "/index.html"):
            path = "/index.html"
        elif path in ("/admin", "/admin/"):
            path = "/admin/index.html"
        rel = unquote(path).lstrip("/")
        full = os.path.normpath(os.path.join(ROOT, rel))
        if not full.startswith(os.path.normpath(ROOT)):
            self._json(403, {"ok": False, "error": "Zugriff verweigert"}); return
        if os.path.isfile(full):
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            with open(full, "rb") as f:
                data = f.read()
        else:
            emb = get_embedded(rel)
            if emb is None:
                # 404-Seite: echter 404-Status mit moderner "Page Not Found"
                nf = get_embedded("404.html")
                if nf and "." not in os.path.basename(rel):
                    data, ctype = nf
                    self.send_response(404)
                    self.send_header("Content-Type", ctype)
                    self._cors()
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self._json(404, {"ok": False, "error": "Nicht gefunden"}); return
            data, ctype = emb
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    # ── GET ───────────────────────────────────────────────
    def do_GET(self):
        note_request()
        path = urlparse(self.path).path

        if path == "/status":
            self._json(200, {"ok": True, "server": "Sychos Oracle", "version": "5.0.0", "build": "24.09.2026-Final",
                "uptime": int(time.time() - START_TIME), "load": len(request_times)}); return

        if path == "/models":
            u = self._get_user()
            plan = effective_plan(u)
            paid_ok = bool(u and (u.get("is_admin") or plan != "free"))
            self._json(200, {"ok": True, "is_paid": paid_ok, "plan": plan, "load_factor": load_factor(),
                "models": [{"id": k, "name": v["name"], "provider": v["provider"],
                            "factor": v["factor"], "paid": bool(v.get("paid")),
                            "vision": bool(v.get("vision")),
                            "locked": bool(v.get("paid")) and not paid_ok}
                           for k, v in MODELS.items()],
                "strengths": [{"id": k, "name": v["name"], "cost": v["cost"],
                               "max_tokens": v["max_tokens"]}
                              for k, v in STRENGTHS.items()]}); return

        # User holt seine offenen Warnungen (Admin-Warnsystem)
        if path == "/warnings":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            db = get_db()
            rows = db.execute("SELECT id,message,created_at FROM warnings WHERE uid=? AND read=0 ORDER BY id",
                              (user["uid"],)).fetchall()
            db.close()
            self._json(200, {"ok": True, "warnings": [dict(r) for r in rows]}); return

        # Plaene & Token-Limits (public – wird im Settings-Planpicker angezeigt)
        if path == "/plans":
            self._json(200, {"ok": True, "days": PLAN_DAYS,
                "payments": bool(STRIPE_SECRET_KEY and STRIPE_PUBLISHABLE_KEY),
                "mor": {
                    "paddle": {"enabled": bool(PADDLE_CLIENT_TOKEN and PADDLE_PRICES),
                               "token": PADDLE_CLIENT_TOKEN, "prices": PADDLE_PRICES,
                               "links": PADDLE_LINKS},
                    "lemonsqueezy": {"enabled": bool(LEMONSQUEEZY_STORE and LEMONSQUEEZY_VARIANTS),
                                     "store": LEMONSQUEEZY_STORE, "variants": LEMONSQUEEZY_VARIANTS}},
                "min_amount": 0.50,
                "plans": [{"id": pid, "name": PLANS[pid]["name"], "price": PLANS[pid]["price"],
                           "desc": PLANS[pid]["desc"],
                           "limits": {"5h": PLANS[pid]["t5"], "week": PLANS[pid]["tweek"],
                                      "month": PLANS[pid]["tmonth"]}}
                          for pid in PLAN_ORDER]}); return

        if path == "/me":
            user = self._get_user()
            if not user:
                self._json(401, {"ok": False, "error": "Nicht angemeldet"}); return
            plan = effective_plan(user)
            d = {"ok": True, "uid": user["uid"], "email": user["email"],
                 "display_name": user["display_name"], "credits": user["credits"],
                 "is_admin": user["is_admin"], "is_paid": bool(user["is_paid"]),
                 "created_at": user.get("created_at", 0),
                 "paid_until": user.get("paid_until", 0) or 0,
                 "plan": plan, "plan_name": PLANS[plan]["name"],
                 "plan_until": user.get("plan_until", 0) or 0,
                 "totp_on": bool(user.get("totp_on")),
                 "usage": usage_state(user["uid"], plan)}
            if user["is_banned"]:
                d.update(self._ban_info(user))
            self._json(200, d); return

        if path == "/chats":
            user = self._get_user()
            if not user:
                self._json(401, {"ok": False, "error": "Nicht angemeldet"}); return
            db = get_db()
            rows = db.execute("SELECT id,title,model,created_at FROM chats WHERE uid=? ORDER BY created_at DESC",
                              (user["uid"],)).fetchall()
            db.close()
            self._json(200, {"ok": True, "chats": [dict(r) for r in rows]}); return

        if path.startswith("/messages/"):
            user = self._get_user()
            if not user:
                self._json(401, {"ok": False, "error": "Nicht angemeldet"}); return
            cid = path.split("/")[2]
            db = get_db()
            own = db.execute("SELECT id FROM chats WHERE id=? AND uid=?", (cid, user["uid"])).fetchone()
            rows = []
            if own:
                rows = db.execute("SELECT role,content,model,cost,created_at,images FROM messages WHERE chat_id=? ORDER BY id",
                                  (cid,)).fetchall()
            db.close()
            if not own:
                self._json(404, {"ok": False, "error": "Chat nicht gefunden"}); return
            out = []
            for r in rows:
                d2 = dict(r)
                try:
                    d2["images"] = json.loads(d2.get("images") or "[]")
                except Exception:
                    d2["images"] = []
                out.append(d2)
            self._json(200, {"ok": True, "messages": out}); return

        # User-eigene Keys: Feature entfernt
        if path == "/settings/keys":
            self._json(410, {"ok": False, "error": "Eigene API-Keys sind entfernt."}); return

        if path == "/admin/users":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            db = get_db()
            db.execute("UPDATE users SET is_paid=0, paid_until=0 WHERE is_paid=1 AND paid_until>0 AND paid_until<?",
                       (time.time(),))
            db.commit()
            rows = db.execute("SELECT uid,email,display_name,credits,bonus_tokens,is_banned,ban_reason,ban_until,is_admin,is_paid,paid_until,created_at,last_seen FROM users").fetchall()
            db.close()
            now = time.time()
            users = []
            for r in rows:
                u = dict(r)
                u["online"] = (now - u.get("last_seen", 0)) < ONLINE_WINDOW
                u["ban_permanent"] = bool(u.get("is_banned")) and not bool(u.get("ban_until", 0))
                users.append(u)
            self._json(200, {"ok": True, "users": users}); return

        if path == "/admin/settings":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            db = get_db()
            res = {"ok": True}
            for prov in ("gemini", "groq", "cline"):
                res[prov + "_key_set"] = bool(resolve_key(db, None, prov))
            db.close()
            self._json(200, res); return

        self._serve_static(path)

    def do_POST(self):
        note_request()
        path = urlparse(self.path).path
        body = self._read_body()
        ip = self.client_address[0] if self.client_address else "?"

        # Register (Start-Tokens) -> Session-Token
        if path == "/register":
            if not rate_ok("reg:" + ip, REGISTER_RATE_LIMIT):
                self._json(429, {"ok": False, "error": "Zu viele Registrierungen. Bitte kurz warten."}); return
            email = body.get("email", "").strip().lower()
            pw = body.get("password", "")
            name = (body.get("display_name", "").strip() or email.split("@")[0])[:40]
            if not email or len(pw) < 3 or "@" not in email:
                self._json(400, {"ok": False, "error": "Gueltige Email und Passwort (min. 3 Zeichen) benoetigt"}); return
            db = get_db()
            n_ip = db.execute("SELECT COUNT(*) AS n FROM users WHERE reg_ip=?", (ip,)).fetchone()["n"]
            if n_ip >= MAX_ACCOUNTS_PER_IP:
                db.close()
                self._json(429, {"ok": False,
                    "error": "Maximale Anzahl von Accounts (" + str(MAX_ACCOUNTS_PER_IP)
                             + ") pro IP erreicht."}); return
            if db.execute("SELECT uid FROM users WHERE email=?", (email,)).fetchone():
                db.close()
                self._json(409, {"ok": False, "error": "Email bereits registriert"}); return
            uid = str(uuid.uuid4())
            token = new_token()
            db.execute("INSERT INTO users (uid,email,password_hash,display_name,credits,bonus_tokens,created_at,reg_ip) VALUES (?,?,?,?,?,?,?,?)",
                       (uid, email, hash_pw(pw), name, START_CREDITS, START_TOKENS, time.time(), ip))
            db.execute("INSERT INTO sessions (token,uid,created_at) VALUES (?,?,?)",
                       (token, uid, time.time()))
            db.commit(); db.close()
            self._json(200, {"ok": True, "token": token, "uid": uid, "display_name": name,
                             "credits": START_CREDITS, "is_admin": 0}); return

        # Login -> Session-Token (auch bei Sperre: Sperr-Modal erscheint im App-Inneren)
        if path == "/login":
            if not rate_ok("login:" + ip, LOGIN_RATE_LIMIT):
                self._json(429, {"ok": False, "error": "Zu viele Versuche. Bitte kurz warten."}); return
            email = body.get("email", "").strip().lower()
            pw = body.get("password", "")
            db = get_db()
            row = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if not row or not verify_pw(pw, row["password_hash"]):
                db.close()
                # Brute-Force-Bremse pro Konto: zaehlt NUR echte Fehlversuche
                if not rate_ok("pwfail:" + email, PWFAIL_LIMIT, window=PWFAIL_WINDOW):
                    self._json(429, {"ok": False, "error": "Zu viele Fehlversuche fuer dieses Konto. Bitte 10 Minuten warten."}); return
                self._json(401, {"ok": False, "error": "Falsche Anmeldedaten"}); return
            user = dict(row)
            # 2FA: wenn aktiv, muss der Authenticator-Code stimmen
            if user.get("totp_on"):
                code = (body.get("code", "") or "").strip()
                if not totp_ok(user.get("totp_secret") or "", code):
                    db.close()
                    self._json(401, {"ok": False, "need_2fa": True,
                        "error": "2FA aktiv – bitte den 6-stelligen Authenticator-Code eingeben."}); return
            if not user["password_hash"].startswith("pbkdf2$"):
                db.execute("UPDATE users SET password_hash=? WHERE uid=?", (hash_pw(pw), user["uid"]))
            if user["is_banned"] and user.get("ban_until", 0) and time.time() > user["ban_until"]:
                db.execute("UPDATE users SET is_banned=0, ban_reason='', ban_until=0 WHERE uid=?",
                           (user["uid"],))
                user["is_banned"] = 0
                user["ban_reason"] = ""
                user["ban_until"] = 0
            token = new_token()
            now = time.time()
            db.execute("INSERT INTO sessions (token,uid,created_at) VALUES (?,?,?)",
                       (token, user["uid"], now))
            db.execute("UPDATE users SET last_login=?, last_seen=? WHERE uid=?", (now, now, user["uid"]))
            db.commit(); db.close()
            d = {"ok": True, "token": token, "uid": user["uid"], "email": user["email"],
                 "display_name": user["display_name"], "credits": user["credits"],
                 "is_admin": user["is_admin"]}
            if user["is_banned"]:
                d.update(self._ban_info(user))
            self._json(200, d); return

        # Logout: Session invalidieren
        if path == "/logout":
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                db = get_db()
                db.execute("DELETE FROM sessions WHERE token=?", (auth[7:].strip(),))
                db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # Eigener API-Key: Feature entfernt (Keys verwaltet ausschliesslich der Admin)
        if path == "/settings/keys":
            self._json(410, {"ok": False, "error": "Eigene API-Keys sind entfernt. Keys verwaltet der Admin."}); return

        # ── Profil & Einstellungen ─────────────────────────
        # PDF-Export: KI-Text als PDF herunterladen
        if path == "/export/pdf":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            title = (body.get("title", "") or "Sychos Export")[:120]
            content = body.get("content", "") or ""
            if not content.strip():
                self._json(400, {"ok": False, "error": "Kein Inhalt zum Exportieren."}); return
            data = make_pdf(title, content)
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", 'attachment; filename="sychos-export.pdf"')
            self._cors()
            self.end_headers()
            self.wfile.write(data)
            return

        # 2FA: Authenticator einrichten (Secret + otpauth-Link fuer die QR-Anzeige)
        if path == "/settings/2fa/setup":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            if user.get("totp_on"):
                self._json(400, {"ok": False, "error": "2FA ist bereits aktiv."}); return
            secret = base64.b32encode(os.urandom(20)).decode().rstrip("=")
            db = get_db()
            db.execute("UPDATE users SET totp_secret=? WHERE uid=?", (secret, user["uid"]))
            db.commit(); db.close()
            uri = ("otpauth://totp/Sychos:" + urllib.parse.quote(user["email"]) +
                   "?secret=" + secret + "&issuer=Sychos&digits=6&period=30")
            self._json(200, {"ok": True, "secret": secret, "otpauth": uri}); return

        # 2FA: mit bestaetigtem Code aktivieren
        if path == "/settings/2fa/enable":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            code = (body.get("code", "") or "").strip()
            db = get_db()
            row = db.execute("SELECT totp_secret FROM users WHERE uid=?", (user["uid"],)).fetchone()
            if not row or not totp_ok(row["totp_secret"], code):
                db.close()
                self._json(400, {"ok": False, "error": "Code falsch – bitte erneut versuchen."}); return
            db.execute("UPDATE users SET totp_on=1 WHERE uid=?", (user["uid"],))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # 2FA: deaktivieren
        if path == "/settings/2fa/disable":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            db = get_db()
            db.execute("UPDATE users SET totp_on=0, totp_secret='' WHERE uid=?", (user["uid"],))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # Anzeigename aendern
        if path == "/settings/profile":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            name = (body.get("display_name", "") or "").strip()[:40]
            if not name:
                self._json(400, {"ok": False, "error": "Name darf nicht leer sein."}); return
            db = get_db()
            db.execute("UPDATE users SET display_name=? WHERE uid=?", (name, user["uid"]))
            db.commit(); db.close()
            self._json(200, {"ok": True, "display_name": name}); return

        # E-Mail aendern (Passwort bestaetigen)
        if path == "/settings/email":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            email = (body.get("email", "") or "").strip().lower()
            pw = body.get("password", "")
            if not email or "@" not in email:
                self._json(400, {"ok": False, "error": "Ungueltige E-Mail."}); return
            db = get_db()
            row = db.execute("SELECT password_hash FROM users WHERE uid=?", (user["uid"],)).fetchone()
            if not row or not verify_pw(pw, row["password_hash"]):
                db.close()
                self._json(401, {"ok": False, "error": "Passwort falsch."}); return
            if db.execute("SELECT 1 FROM users WHERE email=? AND uid<>?", (email, user["uid"])).fetchone():
                db.close()
                self._json(409, {"ok": False, "error": "E-Mail bereits vergeben."}); return
            db.execute("UPDATE users SET email=? WHERE uid=?", (email, user["uid"]))
            db.commit(); db.close()
            self._json(200, {"ok": True, "email": email}); return

        # Passwort aendern (aktuelles Passwort noetig)
        if path == "/settings/password":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            cur_pw = body.get("current_password", "")
            new_pw = body.get("new_password", "")
            if len(new_pw) < 4:
                self._json(400, {"ok": False, "error": "Neues Passwort: mind. 4 Zeichen."}); return
            db = get_db()
            row = db.execute("SELECT password_hash FROM users WHERE uid=?", (user["uid"],)).fetchone()
            if not row or not verify_pw(cur_pw, row["password_hash"]):
                db.close()
                self._json(401, {"ok": False, "error": "Aktuelles Passwort falsch."}); return
            db.execute("UPDATE users SET password_hash=? WHERE uid=?", (hash_pw(new_pw), user["uid"]))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # Account endgueltig loeschen (Passwort noetig; Admin-Account ist geschuetzt)
        if path == "/settings/delete":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            pw = body.get("password", "")
            db = get_db()
            row = db.execute("SELECT password_hash, is_admin FROM users WHERE uid=?", (user["uid"],)).fetchone()
            if not row or not verify_pw(pw, row["password_hash"]):
                db.close()
                self._json(401, {"ok": False, "error": "Passwort falsch."}); return
            if row["is_admin"]:
                db.close()
                self._json(403, {"ok": False, "error": "Der Admin-Account kann nicht geloescht werden."}); return
            for r in db.execute("SELECT id FROM chats WHERE uid=?", (user["uid"],)).fetchall():
                db.execute("DELETE FROM messages WHERE chat_id=?", (r["id"],))
            db.execute("DELETE FROM chats WHERE uid=?", (user["uid"],))
            db.execute("DELETE FROM sessions WHERE uid=?", (user["uid"],))
            db.execute("DELETE FROM user_keys WHERE uid=?", (user["uid"],))
            db.execute("DELETE FROM users WHERE uid=?", (user["uid"],))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # EIGENER Checkout + echte Abbuchung: PaymentIntent fuer die Kartenabfrage IM eigenen UI
        if path == "/pay/intent":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            pid = body.get("plan", "")
            if pid not in PLANS or pid == "free":
                self._json(400, {"ok": False, "error": "Unbekannter Plan."}); return
            if not (STRIPE_SECRET_KEY and STRIPE_PUBLISHABLE_KEY):
                self._json(503, {"ok": False, "error": "Zahlung nicht konfiguriert (Stripe-Keys fehlen)."}); return
            code = (body.get("code", "") or "").strip().lower()
            price = PLANS[pid]["price"]
            if code:
                if code not in PROMO_CODES:
                    self._json(400, {"ok": False, "error": "Ungueltiger Rabattcode."}); return
                price = round(price * (1 - PROMO_CODES[code]), 2)
            amount = max(50, int(round(price * 100)))   # Stripe-Mindestbetrag 0,50 $
            data = urllib.parse.urlencode({
                "amount": str(amount),
                "currency": "usd",
                "automatic_payment_methods[enabled]": "true",
                "description": "Sychos Plan " + PLANS[pid]["name"] + " – 30 Tage",
                "metadata[uid]": user["uid"],
                "metadata[plan]": pid,
                "metadata[code]": code,
                "receipt_email": user["email"],
            }).encode()
            req = urllib.request.Request("https://api.stripe.com/v1/payment_intents",
                data=data, method="POST",
                headers={"Authorization": "Bearer " + STRIPE_SECRET_KEY,
                         "Content-Type": "application/x-www-form-urlencoded"})
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    pi = json.loads(resp.read().decode())
            except Exception:
                self._json(502, {"ok": False, "error": "Zahlungsdienst nicht erreichbar."}); return
            self._json(200, {"ok": True, "client_secret": pi.get("client_secret", ""),
                             "publishable": STRIPE_PUBLISHABLE_KEY, "amount": amount / 100.0}); return

        # Abbuchung bestaetigen -> Plan aktivieren (prueft das ECHTE PaymentIntent bei Stripe)
        if path == "/pay/confirm":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            pi_id = (body.get("intent_id", "") or "").strip()
            if not pi_id or not STRIPE_SECRET_KEY:
                self._json(400, {"ok": False, "error": "Keine Zahlung gefunden."}); return
            req = urllib.request.Request(
                "https://api.stripe.com/v1/payment_intents/" + urllib.parse.quote(pi_id),
                headers={"Authorization": "Bearer " + STRIPE_SECRET_KEY})
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    pi = json.loads(resp.read().decode())
            except Exception:
                self._json(502, {"ok": False, "error": "Zahlungsdienst nicht erreichbar."}); return
            if pi.get("status") != "succeeded":
                self._json(402, {"ok": False, "error": "Zahlung wurde noch nicht abgebucht."}); return
            meta = pi.get("metadata") or {}
            if meta.get("uid") != user["uid"]:
                self._json(403, {"ok": False, "error": "Diese Zahlung gehoert zu einem anderen Konto."}); return
            pid = meta.get("plan", "")
            if pid not in PLANS:
                self._json(400, {"ok": False, "error": "Unbekannter Plan."}); return
            price_paid = (pi.get("amount_received") or 0) / 100.0
            until = time.time() + PLAN_DAYS * 24 * 3600 if pid != "free" else 0
            db = get_db()
            db.execute("UPDATE users SET plan=?, plan_until=?, is_paid=?, paid_until=? WHERE uid=?",
                       (pid, until, 1 if pid != "free" else 0, until, user["uid"]))
            db.execute("INSERT INTO orders (uid,plan,code,price,created_at) VALUES (?,?,?,?,?)",
                       (user["uid"], pid, meta.get("code", ""), price_paid, time.time()))
            db.commit(); db.close()
            self._json(200, {"ok": True, "plan": pid, "plan_name": PLANS[pid]["name"],
                             "price_paid": price_paid, "plan_until": until}); return

        # ECHTES Zahlen: Stripe Checkout Session anlegen -> User zahlt auf Stripes sicherer Seite
        if path == "/plan/checkout":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            pid = body.get("plan", "")
            if pid not in PLANS or pid == "free":
                self._json(400, {"ok": False, "error": "Unbekannter Plan."}); return
            if not STRIPE_SECRET_KEY:
                self._json(503, {"ok": False, "error": "Zahlung nicht konfiguriert (STRIPE_SECRET_KEY fehlt)."}); return
            code = (body.get("code", "") or "").strip().lower()
            price = PLANS[pid]["price"]
            if code:
                if code not in PROMO_CODES:
                    self._json(400, {"ok": False, "error": "Ungueltiger Rabattcode."}); return
                price = round(price * (1 - PROMO_CODES[code]), 2)
            # Stripe verlangt mindestens 0,50 $ – wird transparent an den Kunden ausgewiesen
            amount = max(50, int(round(price * 100)))
            host = self.headers.get("Host", "") or ("localhost:%d" % PORT)
            base = "http://" + host
            data = urllib.parse.urlencode({
                "mode": "payment",
                "client_reference_id": user["uid"],
                "customer_email": user["email"],
                "line_items[0][price_data][currency]": "usd",
                "line_items[0][price_data][unit_amount]": str(amount),
                "line_items[0][price_data][product_data][name]":
                    "Sychos Plan " + PLANS[pid]["name"] + " – 30 Tage",
                "line_items[0][quantity]": "1",
                "success_url": base + "/?paid={CHECKOUT_SESSION_ID}",
                "cancel_url": base + "/?pay=cancel",
                "metadata[uid]": user["uid"],
                "metadata[plan]": pid,
                "metadata[code]": code,
            }).encode()
            req = urllib.request.Request("https://api.stripe.com/v1/checkout/sessions",
                data=data, method="POST",
                headers={"Authorization": "Bearer " + STRIPE_SECRET_KEY,
                         "Content-Type": "application/x-www-form-urlencoded"})
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    sess = json.loads(resp.read().decode())
            except Exception:
                self._json(502, {"ok": False, "error": "Zahlungsdienst nicht erreichbar."}); return
            self._json(200, {"ok": True, "url": sess.get("url", ""),
                             "session_id": sess.get("id", ""), "amount": amount / 100.0}); return

        # ECHTES Zahlen bestaetigen: Stripe pruefen, dann Plan aktivieren
        if path == "/plan/confirm":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            sid = (body.get("session_id", "") or "").strip()
            if not sid or not STRIPE_SECRET_KEY:
                self._json(400, {"ok": False, "error": "Keine Zahlungs-Sitzung."}); return
            req = urllib.request.Request(
                "https://api.stripe.com/v1/checkout/sessions/" + urllib.parse.quote(sid),
                headers={"Authorization": "Bearer " + STRIPE_SECRET_KEY})
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    sess = json.loads(resp.read().decode())
            except Exception:
                self._json(502, {"ok": False, "error": "Zahlungsdienst nicht erreichbar."}); return
            if sess.get("payment_status") != "paid":
                self._json(402, {"ok": False, "error": "Zahlung wurde noch nicht abgebucht."}); return
            meta = sess.get("metadata") or {}
            if meta.get("uid") != user["uid"]:
                self._json(403, {"ok": False, "error": "Diese Zahlung gehoert zu einem anderen Konto."}); return
            pid = meta.get("plan", "")
            if pid not in PLANS:
                self._json(400, {"ok": False, "error": "Unbekannter Plan."}); return
            price_paid = (sess.get("amount_total") or 0) / 100.0
            until = time.time() + PLAN_DAYS * 24 * 3600 if pid != "free" else 0
            db = get_db()
            db.execute("UPDATE users SET plan=?, plan_until=?, is_paid=?, paid_until=? WHERE uid=?",
                       (pid, until, 1 if pid != "free" else 0, until, user["uid"]))
            db.execute("INSERT INTO orders (uid,plan,code,price,created_at) VALUES (?,?,?,?,?)",
                       (user["uid"], pid, meta.get("code", ""), price_paid, time.time()))
            db.commit(); db.close()
            self._json(200, {"ok": True, "plan": pid, "plan_name": PLANS[pid]["name"],
                             "price_paid": price_paid, "plan_until": until}); return

        # ── Merchant-of-Record Webhooks: Paddle / Lemon Squeezy buchen ab UND
        #    fuehren alle Steuern ab – hier wird der Plan nach Zahlung freigeschaltet.
        if path in ("/webhook/paddle", "/webhook/lemonsqueezy"):
            raw = getattr(self, "_raw_body", b"") or b""
            ok_sig = False
            if path == "/webhook/paddle" and PADDLE_WEBHOOK_SECRET:
                sig = self.headers.get("Paddle-Signature", "")
                parts = dict(p.split("=", 1) for p in sig.split(";") if "=" in p)
                ts = parts.get("ts", "")
                h1 = parts.get("h1") or parts.get("hmac", "")
                if ts and h1:
                    msg = (ts + ":" + raw.decode("utf-8", "replace")).encode()
                    # Secret mit/ohne abschliessenden Schrägstrich akzeptieren
                    for secret in {PADDLE_WEBHOOK_SECRET, PADDLE_WEBHOOK_SECRET.strip().rstrip("/"),
                                   PADDLE_WEBHOOK_SECRET.strip() + "/"}:
                        if secret and hmac.compare_digest(
                                hmac.new(secret.encode(), msg, hashlib.sha1).hexdigest(), h1):
                            ok_sig = True
                            break
            elif path == "/webhook/lemonsqueezy" and LEMONSQUEEZY_WEBHOOK_SECRET:
                sig = (self.headers.get("X-Signature", "") or "").replace("sha256=", "")
                calc = hmac.new(LEMONSQUEEZY_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
                ok_sig = hmac.compare_digest(calc, sig)
            if not ok_sig:
                self._json(401, {"ok": False, "error": "Ungueltige Signatur."}); return
            try:
                payload = json.loads(raw.decode("utf-8", "replace") or "{}")
            except Exception:
                payload = {}
            custom, amt = {}, None
            if path == "/webhook/paddle":
                data = payload.get("data") or {}
                custom = data.get("custom_data") or payload.get("custom_data") or {}
                amt = (data.get("details", {}).get("totals", {}) or {}).get("total") \
                    or payload.get("amount_gross")
            else:
                attrs = (payload.get("data") or {}).get("attributes") or {}
                custom = (attrs.get("checkout_data", {}) or {}).get("custom") \
                    or (payload.get("meta", {}) or {}).get("custom_data") or {}
                amt = attrs.get("total")
            uid, pid = str(custom.get("uid", "")), custom.get("plan", "")
            code = str(custom.get("code", "") or "")
            if uid and pid in PLANS:
                try:
                    price_paid = round(float(amt or 0) / 100.0, 2)
                except Exception:
                    price_paid = PLANS[pid]["price"]
                until = time.time() + PLAN_DAYS * 24 * 3600 if pid != "free" else 0
                db = get_db()
                if db.execute("SELECT 1 FROM users WHERE uid=?", (uid,)).fetchone():
                    db.execute("UPDATE users SET plan=?, plan_until=?, is_paid=?, paid_until=? WHERE uid=?",
                               (pid, until, 1 if pid != "free" else 0, until, uid))
                    db.execute("INSERT INTO orders (uid,plan,code,price,created_at) VALUES (?,?,?,?,?)",
                               (uid, pid, code, price_paid, time.time()))
                    db.commit()
                db.close()
            self._json(200, {"ok": True}); return

        # Plan kaufen (Test-Checkout ohne echte Abbuchung) -> 30 Tage aktiv
        # Rabattcode: 'Release' = -20 % (in PROMO_CODES)
        if path == "/plan/buy":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            pid = body.get("plan", "")
            if pid not in PLANS:
                self._json(400, {"ok": False, "error": "Unbekannter Plan."}); return
            code = (body.get("code", "") or "").strip().lower()
            price = PLANS[pid]["price"]
            discount = 0.0
            if code:
                if code not in PROMO_CODES:
                    self._json(400, {"ok": False, "error": "Ungueltiger Rabattcode."}); return
                discount = PROMO_CODES[code]
                price = round(price * (1 - discount), 2)
            until = time.time() + PLAN_DAYS * 24 * 3600 if pid != "free" else 0
            db = get_db()
            db.execute("UPDATE users SET plan=?, plan_until=?, is_paid=?, paid_until=? WHERE uid=?",
                       (pid, until, 1 if pid != "free" else 0, until, user["uid"]))
            db.execute("INSERT INTO orders (uid,plan,code,price,created_at) VALUES (?,?,?,?,?)",
                       (user["uid"], pid, code, price, time.time()))
            db.commit(); db.close()
            self._json(200, {"ok": True, "plan": pid, "plan_name": PLANS[pid]["name"],
                             "plan_until": until, "price_paid": price,
                             "discount": discount, "code": code}); return

        # ── Admin-Warnsystem ──────────────────────────────
        # Admin sendet eine Warn-Nachricht an einen User (Popup im Hub)
        if path == "/admin/warn":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            uid = body.get("uid", "")
            msg = (body.get("message", "") or "").strip()[:500]
            if not msg:
                self._json(400, {"ok": False, "error": "Warnung ohne Text."}); return
            db = get_db()
            if not db.execute("SELECT 1 FROM users WHERE uid=?", (uid,)).fetchone():
                db.close()
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            db.execute("INSERT INTO warnings (uid,message,created_at) VALUES (?,?,?)",
                       (uid, msg, time.time()))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # User holt seine offenen Warnungen
        if path == "/warnings":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            db = get_db()
            rows = db.execute("SELECT id,message,created_at FROM warnings WHERE uid=? AND read=0 ORDER BY id",
                              (user["uid"],)).fetchall()
            db.close()
            self._json(200, {"ok": True, "warnings": [dict(r) for r in rows]}); return

        # Warnungen als gelesen markieren (ohne id = alle)
        if path == "/warnings/read":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            wid = body.get("id", 0)
            db = get_db()
            if wid:
                db.execute("UPDATE warnings SET read=1 WHERE id=? AND uid=?", (wid, user["uid"]))
            else:
                db.execute("UPDATE warnings SET read=1 WHERE uid=?", (user["uid"],))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # Neuer Chat
        if path == "/chat/new":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            cid = str(uuid.uuid4())
            title = (body.get("title", "Neuer Chat") or "Neuer Chat")[:MAX_TITLE_LEN]
            model = body.get("model", "")
            if model not in MODELS:
                model = "gemini-3.6-flash"
            db = get_db()
            db.execute("INSERT INTO chats (id,uid,title,model,created_at) VALUES (?,?,?,?,?)",
                       (cid, user["uid"], title, model, time.time()))
            db.commit(); db.close()
            self._json(200, {"ok": True, "chat_id": cid, "title": title, "model": model}); return

        # Chat umbenennen
        if path == "/chat/rename":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            cid = body.get("chat_id", "")
            title = (body.get("title", "") or "").strip()[:MAX_TITLE_LEN] or "Neuer Chat"
            db = get_db()
            cur = db.execute("UPDATE chats SET title=? WHERE id=? AND uid=?",
                             (title, cid, user["uid"]))
            db.commit(); db.close()
            if cur.rowcount == 0:
                self._json(404, {"ok": False, "error": "Chat nicht gefunden"}); return
            self._json(200, {"ok": True, "chat_id": cid, "title": title}); return

        # Chat loeschen
        if path == "/chat/delete":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            cid = body.get("chat_id", "")
            db = get_db()
            db.execute("DELETE FROM messages WHERE chat_id=? AND chat_id IN (SELECT id FROM chats WHERE uid=?)",
                       (cid, user["uid"]))
            db.execute("DELETE FROM chats WHERE id=? AND uid=?", (cid, user["uid"]))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # AI-Chat senden: Credits atomar reservieren (kein Doppelabzug bei Parallel-Requests)
        if path == "/chat/send":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            if not rate_ok("send:" + user["uid"], SEND_RATE_LIMIT):
                self._json(429, {"ok": False, "error": "Zu viele Nachrichten. Bitte kurz warten."}); return
            cid = body.get("chat_id", "")
            msg = (body.get("message", "") or "").strip()
            model = body.get("model", "")
            strength = body.get("strength", "medium")
            if model not in MODELS:
                self._json(400, {"ok": False, "error": "Unbekanntes Modell"}); return
            plan = effective_plan(user)
            if MODELS[model].get("paid") and plan == "free" and not user.get("is_admin"):
                self._json(402, {"ok": False, "error_type": "premium_locked",
                    "error": "Premium-Modell – ab dem Basic-Plan verfuegbar. Bitte unter Einstellungen -> Plan freischalten."}); return

            # Bilder (Vision): max. 3, nur an Modelle mit vision-Flag
            images = []
            for im in (body.get("images") or [])[:3]:
                try:
                    data = (im.get("data") or "").strip()
                    mime = (im.get("mime") or "image/png").strip().lower()
                except AttributeError:
                    continue
                if not data or len(data) > 4200000 or mime not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
                    self._json(400, {"ok": False, "error": "Bild ungueltig (PNG/JPEG/WEBP/GIF, max. 3 MB)."}); return
                images.append({"type": "image", "mime": mime, "data": data})
            if images and not MODELS[model].get("vision"):
                self._json(400, {"ok": False, "error_type": "no_vision",
                    "error": "Dieses Modell unterstuetzt keine Bilder. Bitte ein Modell mit dem Bild-Symbol \U0001F5BC waehlen (z. B. Gemini 3.6 Flash)."}); return
            if strength not in STRENGTHS:
                strength = "medium"
            if not msg:
                self._json(400, {"ok": False, "error": "Leere Nachricht"}); return
            if len(msg) > MAX_MSG_LEN:
                self._json(400, {"ok": False, "error": "Nachricht zu lang (max. %d Zeichen)" % MAX_MSG_LEN}); return

            db = get_db()
            own = db.execute("SELECT id FROM chats WHERE id=? AND uid=?", (cid, user["uid"])).fetchone()
            if not own:
                db.close()
                self._json(404, {"ok": False, "error": "Chat nicht gefunden"}); return
            rows = db.execute("SELECT role,content,images FROM messages WHERE chat_id=? ORDER BY id",
                              (cid,)).fetchall()
            history = []
            for r in rows:
                content = r["content"]
                try:
                    rimgs = json.loads(r["images"] or "[]")
                except Exception:
                    rimgs = []
                if rimgs:
                    content = [{"type": "text", "text": r["content"]}] + rimgs
                history.append({"role": r["role"], "content": content})
            history.append({"role": "user", "content": ([{"type": "text", "text": msg}] + images) if images else msg})
            # Identitaet: bei Modellwechsel (z.B. GLM -> DeepSeek) antwortet die KI
            # ab jetzt IMMER mit dem AKTUELL gewaehlten Modell – nie mehr mit dem alten.
            ident = ("[System-Hinweis: Du bist 'Sychos'. Dein aktuelles Modell ist "
                     + MODELS[model]["name"] + ". Wenn gefragt, antworte genau so. Behaupte NIEMALS, "
                     "ein anderes Modell oder eine andere KI zu sein (z.B. GLM, DeepSeek, Gemini, ChatGPT), "
                     "auch wenn im Chatverlauf anderes steht.]")
            if isinstance(history[-1]["content"], str):
                history[-1]["content"] += "\n\n" + ident
            else:
                history[-1]["content"][0]["text"] += "\n\n" + ident

            # Token-Limits (5h / Woche / Monat) wie Cline: bei Erreichen bis zum Reset warten
            est = est_tokens(history)
            lim = limit_block(user["uid"], plan, est)
            if lim:
                win, wstate, _ = lim
                db.close()
                self._json(429, {"ok": False, "error_type": "limit_reached",
                    "error": "%s-Limit erreicht (Plan %s). Weiter in %s – oder Plan upgraden."
                             % (TOKEN_WINDOWS[win]["label"], PLANS[plan]["name"],
                                fmt_wait(wstate["resets_at"] - time.time())),
                    "limit_win": win, "resets_at": wstate["resets_at"]}); return

            db.execute("INSERT INTO messages (chat_id,role,content,model,images,created_at) VALUES (?,?,?,?,?,?)",
                       (cid, "user", msg, model, json.dumps(images), time.time()))
            web_used = False
            if body.get("web"):
                hits = web_search(msg)
                if hits:
                    ctx = "\n\n[Aktuelle Web-Recherche als Kontext]\n" + "\n".join(hits)
                    if isinstance(history[-1]["content"], str):
                        history[-1]["content"] += ctx
                    else:
                        history[-1]["content"][0]["text"] += ctx
                web_used = True
            api_key = resolve_key(db, user["uid"], MODELS[model]["provider"])
            db.commit(); db.close()

            # SSE-Stream: Tokens live an das Frontend senden
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self._cors()
            self.end_headers()

            def push(evt, data):
                self.wfile.write(("event: %s\ndata: %s\n\n" % (evt, json.dumps(data, ensure_ascii=False))).encode("utf-8"))
                self.wfile.flush()

            full, tokens, err = [], 0, None
            for ev in stream_provider(history, api_key, strength, model):
                if ev["type"] == "delta":
                    full.append(ev["text"])
                    push("delta", {"t": ev["text"]})
                elif ev["type"] == "end":
                    tokens = ev.get("tokens", 0)
                elif ev["type"] == "error":
                    err = ev
            text = "".join(full)
            if err or not text.strip():
                e2 = err or {"error": "Leere Antwort – bitte erneut versuchen.", "error_type": "api"}
                push("error", {"error": e2["error"], "error_type": e2.get("error_type", "api")})
                return
            # Token-Abrechnung (Input + Output) auf alle drei Limit-Fenster
            in_tok = int(sum((len(m.get("content")) if isinstance(m.get("content"), str)
                              else sum(len(p.get("text") or "") + (1800 if p.get("type") == "image" else 0)
                                       for p in (m.get("content") or [])))
                             for m in history) / 4)
            out_tok = tokens if tokens else max(1, len(text) // 4)
            used = max(1, in_tok + out_tok)
            add_usage(user["uid"], used)
            db = get_db()
            db.execute("INSERT INTO messages (chat_id,role,content,model,tokens,cost,created_at) VALUES (?,?,?,?,?,?,?)",
                       (cid, "assistant", text, model, used, 0, time.time()))
            db.commit(); db.close()
            push("done", {"text": text, "web": web_used, "tokens_used": used,
                          "usage": usage_state(user["uid"], plan)})
            return

        # ── Admin ─────────────────────────────────────────
        if path == "/admin/tokens":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            uid = body.get("uid", "")
            try:
                amount = float(body.get("tokens", 0))
            except (TypeError, ValueError):
                self._json(400, {"ok": False, "error": "Ungueltige Menge"}); return
            if amount == 0 or abs(amount) > 10 ** 9:
                self._json(400, {"ok": False, "error": "Menge ungueltig"}); return
            db = get_db()
            cur = db.execute("UPDATE users SET bonus_tokens = MAX(0, COALESCE(bonus_tokens,0) + ?) WHERE uid=?",
                             (amount, uid))
            row = db.execute("SELECT bonus_tokens FROM users WHERE uid=?", (uid,)).fetchone()
            db.commit(); db.close()
            if cur.rowcount == 0:
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            self._json(200, {"ok": True, "uid": uid, "bonus_tokens": (row["bonus_tokens"] if row else 0)}); return

        # Admin: Account zuruecksetzen -> Free-Plan + Standard-Credits/-Tokens (Usage wird geleert)
        if path == "/admin/reset":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            uid = body.get("uid", "")
            db = get_db()
            cur = db.execute("UPDATE users SET plan='free', plan_until=0, is_paid=0, paid_until=0, "
                             "credits=?, bonus_tokens=? WHERE uid=?",
                             (START_CREDITS, START_TOKENS, uid))
            db.execute("DELETE FROM usage WHERE uid=?", (uid,))
            db.commit(); db.close()
            if cur.rowcount == 0:
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            self._json(200, {"ok": True, "plan": "free", "credits": START_CREDITS,
                             "bonus_tokens": START_TOKENS}); return

        # Admin-Rechte vergeben/entziehen; bei Entzug wird der User SOFORT ausgeloggt
        # (alle Sessions werden geloescht -> sein Token ist sofort ungueltig)
        if path == "/admin/setadmin":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            uid = body.get("uid", "")
            make = bool(body.get("admin", True))
            db = get_db()
            row = db.execute("SELECT is_admin FROM users WHERE uid=?", (uid,)).fetchone()
            if not row:
                db.close()
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            if not make and row["is_admin"]:
                n = db.execute("SELECT COUNT(*) AS n FROM users WHERE is_admin=1").fetchone()["n"]
                if n <= 1:
                    db.close()
                    self._json(400, {"ok": False, "error": "Der letzte Admin kann nicht entzogen werden."}); return
            db.execute("UPDATE users SET is_admin=? WHERE uid=?", (1 if make else 0, uid))
            if not make:
                db.execute("DELETE FROM sessions WHERE uid=?", (uid,))
            db.commit(); db.close()
            self._json(200, {"ok": True, "is_admin": bool(make)}); return

        # User sperren/entsperren: Grund + Dauer (Minuten, 0 = dauerhaft)
        if path == "/admin/ban":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            uid = body.get("uid", "")
            ban = 1 if body.get("ban", True) else 0
            reason = (body.get("reason", "") or "").strip()[:200]
            try:
                duration = max(0, int(body.get("duration_minutes", 0) or 0))
            except (TypeError, ValueError):
                duration = 0
            until = (time.time() + duration * 60) if duration else 0
            db = get_db()
            cur = db.execute(
                "UPDATE users SET is_banned=?, ban_reason=?, ban_until=? WHERE uid=?",
                (ban, reason if ban else "", until if ban else 0, uid))
            db.commit(); db.close()
            if cur.rowcount == 0:
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            self._json(200, {"ok": True, "banned": bool(ban), "ban_until": until if ban else 0}); return

        # Admin vergibt "Sychos Paid" -> entsperrt Premium-Modelle
        if path == "/admin/paid":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            uid = body.get("uid", "")
            # paid: true/1 -> geben, false/0/"nein" -> WEGNEHMEN
            raw = body.get("paid", True)
            raw = body.get("paid", True)
            # Plan-Auswahl: 'plan' = free/basic/pro/max/ultra/business (ohne 'plan' alt: paid -> pro)
            pid = (body.get("plan") or "").strip().lower()
            if pid not in PLANS:
                pid = "pro" if not (raw is False or raw == 0 or str(raw).strip().lower() in ("0", "false", "nein", "weg", "off")) else "free"
            # Dauer in Minuten: 0 = dauerhaft, sonst zeitlich begrenzt (laeuft automatisch ab)
            try:
                dur = max(0, int(body.get("duration_minutes", 0) or 0))
            except (TypeError, ValueError):
                dur = 0
            until = (time.time() + dur * 60) if (pid != "free" and dur) else 0
            paid = 0 if pid == "free" else 1
            db = get_db()
            cur = db.execute("UPDATE users SET is_paid=?, paid_until=?, plan=?, plan_until=? WHERE uid=?",
                             (paid, until, pid, until, uid))
            db.commit(); db.close()
            if cur.rowcount == 0:
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            self._json(200, {"ok": True, "paid": bool(paid), "paid_until": until, "plan": pid,
                             "plan_name": PLANS[pid]["name"], "plan_until": until}); return

        # Admin: globale Provider-Keys setzen (leerer Key = entfernen)
        if path == "/admin/settings":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            db = get_db()
            for prov in ("gemini", "groq", "cline"):
                if prov + "_api_key" in body:
                    val = (body[prov + "_api_key"] or "").strip()[:200]
                    db.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)",
                               (prov + "_api_key", val))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

def write_api_keys_to_db():
    """Traegt die Provider-API-Keys DIREKT in die Datenbank (settings) ein.

    Nutzung:
      python oracle_server.py --set-keys
          -> eingebaute bzw. per Env uebergebene Keys in die DB schreiben
      python oracle_server.py --set-keys GEMINI=xxx GROQ=yyy CLINE=zzz
          -> eigene Keys eintragen (leerer Wert = entfernen)
    Danach wird der Server automatisch gestartet (--no-start = nur in die DB schreiben).
    """
    custom = {}
    for a in sys.argv[1:]:
        if "=" in a and not a.startswith("-"):
            k, v = a.split("=", 1)
            custom[k.strip().upper()] = v
    keys = {
        "gemini": custom.get("GEMINI", GEMINI_API_KEY),
        "groq": custom.get("GROQ", GROQ_API_KEY),
        "cline": custom.get("CLINE", CLINE_API_KEY),
    }
    init_db()
    db = get_db()
    print("\n  API-Keys direkt in die DB schreiben (" + DB_PATH + "):")
    for prov, val in keys.items():
        val = (val or "").strip()
        db.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)",
                   (prov + "_api_key", val))
        print("    [OK] %-6s -> %s" % (prov.capitalize(), mask_key(val) if val else "(leer = entfernt)"))
    db.commit(); db.close()
    print("  Fertig. Keys sind sofort aktiv (Admin-Panel zeigt 'hinterlegt').")


# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════
def main():
    init_db()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("\n  ========================================")
    print("     S Y C H O S   O R A C L E  v5.0     ")
    print("  ========================================")
    print(f"  Hub   : http://localhost:{PORT}")
    print(f"  Admin : http://localhost:{PORT}/admin/")
    print(f"  DB    : {DB_PATH}")
    print("  ----------------------------------------")
    print("  ========================================\n")
    print("  -> Ctrl+C zum Beenden\n")
    # Always-On: selbstheilend + Port-Konflikte loesen (alter Sychos-Server)
    attempts = 0
    while True:
        try:
            server = HTTPServerCls((HOST, PORT), Handler)
        except OSError as e:
            err = getattr(e, "errno", 0)
            if err in (98, 48, 10048) and attempts < 2:
                attempts += 1
                # 1) zuerst alten Sychos-Server raeumen (wichtigster Schritt!)
                if free_port():
                    print("  Alter Sychos-Server beendet -> Port %d ist wieder frei..." % PORT)
                    continue
                # 2) erst danach pruefen, ob schon eine laufende Instanz existiert
                if _service_active():
                    print("\n  Laeuft bereits als Always-On Service (sychos-hub).")
                    print("  -> http://localhost:%d  (systemctl status sychos-hub)" % PORT)
                    return
                print("\n  Port %d ist belegt! Anderen Port waehlen:" % PORT)
                print("    Windows: set ORACLE_PORT=7778 && python oracle_server.py")
                print("    Linux:   ORACLE_PORT=7778 python3 oracle_server.py")
                sys.exit(1)
            raise
        attempts = 0
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n  Server gestoppt.")
            server.server_close()
            break
        except Exception as e:
            print("  Fehler -> Neustart in 5s:", e)
            time.sleep(5)

def install_autostart():
    """Always-On: Windows = Autostart-Link, Linux = systemd (mit sudo)."""
    script = os.path.abspath(__file__)
    if os.name == "nt":
        import subprocess
        py = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.isfile(py):
            py = sys.executable
        startup = os.path.join(os.environ.get("APPDATA", ""),
            "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
        lnk = os.path.join(startup, "SychosHub.lnk")
        ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
              "$s.TargetPath='%s';$s.Arguments='\"%s\"';$s.WindowStyle=7;$s.Save()"
              % (lnk.replace("'", "''"), py, script))
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
        print("  [OK] Autostart installiert (startet bei jedem Windows-Login):")
        print("       " + lnk)
    else:
        if os.geteuid() != 0:
            print("  Linux: Bitte mit sudo starten fuer systemd-Autostart:")
            print("         sudo python3 oracle_server.py --install")
            return
        unit = ("[Unit]\nDescription=Sychos Hub (Always-On)\nAfter=network.target\n\n"
                "[Service]\nType=simple\nExecStart=%s %s\nRestart=always\nRestartSec=5\n"
                "Environment=ORACLE_HOST=0.0.0.0\nEnvironment=ORACLE_PORT=%d\n\n"
                "[Install]\nWantedBy=multi-user.target\n"
                % (sys.executable, script, PORT))
        with open("/etc/systemd/system/sychos-hub.service", "w") as f:
            f.write(unit)
        os.system("systemctl daemon-reload")
        os.system("systemctl enable sychos-hub >/dev/null 2>&1")
        os.system("systemctl restart sychos-hub")
        print("  [OK] systemd-Service installiert (Always-On): sychos-hub")
        print("       -> laeuft bereits im Hintergrund, kein extra Start noetig")
        return True
    return False

def _service_active():
    """True, wenn der eigene systemd-Service sychos-hub bereits laeuft."""
    if os.name == "nt":
        return False
    return os.system("systemctl is-active --quiet sychos-hub >/dev/null 2>&1") == 0

def free_port():
    """Beendet alte Sychos-Prozesse (v2 / Sychos Net) auf dem Port — nie sich selbst."""
    me = os.getpid()
    me_path = os.path.abspath(__file__)
    pat = ("sychos-oracle", "SychosOracle")
    killed = []
    try:
        if os.name == "nt":
            import subprocess
            res = subprocess.run(["powershell", "-NoProfile", "-Command",
                "(Get-NetTCPConnection -LocalPort %d -State Listen).OwningProcess" % PORT],
                capture_output=True, text=True).stdout
            pids = set(x.strip() for x in res.splitlines() if x.strip())
            for pid in pids:
                if pid == str(me):
                    continue
                info = subprocess.run(["powershell", "-NoProfile", "-Command",
                    "(Get-CimInstance Win32_Process -Filter 'ProcessId=%s').CommandLine" % pid],
                    capture_output=True, text=True).stdout
                if any(p in info for p in pat) or (
                        "oracle_server" in info and me_path not in info and "sychos-hub" not in info):
                    subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
                    killed.append(pid)
        else:
            # WICHTIG: alten systemd-Service (v2) stoppen, sonst startet er durch
            # Restart=always sofort wieder und beide streiten um den Port
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                os.system("systemctl stop sychos-oracle >/dev/null 2>&1")
                os.system("systemctl disable sychos-oracle >/dev/null 2>&1")
            for pid in os.listdir("/proc"):
                if not pid.isdigit() or int(pid) == me:
                    continue
                try:
                    with open("/proc/%s/cmdline" % pid, "rb") as f:
                        cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
                except Exception:
                    continue
                if any(p in cmd for p in pat) or (
                        "oracle_server" in cmd and me_path not in cmd and "sychos-hub" not in cmd):
                    try:
                        os.kill(int(pid), 15)
                        killed.append(pid)
                    except Exception:
                        pass
    except Exception:
        pass
    if killed:
        time.sleep(1.5)
    # Erfolg = auf dem Port lauscht nichts mehr
    import socket as _sock
    try:
        c = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
        c.settimeout(1)
        c.connect(("127.0.0.1", PORT))
        c.close()
        return False
    except Exception:
        return True

def uninstall_autostart():
    if os.name == "nt":
        startup = os.path.join(os.environ.get("APPDATA", ""),
            "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
        lnk = os.path.join(startup, "SychosHub.lnk")
        if os.path.isfile(lnk):
            os.remove(lnk)
        print("  [OK] Autostart entfernt.")
    else:
        os.system("systemctl disable --now sychos-hub >/dev/null 2>&1")
        if os.path.isfile("/etc/systemd/system/sychos-hub.service"):
            os.remove("/etc/systemd/system/sychos-hub.service")
        print("  [OK] systemd-Service entfernt.")

if __name__ == "__main__":
    if "--set-keys" in sys.argv:
        write_api_keys_to_db()
        if "--no-start" in sys.argv:
            sys.exit(0)
        print("  -> Server wird gestartet...\n")
    if "--install" in sys.argv:
        if install_autostart():
            sys.exit(0)      # Linux: Service laeuft bereits -> nicht doppelt starten
    elif "--uninstall" in sys.argv:
        uninstall_autostart()
        sys.exit(0)
    main()
