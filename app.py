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
        
        # Create users table with is_admin column
        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                team TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # Add is_admin column if it doesn't exist (for existing databases)
        cur.execute("""
            DO $$ 
            BEGIN 
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                              WHERE table_name='app_users' AND column_name='is_admin') THEN
                    ALTER TABLE app_users ADD COLUMN is_admin BOOLEAN DEFAULT FALSE;
                END IF;
            END $$;
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
            "SELECT username, team, password_hash, COALESCE(is_admin, FALSE) FROM app_users WHERE username = %s",
            (username,)
        )
        user = cur.fetchone()
        
        if not user:
            return {"success": False, "message": "User not found"}
        
        if not verify_password(password, user[2]):
            return {"success": False, "message": "Invalid password"}
        
        return {
            "success": True,
            "user": {"name": user[0], "team": user[1], "is_admin": user[3]}
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

# ------------------ Admin Database Functions ------------------
def db_get_all_users():
    """Get all users with their stats"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT 
                u.id,
                u.username, 
                u.team, 
                COALESCE(u.is_admin, FALSE) as is_admin,
                u.created_at,
                COUNT(ks.id) as total_selections,
                MAX(ks.selected_at) as last_selection
            FROM app_users u
            LEFT JOIN keyword_selections ks ON u.username = ks.username
            GROUP BY u.id, u.username, u.team, u.is_admin, u.created_at
            ORDER BY total_selections DESC
        """)
        rows = cur.fetchall()
        
        users = []
        for row in rows:
            users.append({
                "id": row[0],
                "username": row[1],
                "team": row[2],
                "is_admin": row[3],
                "created_at": to_pakistan_time(row[4]) if row[4] else None,
                "total_selections": row[5],
                "last_selection": to_pakistan_time(row[6]) if row[6] else None
            })
        return users
        
    except Exception as e:
        print(f"[DB] Get all users error: {e}")
        return []
    finally:
        if conn:
            cur.close()
            conn.close()

def db_get_user_selections(username, from_date=None, to_date=None):
    """Get all selections for a specific user with optional date filter"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        query = """
            SELECT keyword, team, selected_at 
            FROM keyword_selections 
            WHERE username = %s
        """
        params = [username]
        
        if from_date:
            query += " AND selected_at >= %s"
            params.append(from_date)
        if to_date:
            query += " AND selected_at <= %s"
            params.append(to_date + " 23:59:59")
        
        query += " ORDER BY selected_at DESC"
        
        cur.execute(query, params)
        rows = cur.fetchall()
        
        selections = []
        for row in rows:
            selections.append({
                "keyword": row[0],
                "team": row[1],
                "timestamp": to_pakistan_time(row[2])
            })
        return selections
        
    except Exception as e:
        print(f"[DB] Get user selections error: {e}")
        return []
    finally:
        if conn:
            cur.close()
            conn.close()

def db_get_admin_stats(from_date=None, to_date=None):
    """Get overall statistics for admin dashboard"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Base date filter
        date_filter = ""
        params = []
        if from_date:
            date_filter += " AND selected_at >= %s"
            params.append(from_date)
        if to_date:
            date_filter += " AND selected_at <= %s"
            params.append(to_date + " 23:59:59")
        
        # Total users
        cur.execute("SELECT COUNT(*) FROM app_users")
        total_users = cur.fetchone()[0]
        
        # Total selections (with date filter)
        cur.execute(f"SELECT COUNT(*) FROM keyword_selections WHERE 1=1 {date_filter}", params)
        total_selections = cur.fetchone()[0]
        
        # Selections by team (with date filter)
        cur.execute(f"""
            SELECT team, COUNT(*) as count 
            FROM keyword_selections 
            WHERE 1=1 {date_filter}
            GROUP BY team 
            ORDER BY count DESC
        """, params)
        team_stats = [{"team": row[0], "count": row[1]} for row in cur.fetchall()]
        
        # Selections by date (last 30 days)
        cur.execute(f"""
            SELECT DATE(selected_at) as date, COUNT(*) as count 
            FROM keyword_selections 
            WHERE selected_at >= CURRENT_DATE - INTERVAL '30 days' {date_filter}
            GROUP BY DATE(selected_at) 
            ORDER BY date DESC
            LIMIT 30
        """, params)
        daily_stats = [{"date": str(row[0]), "count": row[1]} for row in cur.fetchall()]
        
        # Top users (with date filter)
        cur.execute(f"""
            SELECT username, team, COUNT(*) as count 
            FROM keyword_selections 
            WHERE 1=1 {date_filter}
            GROUP BY username, team 
            ORDER BY count DESC 
            LIMIT 10
        """, params)
        top_users = [{"username": row[0], "team": row[1], "count": row[2]} for row in cur.fetchall()]
        
        # Most selected keywords (with date filter)
        cur.execute(f"""
            SELECT keyword, COUNT(*) as count 
            FROM keyword_selections 
            WHERE 1=1 {date_filter}
            GROUP BY keyword 
            ORDER BY count DESC 
            LIMIT 10
        """, params)
        top_keywords = [{"keyword": row[0], "count": row[1]} for row in cur.fetchall()]
        
        return {
            "total_users": total_users,
            "total_selections": total_selections,
            "team_stats": team_stats,
            "daily_stats": daily_stats,
            "top_users": top_users,
            "top_keywords": top_keywords
        }
        
    except Exception as e:
        print(f"[DB] Get admin stats error: {e}")
        return {
            "total_users": 0,
            "total_selections": 0,
            "team_stats": [],
            "daily_stats": [],
            "top_users": [],
            "top_keywords": []
        }
    finally:
        if conn:
            cur.close()
            conn.close()

def db_set_admin(username, is_admin=True):
    """Set or remove admin status for a user"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute(
            "UPDATE app_users SET is_admin = %s WHERE username = %s RETURNING username",
            (is_admin, username)
        )
        result = cur.fetchone()
        conn.commit()
        
        if result:
            return {"success": True, "message": f"Admin status {'granted' if is_admin else 'revoked'} for {username}"}
        return {"success": False, "message": "User not found"}
        
    except Exception as e:
        print(f"[DB] Set admin error: {e}")
        return {"success": False, "message": "Database error"}
    finally:
        if conn:
            cur.close()
            conn.close()

def db_check_admin(username):
    """Check if user is admin"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute(
            "SELECT COALESCE(is_admin, FALSE) FROM app_users WHERE username = %s",
            (username,)
        )
        result = cur.fetchone()
        return result[0] if result else False
        
    except Exception as e:
        print(f"[DB] Check admin error: {e}")
        return False
    finally:
        if conn:
            cur.close()
            conn.close()

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
        
        print(f"[SHEET] Checking for credentials file: {CREDS_FILE}", flush=True)
        print(f"[SHEET] Credentials file exists: {os.path.exists(CREDS_FILE)}", flush=True)
        
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

# ------------------ Admin Routes ------------------
@app.route('/admin')
def admin_page():
    """Render admin dashboard page"""
    return render_template('admin.html')

@app.route('/api/admin/verify', methods=['POST'])
def verify_admin():
    """Verify if current user is admin"""
    data = request.json
    username = data.get('username', '')
    
    is_admin = db_check_admin(username)
    return jsonify({"is_admin": is_admin})

@app.route('/api/admin/stats', methods=['GET'])
def get_admin_stats():
    """Get admin dashboard statistics"""
    # Get query parameters for date filtering
    from_date = request.args.get('from_date')
    to_date = request.args.get('to_date')
    
    stats = db_get_admin_stats(from_date, to_date)
    return jsonify(stats)

@app.route('/api/admin/users', methods=['GET'])
def get_all_users():
    """Get all users with their stats"""
    users = db_get_all_users()
    return jsonify({"users": users})

@app.route('/api/admin/user/<username>/selections', methods=['GET'])
def get_user_selections(username):
    """Get selections for a specific user"""
    from_date = request.args.get('from_date')
    to_date = request.args.get('to_date')
    
    selections = db_get_user_selections(username, from_date, to_date)
    return jsonify({
        "username": username,
        "selections": selections,
        "total": len(selections)
    })

@app.route('/api/admin/set-admin', methods=['POST'])
def set_user_admin():
    """Set or remove admin status for a user"""
    data = request.json
    requester = data.get('requester', '')
    target_user = data.get('username', '')
    is_admin = data.get('is_admin', False)
    
    # Verify requester is admin
    if not db_check_admin(requester):
        return jsonify({"success": False, "message": "Unauthorized"}), 403
    
    result = db_set_admin(target_user, is_admin)
    return jsonify(result)

@app.route('/api/admin/today-selections', methods=['GET'])
def get_today_selections():
    """Get all selections made today"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Get today's selections
        cur.execute("""
            SELECT username, team, keyword, selected_at 
            FROM keyword_selections 
            WHERE DATE(selected_at) = CURRENT_DATE
            ORDER BY selected_at DESC
        """)
        rows = cur.fetchall()
        
        selections = []
        unique_users = set()
        unique_keywords = set()
        
        for row in rows:
            selections.append({
                "user": row[0],
                "team": row[1],
                "keyword": row[2],
                "timestamp": to_pakistan_time(row[3])
            })
            unique_users.add(row[0])
            unique_keywords.add(row[2])
        
        return jsonify({
            "selections": selections,
            "total": len(selections),
            "unique_users": len(unique_users),
            "unique_keywords": len(unique_keywords)
        })
        
    except Exception as e:
        print(f"[DB] Get today selections error: {e}")
        return jsonify({
            "selections": [],
            "total": 0,
            "unique_users": 0,
            "unique_keywords": 0
        })
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
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
