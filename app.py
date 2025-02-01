from flask import Flask, request, Response, render_template, jsonify
# import ollama
import json
import requests
import logging
import os
import fcntl  # For file locking
import sqlite3
from datetime import datetime

app = Flask(__name__)
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Database setup
DB_FILE = 'chat_histories.db'

def init_db():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            # Create chats table
            c.execute('''
                CREATE TABLE IF NOT EXISTS chats (
                    id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    title TEXT
                )
            ''')
            # Create messages table
            c.execute('''
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY (chat_id) REFERENCES chats (id)
                )
            ''')
            conn.commit()
            logger.info("Database initialized successfully")
    except sqlite3.Error as e:
        logger.error(f"Database initialization error: {e}")
        raise

# 流式响应路由
@app.route('/api/generate', methods=['POST'])
def generate():
    print("Received request to /api/generate") # Debug log
    
    # Log raw request data
    print("Request headers:", dict(request.headers))
    print("Request data:", request.get_data(as_text=True))
    
    data = request.json
    print("Parsed JSON data:", data) # Debug log
    
    prompt = data.get('prompt', '')
    model = data.get('model', 'deepseek-r1:8b')
    messages = data.get('messages', [])

    print(f"Processing request - Prompt: {prompt}, Model: {model}, Messages: {messages}") # Debug log

    def generate_stream():
        try:
            print("Making request to Ollama API") # Debug log
            response = requests.post(
                'http://localhost:11434/api/chat',
                # 'http://211.71.56.78:11434/api/chat',
                json={
                    'model': model,
                    'messages': messages + [{'role': 'user', 'content': prompt}],
                    'stream': True
                },
                stream=True
            )
            
            print(f"Ollama API response status: {response.status_code}") # Debug log
            
            # Send model info as the first chunk
            yield f"data: [Using model: {model}]\n\n"
            
            for line in response.iter_lines():
                if line:
                    json_response = json.loads(line.decode('utf-8'))
                    if 'message' in json_response and 'content' in json_response['message']:
                        chunk = f"data: {json.dumps(json_response['message']['content'])}\n\n"
                        yield chunk
                
        except Exception as e:
            print(f"Error in generate_stream: {str(e)}") # Debug log
            yield f"data: [ERROR] {str(e)}\n\n"

    return Response(generate_stream(), mimetype='text/event-stream')

# 主页面路由
@app.route('/')
def index():
    return render_template('index.html')

# Add these functions to handle file-based history
def save_history(messages):
    with open('chat_history.json', 'w', encoding='utf-8') as f:
        json.dump(messages, f, ensure_ascii=False, indent=2)

def load_history():
    try:
        with open('chat_history.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return []

@app.route('/api/history', methods=['GET', 'POST', 'DELETE'])
def handle_history():
    if request.method == 'GET':
        return json.dumps(load_history())
    elif request.method == 'POST':
        messages = request.json
        save_history(messages)
        return json.dumps({"status": "success"})
    elif request.method == 'DELETE':
        if os.path.exists('chat_history.json'):
            os.remove('chat_history.json')
        return json.dumps({"status": "success"})

HISTORY_FILE = 'chat_histories.json'

def load_histories():
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)  # Shared lock for reading
                histories = json.load(f)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)  # Release lock
                return histories
        return []
    except Exception as e:
        print(f"Error loading histories: {str(e)}")
        return []

@app.route('/api/histories', methods=['GET', 'POST'])
def handle_histories():
    if request.method == 'GET':
        try:
            logger.debug("Attempting to fetch histories from database")
            with sqlite3.connect(DB_FILE) as conn:
                conn.row_factory = sqlite3.Row
                c = conn.cursor()
                
                # Modified query to properly handle JSON
                c.execute('''
                    SELECT 
                        c.id,
                        c.timestamp,
                        c.title,
                        json_group_array(
                            json_object(
                                'role', m.role,
                                'content', m.content,
                                'timestamp', m.timestamp
                            )
                        ) as messages
                    FROM chats c
                    LEFT JOIN messages m ON c.id = m.chat_id
                    GROUP BY c.id
                    ORDER BY c.timestamp DESC
                ''')
                
                rows = c.fetchall()
                logger.debug(f"Found {len(rows)} chats in database")
                
                histories = []
                for row in rows:
                    chat_id = row['id']
                    timestamp = row['timestamp']
                    title = row['title']
                    messages_json = row['messages']
                    
                    try:
                        # Parse the JSON array of messages
                        messages = json.loads(messages_json)
                        # Filter out null messages that might come from LEFT JOIN
                        messages = [m for m in messages if m['role'] is not None]
                        
                        histories.append({
                            'id': chat_id,
                            'timestamp': timestamp,
                            'title': title,
                            'messages': messages
                        })
                        logger.debug(f"Processed chat {chat_id} with {len(messages)} messages")
                        
                    except json.JSONDecodeError as e:
                        logger.error(f"Error parsing messages JSON for chat {chat_id}: {e}")
                        logger.error(f"Raw messages JSON: {messages_json}")
                
                logger.debug(f"Successfully prepared {len(histories)} histories for sending")
                return jsonify(histories)
                
        except sqlite3.Error as e:
            logger.error(f"Database error while fetching histories: {e}")
            return jsonify({"error": "Database error", "message": str(e)}), 500
        except Exception as e:
            logger.error(f"Unexpected error while fetching histories: {e}")
            return jsonify({"error": "Unexpected error", "message": str(e)}), 500
            
    elif request.method == 'POST':
        try:
            new_histories = request.json
            logger.debug(f"Received POST request with {len(new_histories)} histories")
            
            with sqlite3.connect(DB_FILE) as conn:
                c = conn.cursor()
                
                for chat in new_histories:
                    chat_id = chat['id']
                    timestamp = chat.get('timestamp', datetime.now().isoformat())
                    title = chat.get('title', '')
                    messages = chat.get('messages', [])
                    
                    logger.debug(f"Processing chat {chat_id} with {len(messages)} messages")
                    
                    # Update or insert chat
                    c.execute('''
                        INSERT OR REPLACE INTO chats (id, timestamp, title)
                        VALUES (?, ?, ?)
                    ''', (chat_id, timestamp, title))
                    
                    # Update messages for this chat
                    if messages:
                        c.execute('DELETE FROM messages WHERE chat_id = ?', (chat_id,))
                        
                        # Insert new messages
                        for msg in messages:
                            c.execute('''
                                INSERT INTO messages (chat_id, role, content, timestamp)
                                VALUES (?, ?, ?, ?)
                            ''', (
                                chat_id,
                                msg['role'],
                                msg['content'],
                                msg.get('timestamp', datetime.now().isoformat())
                            ))
                
                conn.commit()
                logger.debug("Successfully saved all histories to database")
            
            return jsonify({"status": "success"})
            
        except sqlite3.Error as e:
            logger.error(f"Database error while saving histories: {e}")
            return jsonify({"error": "Database error", "message": str(e)}), 500
        except Exception as e:
            logger.error(f"Unexpected error while saving histories: {e}")
            return jsonify({"error": "Unexpected error", "message": str(e)}), 500

# Initialize database when the app starts
# init_db()

if __name__ == '__main__':
    app.run(debug=True, port=5000)