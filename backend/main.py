"""
Tender Analysis System - Backend
Парсит тендеры с goszakup.gov.kz и zakup.sk.kz,
анализирует их через Claude AI и показывает клиентам
"""

import json
import os
import sqlite3
import hashlib
import secrets
import requests
from datetime import datetime, date
from typing import Optional

# ============================================================
# НАСТРОЙКИ
# ============================================================

GOSZAKUP_GRAPHQL_URL = "https://ows.goszakup.gov.kz/v3/graphql"

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GOSZAKUP_API_TOKEN = os.environ.get("GOSZAKUP_TOKEN", "ВАШ_ТОКЕН_ЗДЕСЬ")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "..", "tenders.db")

# ============================================================
# БАЗА ДАННЫХ
# ============================================================

def init_db():
    """Инициализация базы данных"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Клиенты
    c.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            keywords TEXT,        -- ключевые слова через запятую
            industry TEXT,        -- отрасль
            capabilities TEXT,    -- что умеет делать компания
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Тендеры
    c.execute("""
        CREATE TABLE IF NOT EXISTS tenders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            external_id TEXT UNIQUE,
            source TEXT,          -- goszakup / samruk
            title TEXT,
            description TEXT,
            budget REAL,
            currency TEXT,
            deadline TEXT,
            customer TEXT,
            url TEXT,
            raw_data TEXT,        -- полный JSON от API
            fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Результаты анализа
    c.execute("""
        CREATE TABLE IF NOT EXISTS analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tender_id INTEGER,
            client_id INTEGER,
            recommendation TEXT,  -- YES / NO / MAYBE
            score INTEGER,        -- 0-100
            reasons TEXT,
            risks TEXT,
            summary TEXT,
            analyzed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(tender_id) REFERENCES tenders(id),
            FOREIGN KEY(client_id) REFERENCES clients(id)
        )
    """)

    # Пользователи
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            client_id INTEGER,
            is_admin INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(client_id) REFERENCES clients(id)
        )
    """)

    # Сессии (токены)
    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    conn.commit()
    conn.close()
    print("✅ База данных готова")


# ============================================================
# АВТОРИЗАЦИЯ
# ============================================================

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def create_user(username: str, password: str, client_id=None, is_admin=0):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, client_id, is_admin) VALUES (?, ?, ?, ?)",
            (username, hash_password(password), client_id, is_admin)
        )
        conn.commit()
        print(f"✅ Пользователь '{username}' создан")
    except sqlite3.IntegrityError:
        pass  # уже существует
    finally:
        conn.close()

def ensure_admin():
    """Создаёт admin если не существует"""
    conn = sqlite3.connect(DB_PATH)
    exists = conn.execute("SELECT 1 FROM users WHERE username='admin'").fetchone()
    conn.close()
    if not exists:
        create_user("admin", "admin123", client_id=None, is_admin=1)
        print("✅ Создан admin / пароль: admin123")

def verify_token(token: str):
    """Возвращает пользователя по токену или None"""
    if not token:
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    user = conn.execute("""
        SELECT u.* FROM users u
        JOIN sessions s ON s.user_id = u.id
        WHERE s.token = ?
    """, [token]).fetchone()
    conn.close()
    return dict(user) if user else None


# ============================================================
# ПАРСЕР GOSZAKUP.GOV.KZ (GraphQL API)
# ============================================================

def fetch_goszakup_tenders(limit=50, days_back=1):
    """
    Получает свежие тендеры с goszakup через GraphQL API
    Документация: https://ows.goszakup.gov.kz/help/v3/schema/
    """
    query = """
    query($limit: Int, $after: Int, $filter: TrdBuyFiltersInput!) {
        trd_buy(limit: $limit, after: $after, filters: $filter) {
            id
            name_ru
            name_kz
            number_anno
            total_sum
            ref_trade_methods_id
            start_date
            end_date
            count_lots
            customer_bin
            ref_subject_type {
                name_ru
            }
        }
    }
    """

    variables = {
        "limit": limit,
        "after": 0,
        "filter": {
            "ref_buy_status_id": [210, 220]  # активные тендеры
        }
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GOSZAKUP_API_TOKEN}"
    }

    try:
        response = requests.post(
            GOSZAKUP_GRAPHQL_URL,
            json={"query": query, "variables": variables},
            headers=headers,
            timeout=30
        )
        data = response.json()

        if "errors" in data:
            print(f"❌ Ошибка API: {data['errors']}")
            return []

        tenders = data.get("data", {}).get("trd_buy", [])
        print(f"✅ Получено {len(tenders)} тендеров с goszakup.gov.kz")
        return tenders

    except Exception as e:
        print(f"❌ Ошибка подключения к goszakup: {e}")
        return []


def save_goszakup_tenders(tenders):
    """Сохраняет тендеры в базу данных"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    saved = 0

    for t in tenders:
        external_id = f"goszakup_{t['id']}"
        url = f"https://goszakup.gov.kz/ru/announce/index/{t['id']}"

        try:
            c.execute("""
                INSERT OR IGNORE INTO tenders
                (external_id, source, title, description, budget, currency, deadline, customer, url, raw_data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                external_id,
                "goszakup",
                t.get("name_ru", "Без названия"),
                t.get("ref_subject_type", {}).get("name_ru", ""),
                t.get("total_sum", 0),
                "KZT",
                t.get("end_date", ""),
                t.get("customer_bin", ""),
                url,
                json.dumps(t, ensure_ascii=False)
            ))
            if conn.total_changes > saved:
                saved += 1
        except Exception as e:
            print(f"  Ошибка сохранения тендера {t['id']}: {e}")

    conn.commit()
    conn.close()
    print(f"✅ Сохранено новых тендеров: {saved}")
    return saved


# ============================================================
# ПАРСЕР ZAKUP.SK.KZ (Самрук-Казына)
# ============================================================

def fetch_samruk_tenders(limit=50):
    """
    Получает тендеры с Самрук-Казына
    Самрук использует REST API
    """
    url = "https://zakup.sk.kz/eproc-ebs/api/open/announce/list"

    params = {
        "page": 0,
        "size": limit,
        "sort": "publishDate,desc",
        "statusId": "1"  # активные
    }

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    try:
        response = requests.get(url, params=params, headers=headers, timeout=30)
        if response.status_code == 200:
            data = response.json()
            tenders = data.get("content", data if isinstance(data, list) else [])
            print(f"✅ Получено {len(tenders)} тендеров с zakup.sk.kz")
            return tenders
        else:
            print(f"❌ Самрук вернул статус {response.status_code}")
            return []
    except Exception as e:
        print(f"❌ Ошибка подключения к Самрук: {e}")
        return []


def save_samruk_tenders(tenders):
    """Сохраняет тендеры Самрука в базу"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    saved = 0

    for t in tenders:
        tid = t.get("id") or t.get("announceId") or t.get("lotId")
        if not tid:
            continue

        external_id = f"samruk_{tid}"
        url = f"https://zakup.sk.kz/eproc-ebs/#/open/announce/{tid}"

        try:
            c.execute("""
                INSERT OR IGNORE INTO tenders
                (external_id, source, title, description, budget, currency, deadline, customer, url, raw_data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                external_id,
                "samruk",
                t.get("nameRu") or t.get("name", "Без названия"),
                t.get("descriptionRu") or t.get("description", ""),
                t.get("totalSum") or t.get("sum", 0),
                t.get("currency", "KZT"),
                t.get("endDate") or t.get("deadline", ""),
                t.get("customerName", ""),
                url,
                json.dumps(t, ensure_ascii=False)
            ))
            if conn.total_changes > saved:
                saved += 1
        except Exception as e:
            print(f"  Ошибка сохранения самрук тендера {tid}: {e}")

    conn.commit()
    conn.close()
    print(f"✅ Сохранено новых тендеров Самрука: {saved}")
    return saved


# ============================================================
# AI АНАЛИЗ (Claude)
# ============================================================

def analyze_tender_for_client(tender: dict, client: dict) -> dict:
    """
    Анализирует тендер на соответствие профилю клиента через Groq AI (Llama 3.3)
    """
    prompt = f"""Ты эксперт по государственным закупкам Казахстана.

ПРОФИЛЬ КОМПАНИИ КЛИЕНТА:
- Название: {client['name']}
- Описание: {client['description']}
- Отрасль: {client['industry']}
- Возможности: {client['capabilities']}
- Ключевые слова: {client['keywords']}

ТЕНДЕР:
- Источник: {tender['source']}
- Название: {tender['title']}
- Описание: {tender['description']}
- Бюджет: {tender['budget']:,.0f} {tender['currency']}
- Дедлайн: {tender['deadline']}
- Заказчик: {tender['customer']}
- Ссылка: {tender['url']}

Проанализируй соответствие этого тендера профилю компании и ответь ТОЛЬКО в JSON формате:
{{
    "recommendation": "YES" | "NO" | "MAYBE",
    "score": <число от 0 до 100>,
    "summary": "<краткий вывод 2-3 предложения>",
    "reasons": ["<причина 1>", "<причина 2>", ...],
    "risks": ["<риск 1>", "<риск 2>", ...],
    "action": "<что нужно сделать если участвовать>"
}}

YES = однозначно участвовать
NO = не подходит
MAYBE = можно рассмотреть при определённых условиях"""

    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 1000,
                "temperature": 0.3
            },
            timeout=30
        )
        data = resp.json()
        response_text = data["choices"][0]["message"]["content"]
        response_text = response_text.replace("```json", "").replace("```", "").strip()
        result = json.loads(response_text)
        return result

    except Exception as e:
        print(f"  ❌ Ошибка AI анализа: {e}")
        return {
            "recommendation": "MAYBE",
            "score": 50,
            "summary": "Не удалось выполнить анализ",
            "reasons": [],
            "risks": ["Ошибка анализа"],
            "action": "Проверить вручную"
        }


def run_analysis_for_all_clients():
    """Запускает анализ всех непроверенных тендеров для всех клиентов"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    clients = c.execute("SELECT * FROM clients").fetchall()
    tenders = c.execute("""
        SELECT * FROM tenders
        WHERE id NOT IN (
            SELECT DISTINCT tender_id FROM analyses
        )
        ORDER BY fetched_at DESC
        LIMIT 20
    """).fetchall()

    print(f"\n🤖 Анализирую {len(tenders)} тендеров для {len(clients)} клиентов...")

    for tender in tenders:
        for client in clients:
            print(f"  → {tender['title'][:50]}... для {client['name']}")
            result = analyze_tender_for_client(dict(tender), dict(client))

            c.execute("""
                INSERT INTO analyses
                (tender_id, client_id, recommendation, score, reasons, risks, summary)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                tender["id"],
                client["id"],
                result.get("recommendation", "MAYBE"),
                result.get("score", 50),
                json.dumps(result.get("reasons", []), ensure_ascii=False),
                json.dumps(result.get("risks", []), ensure_ascii=False),
                result.get("summary", "")
            ))
            conn.commit()

    conn.close()
    print("✅ Анализ завершён")


# ============================================================
# ДОБАВЛЕНИЕ ТЕСТОВЫХ КЛИЕНТОВ
# ============================================================

def add_sample_clients():
    """Добавляет примеры клиентов для тестирования"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Проверяем есть ли уже клиенты
    count = c.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    if count > 0:
        conn.close()
        return

    clients = [
        {
            "name": "ТехноСтрой KZ",
            "description": "Строительная компания, специализируется на возведении коммерческих и жилых объектов",
            "industry": "Строительство",
            "keywords": "строительство, ремонт, реконструкция, здание, сооружение",
            "capabilities": "Строительство зданий до 15 этажей, дорожные работы, инженерные сети"
        },
        {
            "name": "ДатаСофт Казахстан",
            "description": "IT компания, разработка программного обеспечения и внедрение информационных систем",
            "industry": "IT / Программное обеспечение",
            "keywords": "программное обеспечение, система, автоматизация, разработка, IT, цифровизация",
            "capabilities": "Разработка веб-систем, мобильных приложений, ERP, интеграции с государственными системами"
        }
    ]

    for cl in clients:
        c.execute("""
            INSERT INTO clients (name, description, industry, keywords, capabilities)
            VALUES (?, ?, ?, ?, ?)
        """, (cl["name"], cl["description"], cl["industry"], cl["keywords"], cl["capabilities"]))

    conn.commit()
    conn.close()
    print("✅ Добавлены тестовые клиенты")


# ============================================================
# ДОБАВЛЕНИЕ ДЕМО-ТЕНДЕРОВ (для тестирования без API токена)
# ============================================================

def add_demo_tenders():
    """Добавляет демо-тендеры для тестирования"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    count = c.execute("SELECT COUNT(*) FROM tenders").fetchone()[0]
    if count > 0:
        conn.close()
        return

    demo_tenders = [
        {
            "external_id": "goszakup_demo_1",
            "source": "goszakup",
            "title": "Разработка информационной системы управления документами",
            "description": "Создание электронной системы документооборота для государственного органа",
            "budget": 45000000,
            "currency": "KZT",
            "deadline": "2026-06-15",
            "customer": "Министерство цифрового развития РК",
            "url": "https://goszakup.gov.kz/ru/announce/index/demo1"
        },
        {
            "external_id": "goszakup_demo_2",
            "source": "goszakup",
            "title": "Строительство административного здания в г. Астана",
            "description": "Возведение 5-этажного административного здания площадью 3500 кв.м",
            "budget": 850000000,
            "currency": "KZT",
            "deadline": "2026-05-30",
            "customer": "Акимат г. Астана",
            "url": "https://goszakup.gov.kz/ru/announce/index/demo2"
        },
        {
            "external_id": "samruk_demo_1",
            "source": "samruk",
            "title": "Поставка компьютерного оборудования и лицензионного ПО",
            "description": "Закупка 200 рабочих станций, серверного оборудования и программных лицензий",
            "budget": 120000000,
            "currency": "KZT",
            "deadline": "2026-06-01",
            "customer": "АО Самрук-Казына",
            "url": "https://zakup.sk.kz/eproc-ebs/#/open/announce/demo1"
        },
        {
            "external_id": "samruk_demo_2",
            "source": "samruk",
            "title": "Ремонт кровли производственного здания",
            "description": "Капитальный ремонт кровли площадью 2200 кв.м с заменой гидроизоляции",
            "budget": 35000000,
            "currency": "KZT",
            "deadline": "2026-05-25",
            "customer": "АО КазМунайГаз",
            "url": "https://zakup.sk.kz/eproc-ebs/#/open/announce/demo2"
        },
        {
            "external_id": "goszakup_demo_3",
            "source": "goszakup",
            "title": "Разработка мобильного приложения для граждан",
            "description": "Создание iOS и Android приложения для получения государственных услуг",
            "budget": 28000000,
            "currency": "KZT",
            "deadline": "2026-06-20",
            "customer": "Министерство труда и социальной защиты РК",
            "url": "https://goszakup.gov.kz/ru/announce/index/demo3"
        }
    ]

    for t in demo_tenders:
        c.execute("""
            INSERT OR IGNORE INTO tenders
            (external_id, source, title, description, budget, currency, deadline, customer, url, raw_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            t["external_id"], t["source"], t["title"], t["description"],
            t["budget"], t["currency"], t["deadline"], t["customer"],
            t["url"], "{}"
        ))

    conn.commit()
    conn.close()
    print("✅ Добавлены демо-тендеры")


# ============================================================
# HTTP API для фронтенда (простой сервер)
# ============================================================

from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

FRONTEND_PATH = os.path.join(BASE_DIR, "..", "frontend", "index.html")

class TenderAPIHandler(BaseHTTPRequestHandler):

    def _get_token(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        return None

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        """Возвращает пользователя или отправляет 401"""
        user = verify_token(self._get_token())
        if not user:
            self._json({"error": "Требуется авторизация"}, 401)
        return user

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        try:
            if path in ("/", "/index.html"):
                with open(FRONTEND_PATH, "rb") as f:
                    html = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(html)
                return

            elif path == "/api/me":
                user = self._auth()
                if not user:
                    return
                self._json({"id": user["id"], "username": user["username"],
                            "client_id": user["client_id"], "is_admin": user["is_admin"]})

            elif path == "/api/clients":
                user = self._auth()
                if not user:
                    return
                conn = sqlite3.connect(DB_PATH)
                conn.row_factory = sqlite3.Row
                if user["is_admin"]:
                    rows = conn.execute("SELECT * FROM clients ORDER BY name").fetchall()
                else:
                    rows = conn.execute("SELECT * FROM clients WHERE id=?",
                                        [user["client_id"]]).fetchall()
                conn.close()
                self._json([dict(r) for r in rows])

            elif path == "/api/tenders":
                user = self._auth()
                if not user:
                    return
                # Клиент видит только свои тендеры
                client_id = user["client_id"] if not user["is_admin"] else params.get("client_id", [1])[0]
                source = params.get("source", [None])[0]
                recommendation = params.get("recommendation", [None])[0]

                query = """
                    SELECT t.*, a.recommendation, a.score, a.summary, a.reasons, a.risks
                    FROM tenders t
                    LEFT JOIN analyses a ON t.id = a.tender_id AND a.client_id = ?
                    WHERE 1=1
                """
                args = [client_id or 1]
                if source:
                    query += " AND t.source = ?"
                    args.append(source)
                if recommendation:
                    query += " AND a.recommendation = ?"
                    args.append(recommendation)
                query += " ORDER BY t.fetched_at DESC LIMIT 100"

                conn = sqlite3.connect(DB_PATH)
                conn.row_factory = sqlite3.Row
                rows = conn.execute(query, args).fetchall()
                conn.close()

                result = []
                for r in rows:
                    d = dict(r)
                    d["reasons"] = json.loads(d["reasons"] or "[]")
                    d["risks"] = json.loads(d["risks"] or "[]")
                    result.append(d)
                self._json(result)

            elif path == "/api/stats":
                user = self._auth()
                if not user:
                    return
                client_id = user["client_id"] if not user["is_admin"] else params.get("client_id", [1])[0]
                conn = sqlite3.connect(DB_PATH)
                stats = {}
                for rec in ["YES", "NO", "MAYBE"]:
                    stats[rec] = conn.execute(
                        "SELECT COUNT(*) FROM analyses WHERE client_id=? AND recommendation=?",
                        [client_id, rec]
                    ).fetchone()[0]
                stats["total"] = conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0]
                conn.close()
                self._json(stats)

            else:
                self._json({"error": "not found"}, 404)

        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length)) if content_length else {}

        try:
            if parsed.path == "/api/login":
                username = body.get("username", "")
                password = body.get("password", "")
                conn = sqlite3.connect(DB_PATH)
                conn.row_factory = sqlite3.Row
                user = conn.execute(
                    "SELECT * FROM users WHERE username=? AND password_hash=?",
                    [username, hash_password(password)]
                ).fetchone()
                if not user:
                    conn.close()
                    self._json({"error": "Неверный логин или пароль"}, 401)
                    return
                token = secrets.token_hex(32)
                conn.execute("INSERT INTO sessions (token, user_id) VALUES (?, ?)",
                             [token, user["id"]])
                conn.commit()
                conn.close()
                self._json({"token": token, "username": user["username"],
                            "client_id": user["client_id"], "is_admin": user["is_admin"]})

            elif parsed.path == "/api/logout":
                token = self._get_token()
                if token:
                    conn = sqlite3.connect(DB_PATH)
                    conn.execute("DELETE FROM sessions WHERE token=?", [token])
                    conn.commit()
                    conn.close()
                self._json({"status": "ok"})

            elif parsed.path == "/api/clients":
                user = self._auth()
                if not user or not user["is_admin"]:
                    self._json({"error": "Только для администратора"}, 403)
                    return
                conn = sqlite3.connect(DB_PATH)
                conn.execute("""
                    INSERT INTO clients (name, description, industry, keywords, capabilities)
                    VALUES (?, ?, ?, ?, ?)
                """, (body["name"], body.get("description",""), body.get("industry",""),
                      body.get("keywords",""), body.get("capabilities","")))
                conn.commit()
                conn.close()
                self._json({"status": "ok"})

            elif parsed.path == "/api/users":
                user = self._auth()
                if not user or not user["is_admin"]:
                    self._json({"error": "Только для администратора"}, 403)
                    return
                create_user(body["username"], body["password"],
                            body.get("client_id"), body.get("is_admin", 0))
                self._json({"status": "ok"})

            elif parsed.path == "/api/fetch":
                user = self._auth()
                if not user or not user["is_admin"]:
                    self._json({"error": "Только для администратора"}, 403)
                    return
                gz = fetch_goszakup_tenders(limit=20)
                save_goszakup_tenders(gz)
                sk = fetch_samruk_tenders(limit=20)
                save_samruk_tenders(sk)
                self._json({"goszakup": len(gz), "samruk": len(sk)})

            elif parsed.path == "/api/analyze":
                user = self._auth()
                if not user or not user["is_admin"]:
                    self._json({"error": "Только для администратора"}, 403)
                    return
                run_analysis_for_all_clients()
                self._json({"status": "ok"})

            else:
                self._json({"error": "not found"}, 404)

        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def log_message(self, format, *args):
        pass


# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":
    print("🚀 Запуск Tender Analysis System...")
    init_db()
    add_sample_clients()
    add_demo_tenders()
    ensure_admin()
    run_analysis_for_all_clients()

    port = int(os.environ.get("PORT", 8000))
    server = HTTPServer(("0.0.0.0", port), TenderAPIHandler)
    print(f"✅ API сервер запущен на http://localhost:{port}")
    print("   Открой http://localhost:{port} в браузере")
    print("   Ctrl+C для остановки\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Сервер остановлен")
