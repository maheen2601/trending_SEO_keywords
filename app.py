import eventlet
eventlet.monkey_patch()
import sys
import io
# Fix Windows console encoding for Unicode
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from flask import Flask, render_template, request, jsonify, session
from flask_socketio import SocketIO, emit
from flask_cors import CORS
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta, timezone
import json
import os
import psycopg2
import hashlib

# Pakistan Standard Time (UTC+5)
PKT = timezone(timedelta(hours=5))

def to_pakistan_time(dt):
    """Convert datetime to Pakistan timezone"""
    if dt is None:
        return datetime.now(PKT).strftime("%Y-%m-%d %H:%M:%S")
    # If naive datetime, assume it's UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(PKT).strftime("%Y-%m-%d %H:%M:%S")

app = Flask(__name__)
app.secret_key = 'your-secret-key-change-this-in-production'
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# ------------------ Database Configuration ------------------
DB_URL = "postgresql://neondb_owner:npg_7SjyKhDinEv8@ep-young-term-a5zyo5in-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require"
DB_URL = os.environ.get("DATABASE_URL")

# ------------------ In-Memory Cache (synced with DB) ------------------
online_users = set()
selections_cache = []  # Cache selections to avoid repeated DB calls
cache_loaded = False

# ------------------ Google Sheets Configuration ------------------
SHEET_ID = "1YeAVnMLPV5nfRE1hUbqyqmhXbBbcKzQC1JK86gPQEiY"
# CREDENTIALS_FILE = "credentials.json"
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_PATH")

CREDS_FILE = None
if GOOGLE_CREDENTIALS_JSON:
    CREDS_FILE = "/tmp/google_credentials.json"
    with open(CREDS_FILE, "w") as f:
        f.write(GOOGLE_CREDENTIALS_JSON)

# ------------------ Password Hashing ------------------
def hash_password(password):
    """Hash password using SHA-256"""
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password, hashed):
    """Verify password against hash"""
    return hash_password(password) == hashed

# ------------------ Database Functions ------------------
def get_db_connection():
    """Get database connection"""
    return psycopg2.connect(DB_URL)

def init_database():
    """Initialize database tables"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Create users table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                team TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # Create selections table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS keyword_selections (
                id SERIAL PRIMARY KEY,
                username TEXT NOT NULL,
                team TEXT NOT NULL,
                keyword TEXT NOT NULL,
                selected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (username, keyword)
            );
        """)
        
        conn.commit()
        print("[DB] Tables initialized successfully")
        
    except Exception as e:
        print(f"[DB] Error initializing database: {e}")
    finally:
        if conn:
            cur.close()
            conn.close()

def db_register_user(username, team, password):
    """Register a new user in database"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Check if user exists
        cur.execute("SELECT username FROM app_users WHERE username = %s", (username,))
        if cur.fetchone():
            return {"success": False, "message": "Username already exists"}
        
        # Insert new user
        password_hash = hash_password(password)
        cur.execute(
            "INSERT INTO app_users (username, team, password_hash) VALUES (%s, %s, %s)",
            (username, team, password_hash)
        )
        conn.commit()
        return {"success": True, "message": "Registration successful"}
        
    except Exception as e:
        print(f"[DB] Registration error: {e}")
        return {"success": False, "message": "Database error"}
    finally:
        if conn:
            cur.close()
            conn.close()

def db_login_user(username, password):
    """Verify user credentials from database"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute(
            "SELECT username, team, password_hash FROM app_users WHERE username = %s",
            (username,)
        )
        user = cur.fetchone()
        
        if not user:
            return {"success": False, "message": "User not found"}
        
        if not verify_password(password, user[2]):
            return {"success": False, "message": "Invalid password"}
        
        return {
            "success": True,
            "user": {"name": user[0], "team": user[1]}
        }
        
    except Exception as e:
        print(f"[DB] Login error: {e}")
        return {"success": False, "message": "Database error"}
    finally:
        if conn:
            cur.close()
            conn.close()

def db_reset_password(username, team, new_password):
    """Reset user password after verifying username and team"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Verify user exists with matching team
        cur.execute(
            "SELECT username, team FROM app_users WHERE username = %s",
            (username,)
        )
        user = cur.fetchone()
        
        if not user:
            return {"success": False, "message": "User not found"}
        
        if user[1].lower() != team.lower():
            return {"success": False, "message": "Team name doesn't match our records"}
        
        # Update password
        new_hash = hash_password(new_password)
        cur.execute(
            "UPDATE app_users SET password_hash = %s WHERE username = %s",
            (new_hash, username)
        )
        conn.commit()
        return {"success": True, "message": "Password reset successful"}
        
    except Exception as e:
        print(f"[DB] Password reset error: {e}")
        return {"success": False, "message": "Database error"}
    finally:
        if conn:
            cur.close()
            conn.close()

def db_add_selection(username, team, keyword):
    """Add a keyword selection to database"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute(
            """INSERT INTO keyword_selections (username, team, keyword) 
               VALUES (%s, %s, %s)
               ON CONFLICT (username, keyword) DO NOTHING""",
            (username, team, keyword)
        )
        conn.commit()
        return True
        
    except Exception as e:
        print(f"[DB] Add selection error: {e}")
        return False
    finally:
        if conn:
            cur.close()
            conn.close()

def db_remove_selection(username, keyword):
    """Remove a keyword selection from database"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute(
            "DELETE FROM keyword_selections WHERE username = %s AND keyword = %s",
            (username, keyword)
        )
        conn.commit()
        return True
        
    except Exception as e:
        print(f"[DB] Remove selection error: {e}")
        return False
    finally:
        if conn:
            cur.close()
            conn.close()

def db_get_all_selections():
    """Get all selections from database"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute(
            """SELECT username, team, keyword, selected_at 
               FROM keyword_selections 
               ORDER BY selected_at DESC"""
        )
        rows = cur.fetchall()
        
        selections = []
        for row in rows:
            selections.append({
                "user": row[0],
                "team": row[1],
                "keyword": row[2],
                "timestamp": to_pakistan_time(row[3])
            })
        return selections
        
    except Exception as e:
        print(f"[DB] Get selections error: {e}")
        return []
    finally:
        if conn:
            cur.close()
            conn.close()

def db_toggle_selection(username, team, keyword):
    """Toggle selection in a single DB operation - returns (action, all_selections)"""
    global selections_cache
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Check if exists and delete in one query
        cur.execute(
            "DELETE FROM keyword_selections WHERE username = %s AND keyword = %s RETURNING id",
            (username, keyword)
        )
        deleted = cur.fetchone()
        
        if deleted:
            # Was deleted (deselected)
            action = "deselected"
        else:
            # Didn't exist, so insert (select)
            cur.execute(
                "INSERT INTO keyword_selections (username, team, keyword) VALUES (%s, %s, %s)",
                (username, team, keyword)
            )
            action = "selected"
        
        conn.commit()
        
        # Fetch updated selections in same connection
        cur.execute(
            """SELECT username, team, keyword, selected_at 
               FROM keyword_selections 
               ORDER BY selected_at DESC"""
        )
        rows = cur.fetchall()
        
        selections = []
        for row in rows:
            selections.append({
                "user": row[0],
                "team": row[1],
                "keyword": row[2],
                "timestamp": to_pakistan_time(row[3])
            })
        
        # Update cache
        selections_cache = selections
        
        return action, selections
        
    except Exception as e:
        print(f"[DB] Toggle selection error: {e}")
        return "error", selections_cache
    finally:
        if conn:
            cur.close()
            conn.close()

def load_selections_cache():
    """Load selections into cache on startup"""
    global selections_cache, cache_loaded
    selections_cache = db_get_all_selections()
    cache_loaded = True
    print(f"[Cache] Loaded {len(selections_cache)} selections")

# ------------------ Google Sheets Function ------------------
def get_google_sheet_data():
    """Fetch keywords from Google Sheet with all columns"""
    print("[SHEET] Starting to fetch Google Sheet data...", flush=True)
    try:
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        
        # print(f"[SHEET] Checking for credentials file: {CREDENTIALS_FILE}", flush=True)
        # print(f"[SHEET] Credentials file exists: {os.path.exists(CREDENTIALS_FILE)}", flush=True)
        
        # if not os.path.exists(CREDENTIALS_FILE):
        # if not os.path.exists(GOOGLE_CREDENTIALS_JSON):
        if not CREDS_FILE or not os.path.exists(CREDS_FILE):
            # Sample data with separate date and time columns
            return [
                {"id": 1, "keyword": "Sample Keyword 1", "title": "Breaking News Story", "remarks": "Hot topic", "category": "Tech", "hours_ago": "2h ago", "date": "05-01-2026", "time": "14:30:00"},
                {"id": 2, "keyword": "Sample Keyword 2", "title": "Latest Update", "remarks": "Trending", "category": "News", "hours_ago": "4h ago", "date": "05-01-2026", "time": "12:30:00"},
                {"id": 3, "keyword": "Sample Keyword 3", "title": "Match Highlights", "remarks": "Popular", "category": "Sports", "hours_ago": "1h ago", "date": "06-01-2026", "time": "15:30:00"},
                {"id": 4, "keyword": "Sample Keyword 4", "title": "Celebrity News", "remarks": "Viral", "category": "Entertainment", "hours_ago": "6h ago", "date": "06-01-2026", "time": "10:30:00"},
                {"id": 5, "keyword": "Sample Keyword 5", "title": "Market Analysis", "remarks": "Rising", "category": "Business", "hours_ago": "3h ago", "date": "07-01-2026", "time": "13:30:00"},
            ]
        
        print("[SHEET] Loading credentials...", flush=True)
        # creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
        creds = ServiceAccountCredentials.from_json_keyfile_name(CREDS_FILE, scope)    
        print("[SHEET] Authorizing with Google...", flush=True)
        client = gspread.authorize(creds)
        print(f"[SHEET] Opening sheet with ID: {SHEET_ID}", flush=True)
        sheet = client.open_by_key(SHEET_ID).sheet1
        print("[SHEET] Fetching all records...", flush=True)
        records = sheet.get_all_records()
        print(f"[SHEET] Found {len(records)} records", flush=True)
        keywords = []
        for i, record in enumerate(records):
            # Debug: Print column names from first record
            if i == 0:
                print(f"[DEBUG] Sheet columns: {list(record.keys())}")
                print(f"[DEBUG] First row raw data: {record}")
            
            # Get values - try multiple column name formats and strip whitespace from keys
            # Create a normalized dict with lowercase stripped keys
            normalized = {str(k).strip().lower(): v for k, v in record.items()}
            
            keyword = normalized.get("keywords") or normalized.get("keyword") or f"Keyword {i+1}"
            title = normalized.get("title") or normalized.get("titles") or ""
            
            # Debug title
            if i == 0:
                print(f"[DEBUG] Title value found: '{title}'")
                print(f"[DEBUG] 'title' in normalized: {'title' in normalized}")
                if 'title' in normalized:
                    print(f"[DEBUG] normalized['title'] = '{normalized['title']}'")
            remarks = normalized.get("remarks") or normalized.get("remark") or ""
            category = normalized.get("category") or "General"
            hours_ago = normalized.get("hours ago") or normalized.get("hours_ago") or ""
            
            # Separate date and time columns
            date_val = normalized.get("date") or ""
            time_val = normalized.get("time") or ""
            
            keywords.append({
                "id": i + 1,
                "keyword": str(keyword),
                "title": str(title),
                "remarks": str(remarks),
                "category": str(category),
                "hours_ago": str(hours_ago),
                "date": str(date_val),
                "time": str(time_val)
            })
        return keywords
    except Exception as e:
        print(f"[SHEET ERROR] Error fetching Google Sheet: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return [
            {"id": 1, "keyword": "Trending Topic 1", "title": "", "remarks": "", "category": "General", "hours_ago": "", "date": "", "time": ""},
            {"id": 2, "keyword": "Trending Topic 2", "title": "", "remarks": "", "category": "General", "hours_ago": "", "date": "", "time": ""},
            {"id": 3, "keyword": "Trending Topic 3", "title": "", "remarks": "", "category": "General", "hours_ago": "", "date": "", "time": ""},
        ]

# ------------------ Routes ------------------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    name = data.get('name', '').strip()
    team = data.get('team', '').strip()
    password = data.get('password', '').strip()
    
    if not name or not team or not password:
        return jsonify({"success": False, "message": "All fields are required"}), 400
    
    if len(password) < 4:
        return jsonify({"success": False, "message": "Password must be at least 4 characters"}), 400
    
    result = db_register_user(name, team, password)
    status_code = 200 if result["success"] else 400
    return jsonify(result), status_code

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    name = data.get('name', '').strip()
    password = data.get('password', '').strip()
    
    if not name or not password:
        return jsonify({"success": False, "message": "Name and password required"}), 400
    
    result = db_login_user(name, password)
    if result["success"]:
        session['user'] = name
        return jsonify(result)
    else:
        return jsonify(result), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    session.pop('user', None)
    return jsonify({"success": True})

@app.route('/api/reset-password', methods=['POST'])
def reset_password():
    data = request.json
    name = data.get('name', '').strip()
    team = data.get('team', '').strip()
    new_password = data.get('new_password', '').strip()
    
    if not name or not team or not new_password:
        return jsonify({"success": False, "message": "All fields are required"}), 400
    
    if len(new_password) < 4:
        return jsonify({"success": False, "message": "Password must be at least 4 characters"}), 400
    
    result = db_reset_password(name, team, new_password)
    status_code = 200 if result["success"] else 400
    return jsonify(result), status_code

@app.route('/api/keywords', methods=['GET'])
def get_keywords():
    keywords = get_google_sheet_data()
    # Debug: Print first keyword to see if title is there
    if keywords:
        print(f"[DEBUG] First keyword data: {keywords[0]}")
    return jsonify({"keywords": keywords})

@app.route('/api/selections', methods=['GET'])
def get_selections():
    global selections_cache, cache_loaded
    if not cache_loaded:
        load_selections_cache()
    return jsonify({"selections": selections_cache})

@app.route('/api/refresh-cache', methods=['POST'])
def refresh_cache():
    """Manually refresh the selections cache from database"""
    global selections_cache, cache_loaded
    selections_cache = db_get_all_selections()
    cache_loaded = True
    print(f"[Cache] Manually refreshed - {len(selections_cache)} selections")
    return jsonify({"success": True, "count": len(selections_cache)})

@app.route('/api/keyword-details/<keyword>', methods=['GET'])
def get_keyword_details(keyword):
    """Get all users who selected a specific keyword"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute(
            """SELECT username, team, selected_at 
               FROM keyword_selections 
               WHERE keyword = %s
               ORDER BY selected_at DESC""",
            (keyword,)
        )
        rows = cur.fetchall()
        
        users = []
        for row in rows:
            users.append({
                "user": row[0],
                "team": row[1],
                "timestamp": to_pakistan_time(row[2])
            })
        
        return jsonify({
            "keyword": keyword,
            "total_selections": len(users),
            "users": users
        })
        
    except Exception as e:
        print(f"[DB] Get keyword details error: {e}")
        return jsonify({"keyword": keyword, "total_selections": 0, "users": []})
    finally:
        if conn:
            cur.close()
            conn.close()

# ------------------ WebSocket Events ------------------
@socketio.on('connect')
def handle_connect():
    print(f"Client connected: {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    print(f"Client disconnected: {request.sid}")

@socketio.on('user_online')
def handle_user_online(data):
    username = data.get('username')
    if username:
        online_users.add(username)
        emit('online_users_update', list(online_users), broadcast=True)

@socketio.on('user_offline')
def handle_user_offline(data):
    username = data.get('username')
    if username and username in online_users:
        online_users.discard(username)
        emit('online_users_update', list(online_users), broadcast=True)

@socketio.on('select_keyword')
def handle_keyword_selection(data):
    username = data.get('username')
    team = data.get('team')
    keyword = data.get('keyword')
    
    if not username or not team or not keyword:
        return
    
    # Toggle selection in ONE database call (much faster!)
    action, selections = db_toggle_selection(username, team, keyword)
    
    # Broadcast to all clients
    emit('selection_update', {
        "selections": selections,
        "action": action,
        "user": username,
        "team": team,
        "keyword": keyword
    }, broadcast=True)

@socketio.on('refresh_keywords')
def handle_refresh_keywords():
    keywords = get_google_sheet_data()
    emit('keywords_update', {"keywords": keywords}, broadcast=True)

if __name__ == '__main__':
    # Create templates folder if not exists
    os.makedirs('templates', exist_ok=True)
    os.makedirs('static', exist_ok=True)
    
    # Initialize database tables
    print("[DB] Initializing database...")
    init_database()
    
    # Load selections cache
    load_selections_cache()
    
    print("Starting Keyword Selection App...")
    print("Open http://localhost:5000 in your browser")
    # socketio.run(app, debug=True, host='0.0.0.0', port=5000)
    socketio.run(app, host='0.0.0.0', port=10000, debug=False)
