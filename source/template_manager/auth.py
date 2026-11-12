from contextlib import contextmanager
import hashlib
import hmac
import secrets
import sqlite3
import threading
import time


class Auth:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, salt TEXT, hash TEXT, role TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, username TEXT, expires REAL)')
        self.failures = {}
        self.failures_lock = threading.Lock()

    @contextmanager
    def db(self):
        # sqlite3's own context manager only commits; close explicitly so the
        # file handle is released at once (Windows cannot move or delete open files).
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, username, password, role):
        username = username.strip()
        if not username or len(username) > 80 or len(password) < 12 or len(password) > 256:
            raise ValueError('用户名须为 1–80 字，密码须为 12–256 字符')
        if role not in ('admin', 'editor', 'viewer'):
            raise ValueError('无效权限')
        salt = secrets.token_hex(16)
        hashed = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600000).hex()
        with self.db() as db:
            db.execute('INSERT INTO users VALUES (?,?,?,?)', (username, salt, hashed, role))

    def login(self, username, password, ip):
        now = time.time()
        # Logins run in parallel: count this attempt up front (under a lock) so concurrent
        # guesses cannot all slip past the limit; a successful login clears the record.
        with self.failures_lock:
            attempts = [t for t in self.failures.get(ip, []) if now - t < 900]
            if len(attempts) >= 10:
                self.failures[ip] = attempts
                raise ValueError('登录尝试过多，请 15 分钟后再试')
            self.failures[ip] = attempts + [now]
        with self.db() as db:
            row = db.execute('SELECT salt,hash FROM users WHERE username=?', (username,)).fetchone()
        salt = row[0] if row else '00' * 16
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600000).hex()
        if not row or not hmac.compare_digest(actual, row[1]):
            raise ValueError('用户名或密码错误')
        with self.failures_lock:
            self.failures.pop(ip, None)
        with self.db() as db:
            token = secrets.token_urlsafe(32)
            db.execute('DELETE FROM sessions WHERE expires < ?', (now,))
            db.execute('INSERT INTO sessions VALUES (?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), username, now + 43200))
            return token

    def user(self, token):
        with self.db() as db:
            row = db.execute('SELECT u.username,u.role FROM sessions s JOIN users u ON u.username=s.username WHERE s.token=? AND s.expires>?', (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        return {'username': row[0], 'role': row[1]} if row else None

    def logout(self, token):
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE token=?', (hashlib.sha256(token.encode()).hexdigest(),))
