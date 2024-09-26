import json
import psycopg2
from psycopg2.extras import DictCursor
from datetime import datetime

class Database:
    def __init__(self, host, port_id, database, user, password):
        self.host = host
        self.port_id = port_id
        self.database = database
        self.user = user
        self.password = password
        self.conn = None
        self.cursor = None

    def connect(self):
        try:
            self.conn = psycopg2.connect(
                host=self.host,
                port=self.port_id,
                database=self.database,
                user=self.user,
                password=self.password
            )
            self.cursor = self.conn.cursor(cursor_factory=DictCursor)
        except psycopg2.Error as e:
            print(f"Error connecting to database: {e}")

    def create_table(self):
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS subscription_keys (
                key TEXT PRIMARY KEY,
                user_id BIGINT,
                expiry_time TEXT
            )
        """)
        
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_sessions (
                chat_id BIGINT PRIMARY KEY,
                uuid VARCHAR(255) NOT NULL
            )
        """)
        
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS scripts (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                script_id VARCHAR(255) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_states (
                chat_id BIGINT PRIMARY KEY,
                state_data JSONB NOT NULL
            )
        """)
        
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_voice_settings (
                user_id BIGINT PRIMARY KEY,
                voice_name VARCHAR(255) NOT NULL
            )
        """)
        
        # New table for tracking user call counts
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_calls (
                user_id BIGINT PRIMARY KEY,
                call_count INTEGER DEFAULT 0
            )
        """)
        
        # New table for tracking banned users
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id BIGINT PRIMARY KEY,
                banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        self.conn.commit()

    def modify_user_id_column(self):
        self.cursor.execute("ALTER TABLE subscription_keys ALTER COLUMN user_id TYPE bigint;")
        self.conn.commit()

    def get_all_user_ids(self):
        try:
            self.cursor.execute("SELECT chat_id FROM user_sessions")
            return [row['chat_id'] for row in self.cursor.fetchall()]
        except Exception as e:
            print(f"Error retrieving user IDs: {e}")
            return []

    def check_and_remove_expired_keys(self):
        try:
            current_time = datetime.now()
            self.cursor.execute("DELETE FROM subscription_keys WHERE expiry_time <= %s RETURNING key, user_id", (current_time,))
            expired_keys = self.cursor.fetchall()
            self.conn.commit()
            if expired_keys:
                print(f"Removed expired keys: {expired_keys}")
        except Exception as e:
            print(f"Error removing expired keys: {e}")
            self.conn.rollback()

    # Subscription key management
    def insert_key(self, key, user_id, expiry_time):
        try:
            self.cursor.execute("INSERT INTO subscription_keys VALUES (%s, %s, %s)", (key, user_id, expiry_time))
            self.conn.commit()
        except Exception as e:
            print(f"Error inserting key: {e}")
            self.conn.rollback()

    def get_key(self, key):
        try:
            self.cursor.execute("SELECT * FROM subscription_keys WHERE key = %s", (key,))
            return self.cursor.fetchone()
        except Exception as e:
            print(f"Error getting key: {e}")
            return None

    def update_key(self, key, user_id, expiry_time):
        try:
            self.cursor.execute("UPDATE subscription_keys SET user_id = %s, expiry_time = %s WHERE key = %s", (user_id, expiry_time, key))
            self.conn.commit()
        except Exception as e:
            print(f"Error updating key: {e}")
            self.conn.rollback()

    # User session management
    def store_session(self, chat_id, uuid):
        try:
            self.cursor.execute(
                "INSERT INTO user_sessions (chat_id, uuid) VALUES (%s, %s) ON CONFLICT (chat_id) DO UPDATE SET uuid = %s",
                (chat_id, uuid, uuid)
            )
            self.conn.commit()
            print(f"Session for chat_id {chat_id} stored successfully.")
        except Exception as e:
            print(f"Error storing session: {e}")
            self.conn.rollback()

    def get_session(self, chat_id):
        try:
            self.cursor.execute("SELECT * FROM user_sessions WHERE chat_id = %s", (chat_id,))
            return self.cursor.fetchone()
        except Exception as e:
            print(f"Error getting session: {e}")
            return None

    # Script management
    def insert_script(self, user_id, script_id):
        try:
            self.cursor.execute(
                "INSERT INTO scripts (user_id, script_id) VALUES (%s, %s) RETURNING id;",
                (user_id, script_id)
            )
            self.conn.commit()
            return self.cursor.fetchone()['id']
        except Exception as e:
            print(f"Error inserting script: {e}")
            self.conn.rollback()
            return None

    def get_script(self, script_id):
        try:
            self.cursor.execute("SELECT * FROM scripts WHERE script_id = %s", (script_id,))
            return self.cursor.fetchone()
        except Exception as e:
            print(f"Error getting script: {e}")
            return None

    def get_all_keys(self):
        try:
            self.cursor.execute("SELECT key, user_id, expiry_time FROM subscription_keys")
            return self.cursor.fetchall()
        except Exception as e:
            print(f"Error retrieving all keys: {e}")
            return None

    def remove_key_and_user(self, key, user_id):
        try:
            self.cursor.execute("DELETE FROM subscription_keys WHERE key = %s RETURNING key, user_id", (key,))
            self.conn.commit()
            print(f"Removed key {key} and user {user_id} from the database.")
        except Exception as e:
            print(f"Error removing key and user: {e}")
            self.conn.rollback()

    # State management
    def save_state(self, chat_id, state_data):
        try:
            self.cursor.execute(
                "INSERT INTO user_states (chat_id, state_data) VALUES (%s, %s) "
                "ON CONFLICT (chat_id) DO UPDATE SET state_data = %s",
                (chat_id, json.dumps(state_data), json.dumps(state_data))
            )
            self.conn.commit()
        except Exception as e:
            print(f"Error saving state: {e}")
            self.conn.rollback()

    def get_state(self, chat_id):
        try:
            self.cursor.execute("SELECT state_data FROM user_states WHERE chat_id = %s", (chat_id,))
            result = self.cursor.fetchone()
            if result:
                return json.loads(result['state_data']) if isinstance(result['state_data'], str) else result['state_data']
            else:
                return {}
        except Exception as e:
            print(f"Error retrieving state: {e}")
            return {}

    def get_key_details(self, user_id):
        try:
            self.cursor.execute("SELECT key, expiry_time FROM subscription_keys WHERE user_id = %s", (user_id,))
            result = self.cursor.fetchone()
            return result if result else None
        except Exception as e:
            print(f"Error retrieving key details: {e}")
            return None

    # Voice name management
    def save_voice_name(self, user_id, voice_name):
        try:
            self.cursor.execute(
                "INSERT INTO user_voice_settings (user_id, voice_name) VALUES (%s, %s) "
                "ON CONFLICT (user_id) DO UPDATE SET voice_name = %s",
                (user_id, voice_name, voice_name)
            )
            self.conn.commit()
        except Exception as e:
            print(f"Error saving voice name: {e}")
            self.conn.rollback()

    def get_voice_name(self, user_id):
        try:
            self.cursor.execute("SELECT voice_name FROM user_voice_settings WHERE user_id = %s", (user_id,))
            result = self.cursor.fetchone()
            return result['voice_name'] if result else None
        except Exception as e:
            print(f"Error retrieving voice name: {e}")
            return None

    # Call tracking - new functions
    def increment_call_count(self, user_id):
        try:
            self.cursor.execute('''
                INSERT INTO user_calls (user_id, call_count)
                VALUES (%s, 1)
                ON CONFLICT (user_id)
                DO UPDATE SET call_count = user_calls.call_count + 1
            ''', (user_id,))
            self.conn.commit()
        except Exception as e:
            print(f"Error incrementing call count: {e}")
            self.conn.rollback()

    def get_call_count(self, user_id):
        try:
            self.cursor.execute("SELECT call_count FROM user_calls WHERE user_id = %s", (user_id,))
            result = self.cursor.fetchone()
            return result['call_count'] if result else 0
        except Exception as e:
            print(f"Error retrieving call count: {e}")
            return 0

    def get_all_call_counts(self):
        try:
            self.cursor.execute("SELECT user_id, call_count FROM user_calls")
            return self.cursor.fetchall()
        except Exception as e:
            print(f"Error retrieving all call counts: {e}")
            return []

    # New function: Check if a user is banned
    def is_user_banned(self, user_id):
        try:
            # Check if the user is in the banned_users table
            self.cursor.execute("SELECT 1 FROM banned_users WHERE user_id = %s", (user_id,))
            result = self.cursor.fetchone()
            return result is not None
        except Exception as e:
            print(f"Error checking if user is banned: {e}")
            return False

    def close(self):
        if self.conn:
            self.conn.close()
