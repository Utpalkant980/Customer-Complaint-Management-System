import json
import re
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "complaints.db"
STATUSES = {"Open", "In progress", "Resolved"}
CATEGORIES = {
    "Billing",
    "Delivery",
    "Product quality",
    "Technical support",
    "Account",
    "Other",
}
NEXT_STEPS = {
    "Billing": "Review the invoice and verify the payment before issuing any adjustment.",
    "Delivery": "Check the latest carrier scan and share an updated delivery estimate.",
    "Product quality": "Request photos of the item and offer the appropriate replacement or return.",
    "Technical support": "Reproduce the issue, then share a workaround or escalate to engineering.",
    "Account": "Verify the customer's identity and guide them through the account change.",
    "Other": "Review the customer's message and route it to the best-equipped team.",
}
DEFAULT_SETTINGS = {
    "workspace_name": "Studio North",
    "team_name": "Customer support",
    "manager_name": "Jordan Davis",
    "manager_role": "Support manager",
    "ai_assist_enabled": True,
}
DEFAULT_ACCOUNT_EMAIL = "jordan.davis@example.com"

SAMPLE_COMPLAINTS = [
    ("Maya Chen", "maya.chen@example.com", "Charged twice for my subscription",
     "I was charged twice for this month's subscription. Please refund the duplicate payment.", "Billing", "High", "Negative", "Open"),
    ("Oliver Grant", "oliver.g@example.com", "Package hasn't arrived",
     "My order was supposed to arrive three days ago. The tracking page hasn't updated.", "Delivery", "High", "Negative", "In progress"),
    ("Amara Okafor", "amara.o@example.com", "App keeps closing on checkout",
     "The app crashes every time I try to finish checkout on my phone.", "Technical support", "High", "Negative", "Open"),
    ("Ethan Brooks", "ethan.b@example.com", "Item arrived with a cracked screen",
     "My new monitor arrived damaged and has a crack across the screen.", "Product quality", "High", "Negative", "In progress"),
    ("Sofia Laurent", "sofia.l@example.com", "Need to update my account email",
     "I can't find where to change the email address associated with my account.", "Account", "Medium", "Neutral", "Resolved"),
    ("Noah Williams", "noah.w@example.com", "Question about a recent invoice",
     "Could someone explain the service fee on my latest invoice? Thanks!", "Billing", "Low", "Positive", "Resolved"),
]


def connect():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE IF NOT EXISTS complaints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer TEXT NOT NULL,
            email TEXT NOT NULL,
            subject TEXT NOT NULL,
            message TEXT NOT NULL,
            category TEXT NOT NULL,
            priority TEXT NOT NULL,
            sentiment TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS app_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            workspace_name TEXT NOT NULL,
            team_name TEXT NOT NULL,
            manager_name TEXT NOT NULL,
            manager_role TEXT NOT NULL,
            ai_assist_enabled INTEGER NOT NULL
        )"""
    )
    connection.execute(
        """INSERT OR IGNORE INTO app_settings
        (id, workspace_name, team_name, manager_name, manager_role, ai_assist_enabled)
        VALUES (1, ?, ?, ?, ?, ?)""",
        (
            DEFAULT_SETTINGS["workspace_name"],
            DEFAULT_SETTINGS["team_name"],
            DEFAULT_SETTINGS["manager_name"],
            DEFAULT_SETTINGS["manager_role"],
            int(DEFAULT_SETTINGS["ai_assist_enabled"]),
        ),
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL COLLATE NOCASE UNIQUE,
            role TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )"""
    )
    active_account = connection.execute(
        "SELECT id FROM accounts WHERE is_active = 1"
    ).fetchone()
    if active_account is None:
        settings = connection.execute(
            "SELECT manager_name, manager_role FROM app_settings WHERE id = 1"
        ).fetchone()
        connection.execute(
            """INSERT OR IGNORE INTO accounts
            (name, email, role, is_active, created_at) VALUES (?, ?, ?, 1, ?)""",
            (
                settings["manager_name"],
                DEFAULT_ACCOUNT_EMAIL,
                settings["manager_role"],
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    if connection.execute("SELECT COUNT(*) FROM complaints").fetchone()[0] == 0:
        now = datetime.now(timezone.utc).isoformat()
        connection.executemany(
            """INSERT INTO complaints
            (customer, email, subject, message, category, priority, sentiment, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(*complaint, now) for complaint in SAMPLE_COMPLAINTS],
        )
        connection.commit()
    return connection


def analyze_complaint(subject, message):
    text = f"{subject} {message}".lower()
    category_terms = {
        "Billing": ("bill", "billing", "invoice", "charge", "payment", "refund", "subscription", "price"),
        "Delivery": ("delivery", "deliver", "shipping", "ship", "package", "parcel", "tracking", "order"),
        "Product quality": ("damaged", "broken", "crack", "defect", "quality", "faulty", "arrived"),
        "Technical support": ("app", "crash", "error", "bug", "website", "technical", "login", "not working"),
        "Account": ("account", "password", "profile", "email", "sign in", "access"),
    }
    matches = {
        category: sum(1 for term in terms if term in text)
        for category, terms in category_terms.items()
    }
    category = max(matches, key=matches.get) if max(matches.values(), default=0) else "Other"

    urgent_terms = ("urgent", "immediately", "fraud", "unsafe", "safety", "multiple times", "charged twice")
    negative_terms = ("angry", "terrible", "unacceptable", "disappointed", "frustrated", "broken", "damaged", "crash", "late")
    priority = "High" if any(term in text for term in urgent_terms) else (
        "Medium" if any(term in text for term in negative_terms) else "Low"
    )
    sentiment = "Negative" if any(term in text for term in negative_terms + urgent_terms) else "Neutral"
    if any(term in text for term in ("thank", "appreciate", "great", "please")) and sentiment == "Neutral":
        sentiment = "Positive"

    return {
        "category": category,
        "priority": priority,
        "sentiment": sentiment,
        "recommendation": NEXT_STEPS[category],
    }


def enrich_complaint(complaint):
    complaint["recommendation"] = analyze_complaint(
        complaint["subject"], complaint["message"]
    )["recommendation"]
    return complaint


def build_dashboard(search="", status="All"):
    with connect() as db:
        all_complaints = [dict(row) for row in db.execute(
            "SELECT * FROM complaints ORDER BY created_at DESC, id DESC"
        )]

    open_count = sum(item["status"] != "Resolved" for item in all_complaints)
    resolved_count = len(all_complaints) - open_count
    category_counts = {}
    for item in all_complaints:
        category_counts[item["category"]] = category_counts.get(item["category"], 0) + 1
    categories = [
        {"name": category, "count": count}
        for category, count in sorted(
            category_counts.items(), key=lambda entry: (-entry[1], entry[0])
        )
    ]
    top_category = categories[0] if categories else None
    high_priority_open = sum(
        item["priority"] == "High" and item["status"] != "Resolved"
        for item in all_complaints
    )
    query = search.casefold()
    filtered = [
        item for item in all_complaints
        if (status == "All" or item["status"] == status)
        and (
            not query
            or query in item["customer"].casefold()
            or query in item["email"].casefold()
            or query in item["subject"].casefold()
            or query in item["category"].casefold()
        )
    ]

    return {
        "complaints": [enrich_complaint(item) for item in filtered],
        "stats": {
            "total": len(all_complaints),
            "open": open_count,
            "resolved": resolved_count,
            "high_priority": sum(
                item["priority"] == "High" and item["status"] != "Resolved"
                for item in all_complaints
            ),
            "resolution_rate": round(resolved_count / len(all_complaints) * 100)
            if all_complaints else 0,
        },
        "categories": categories,
        "insight": {
            "top_category": top_category,
            "high_priority_open": high_priority_open,
        },
    }


def get_settings():
    with connect() as db:
        settings = dict(db.execute(
            """SELECT workspace_name, team_name, manager_name, manager_role,
            ai_assist_enabled FROM app_settings WHERE id = 1"""
        ).fetchone())
    settings["ai_assist_enabled"] = bool(settings["ai_assist_enabled"])
    return settings


def get_accounts():
    with connect() as db:
        accounts = [dict(row) for row in db.execute(
            "SELECT id, name, email, role, is_active FROM accounts ORDER BY is_active DESC, name COLLATE NOCASE"
        )]
    return {
        "accounts": accounts,
        "active_account": next((account for account in accounts if account["is_active"]), None),
    }


class ComplaintHandler(BaseHTTPRequestHandler):
    def is_allowed_local_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return False
        try:
            parsed_origin = urlparse(origin)
            return (
                parsed_origin.scheme == "http"
                and parsed_origin.hostname in {"localhost", "127.0.0.1"}
                and parsed_origin.port is not None
            )
        except ValueError:
            return False

    def send_cors_headers(self):
        origin = self.headers.get("Origin")
        if self.is_allowed_local_origin():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        if not self.is_allowed_local_origin():
            return self.send_json(403, {"error": "Origin is not allowed."})
        self.send_response(204)
        self.send_cors_headers()
        self.end_headers()

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > 16_384:
            raise ValueError("Request body must be between 1 byte and 16 KB.")
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object.")
        return payload

    def do_GET(self):
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        query = parse_qs(parsed_url.query, max_num_fields=10)
        if path == "/api/health":
            return self.send_json(200, {
                "status": "ok",
                "service": "complaint-management",
                "database": "sqlite",
            })
        search = query.get("search", [""])[0].strip()
        status = query.get("status", ["All"])[0]
        if len(search) > 200:
            return self.send_json(400, {"error": "Search must be 200 characters or fewer."})
        if status not in STATUSES | {"All"}:
            return self.send_json(400, {"error": "Choose a valid complaint status."})
        if path == "/api/dashboard":
            return self.send_json(200, build_dashboard(search, status))
        if path == "/api/settings":
            return self.send_json(200, {"settings": get_settings()})
        if path == "/api/accounts":
            return self.send_json(200, get_accounts())
        if path == "/api/complaints":
            dashboard = build_dashboard(search, status)
            complaints = dashboard["complaints"]
            return self.send_json(200, {"complaints": complaints})
        if path == "/":
            content = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_cors_headers()
            self.end_headers()
            return self.wfile.write(content)
        if path == "/styles.css":
            content = (ROOT / "styles.css").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/css; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self.send_cors_headers()
            self.end_headers()
            return self.wfile.write(content)
        return self.send_json(404, {"error": "Not found."})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/accounts":
            try:
                payload = self.read_json()
                name = str(payload.get("name", "")).strip()
                email = str(payload.get("email", "")).strip().lower()
                role = str(payload.get("role", "")).strip()
                if not name or not email or not role:
                    return self.send_json(400, {"error": "Name, email, and role are required."})
                if len(name) > 100 or len(email) > 254 or len(role) > 100:
                    return self.send_json(400, {"error": "Account fields exceed the allowed length."})
                if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                    return self.send_json(400, {"error": "Enter a valid email address."})
                with connect() as db:
                    db.execute("UPDATE accounts SET is_active = 0 WHERE is_active = 1")
                    cursor = db.execute(
                        """INSERT INTO accounts (name, email, role, is_active, created_at)
                        VALUES (?, ?, ?, 1, ?)""",
                        (name, email, role, datetime.now(timezone.utc).isoformat()),
                    )
                    account_id = cursor.lastrowid
                    db.execute(
                        "UPDATE app_settings SET manager_name = ?, manager_role = ? WHERE id = 1",
                        (name, role),
                    )
                account_data = get_accounts()
                account_data["active_account"] = next(
                    account for account in account_data["accounts"]
                    if account["id"] == account_id
                )
                return self.send_json(201, account_data)
            except sqlite3.IntegrityError:
                return self.send_json(409, {"error": "An account with that email already exists."})
            except (ValueError, json.JSONDecodeError) as error:
                return self.send_json(400, {"error": str(error) or "Invalid JSON request."})
        if path != "/api/complaints":
            return self.send_json(404, {"error": "Not found."})
        try:
            payload = self.read_json()
            customer = str(payload.get("customer", "")).strip()
            email = str(payload.get("email", "")).strip()
            subject = str(payload.get("subject", "")).strip()
            message = str(payload.get("message", "")).strip()
            if not all((customer, email, subject, message)):
                return self.send_json(400, {"error": "Name, email, subject, and message are required."})
            if len(customer) > 100 or len(email) > 254 or len(subject) > 160 or len(message) > 4000:
                return self.send_json(400, {"error": "One or more fields exceed the allowed length."})
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                return self.send_json(400, {"error": "Enter a valid email address."})
            if get_settings()["ai_assist_enabled"]:
                analysis = analyze_complaint(subject, message)
            else:
                analysis = {
                    "category": "Other",
                    "priority": "Low",
                    "sentiment": "Neutral",
                }
            created_at = datetime.now(timezone.utc).isoformat()
            with connect() as db:
                cursor = db.execute(
                    """INSERT INTO complaints
                    (customer, email, subject, message, category, priority, sentiment, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'Open', ?)""",
                    (customer, email, subject, message, analysis["category"], analysis["priority"],
                     analysis["sentiment"], created_at),
                )
                complaint = enrich_complaint(dict(db.execute(
                    "SELECT * FROM complaints WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()))
            return self.send_json(201, {"complaint": complaint, "analysis": analysis})
        except (ValueError, json.JSONDecodeError) as error:
            return self.send_json(400, {"error": str(error) or "Invalid JSON request."})

    def do_PATCH(self):
        account_match = re.fullmatch(r"/api/accounts/(\d+)/active", urlparse(self.path).path)
        if account_match:
            account_id = int(account_match.group(1))
            with connect() as db:
                account = db.execute(
                    "SELECT id, name, email, role FROM accounts WHERE id = ?",
                    (account_id,),
                ).fetchone()
                if account is None:
                    return self.send_json(404, {"error": "Account not found."})
                db.execute("UPDATE accounts SET is_active = 0")
                db.execute("UPDATE accounts SET is_active = 1 WHERE id = ?", (account_id,))
                db.execute(
                    "UPDATE app_settings SET manager_name = ?, manager_role = ? WHERE id = 1",
                    (account["name"], account["role"]),
                )
            return self.send_json(200, get_accounts())
        if urlparse(self.path).path == "/api/settings":
            try:
                payload = self.read_json()
                updates = {}
                for field, max_length in (
                    ("workspace_name", 100),
                    ("team_name", 100),
                    ("manager_name", 100),
                    ("manager_role", 100),
                ):
                    if field in payload:
                        value = str(payload[field]).strip()
                        if not value or len(value) > max_length:
                            return self.send_json(400, {
                                "error": f"{field.replace('_', ' ').capitalize()} must be 1–{max_length} characters."
                            })
                        updates[field] = value
                if "ai_assist_enabled" in payload:
                    if not isinstance(payload["ai_assist_enabled"], bool):
                        return self.send_json(400, {"error": "AI assist setting must be true or false."})
                    updates["ai_assist_enabled"] = int(payload["ai_assist_enabled"])
                if not updates:
                    return self.send_json(400, {"error": "Provide at least one setting to update."})
                assignments = ", ".join(f"{field} = ?" for field in updates)
                with connect() as db:
                    db.execute(
                        f"UPDATE app_settings SET {assignments} WHERE id = 1",
                        tuple(updates.values()),
                    )
                    account_updates = {}
                    if "manager_name" in updates:
                        account_updates["name"] = updates["manager_name"]
                    if "manager_role" in updates:
                        account_updates["role"] = updates["manager_role"]
                    if account_updates:
                        account_assignments = ", ".join(
                            f"{field} = ?" for field in account_updates
                        )
                        db.execute(
                            f"UPDATE accounts SET {account_assignments} WHERE is_active = 1",
                            tuple(account_updates.values()),
                        )
                return self.send_json(200, {"settings": get_settings()})
            except (ValueError, json.JSONDecodeError) as error:
                return self.send_json(400, {"error": str(error) or "Invalid JSON request."})
        match = re.fullmatch(r"/api/complaints/(\d+)", urlparse(self.path).path)
        if not match:
            return self.send_json(404, {"error": "Not found."})
        try:
            payload = self.read_json()
            status = payload.get("status")
            if status not in STATUSES:
                return self.send_json(400, {"error": "Choose a valid complaint status."})
            with connect() as db:
                cursor = db.execute(
                    "UPDATE complaints SET status = ? WHERE id = ?", (status, int(match.group(1)))
                )
                if cursor.rowcount == 0:
                    return self.send_json(404, {"error": "Complaint not found."})
                complaint = enrich_complaint(dict(db.execute(
                    "SELECT * FROM complaints WHERE id = ?", (int(match.group(1)),)
                ).fetchone()))
            return self.send_json(200, {"complaint": complaint})
        except (ValueError, json.JSONDecodeError) as error:
            return self.send_json(400, {"error": str(error) or "Invalid JSON request."})

    def log_message(self, format_string, *args):
        print(f"{self.address_string()} - {format_string % args}")


if __name__ == "__main__":
    with connect():
        pass
    server = ThreadingHTTPServer(("127.0.0.1", 8000), ComplaintHandler)
    print("Complaint management app running at http://127.0.0.1:8000")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()
