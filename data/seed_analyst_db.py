"""
Seed script — creates and populates data/analyst.db with a SaaS subscription
analytics dataset.

Schema
------
  plans         — subscription tiers (free / starter / pro / enterprise)
  accounts      — customer companies with industry and churn date
  subscriptions — one active or cancelled subscription per account
  users         — individual seat-holders within each account
  events        — product usage events (login, export, api_call, …)
  invoices      — billing invoices per account

Run once:
  python data/seed_analyst_db.py
"""

import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

random.seed(7)

DB_PATH = Path(__file__).parent / "analyst.db"

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL UNIQUE,
    monthly_price REAL    NOT NULL,
    max_seats     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    industry   TEXT NOT NULL,
    country    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    churned_at TEXT          -- NULL = still active
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id     INTEGER NOT NULL REFERENCES accounts(id),
    plan_id        INTEGER NOT NULL REFERENCES plans(id),
    status         TEXT    NOT NULL CHECK(status IN ('active','cancelled','paused')),
    billing_cycle  TEXT    NOT NULL CHECK(billing_cycle IN ('monthly','annual')),
    mrr            REAL    NOT NULL,
    started_at     TEXT    NOT NULL,
    ended_at       TEXT          -- NULL = still running
);

CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id     INTEGER NOT NULL REFERENCES accounts(id),
    name           TEXT    NOT NULL,
    email          TEXT    NOT NULL UNIQUE,
    role           TEXT    NOT NULL CHECK(role IN ('admin','member','viewer')),
    created_at     TEXT    NOT NULL,
    last_active_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    user_id     INTEGER NOT NULL REFERENCES users(id),
    event_type  TEXT    NOT NULL,
    occurred_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    amount      REAL    NOT NULL,
    status      TEXT    NOT NULL CHECK(status IN ('paid','pending','failed')),
    issued_at   TEXT    NOT NULL,
    paid_at     TEXT          -- NULL if not yet paid
);
"""

# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------
PLANS = [
    ("Free",       0.00,   3),
    ("Starter",   49.00,  10),
    ("Pro",      149.00,  25),
    ("Enterprise",499.00, 999),
]

INDUSTRIES = [
    "Software & Technology",
    "Financial Services",
    "Healthcare",
    "E-Commerce & Retail",
    "Media & Entertainment",
    "Manufacturing",
    "Education",
    "Consulting & Professional Services",
    "Real Estate",
    "Logistics & Supply Chain",
]

COUNTRIES = [
    "United States", "United Kingdom", "Germany", "Canada",
    "Australia", "France", "Netherlands", "India", "Brazil", "Singapore",
]
COUNTRY_WEIGHTS = [35, 15, 8, 8, 6, 6, 5, 5, 5, 7]

COMPANY_SUFFIXES = [
    "Inc", "LLC", "Ltd", "Group", "Solutions", "Technologies",
    "Labs", "Systems", "Partners", "Co",
]

ADJECTIVES = [
    "Acme","Apex","Atlas","Blue","Bright","Cedar","Core","Delta","Echo",
    "First","Flash","Globe","Green","Grid","Harbor","Horizon","Ionic",
    "Lumen","Maple","Nexus","Nova","Orbit","Peak","Pixel","Prism",
    "Quanta","Ridge","Sage","Solar","Spark","Summit","Swift","Terra",
    "Titan","Vantage","Vertex","Vista","Wave","Zenith","Zeta",
]

FIRST_NAMES = [
    "Alice","Bob","Carol","David","Eve","Frank","Grace","Heidi","Ivan",
    "Judy","Kevin","Laura","Mallory","Nathan","Olivia","Paul","Quinn",
    "Rachel","Steve","Tina","Uma","Victor","Wendy","Xavier","Yara","Zach",
    "Aisha","Bruno","Chloe","Diego","Elena","Felix","Gina","Hugo","Iris",
    "Jake","Kira","Leo","Maya","Nico","Opal","Pedro","Rosa","Sam","Tara",
]

LAST_NAMES = [
    "Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis",
    "Wilson","Taylor","Anderson","Thomas","Jackson","White","Harris",
    "Martin","Thompson","Young","Allen","King","Wright","Scott","Baker",
    "Hill","Ramirez","Campbell","Mitchell","Roberts","Carter","Evans",
]

EVENT_TYPES = [
    "login", "dashboard_view", "report_view", "export_csv",
    "api_call", "settings_change", "invite_sent", "data_import",
]
EVENT_WEIGHTS = [30, 22, 18, 10, 9, 4, 4, 3]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def rdate(start: datetime, end: datetime) -> datetime:
    return start + timedelta(seconds=random.randint(0, int((end - start).total_seconds())))


def fmt(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def make_company_name(seen: set) -> str:
    for _ in range(100):
        name = f"{random.choice(ADJECTIVES)} {random.choice(COMPANY_SUFFIXES)}"
        if name not in seen:
            seen.add(name)
            return name
    return f"{random.choice(ADJECTIVES)} {random.randint(100,999)}"


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------
def seed(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()

    # ── Plans ────────────────────────────────────────────────────────────────
    cur.executemany(
        "INSERT OR IGNORE INTO plans (name, monthly_price, max_seats) VALUES (?,?,?)",
        PLANS,
    )
    conn.commit()
    plan_rows = cur.execute("SELECT id, name, monthly_price, max_seats FROM plans").fetchall()
    plan_by_id = {r[0]: {"name": r[1], "price": r[2], "seats": r[3]} for r in plan_rows}
    plan_ids   = list(plan_by_id.keys())
    # Weighted toward Starter/Pro
    plan_weights = [5, 35, 40, 20]

    # ── Accounts ─────────────────────────────────────────────────────────────
    company_names: set = set()
    account_start = datetime(2022, 1, 1)
    account_end   = datetime(2025, 6, 30)

    accounts = []
    for _ in range(200):
        name    = make_company_name(company_names)
        industry = random.choice(INDUSTRIES)
        country  = random.choices(COUNTRIES, weights=COUNTRY_WEIGHTS, k=1)[0]
        created  = rdate(account_start, account_end)
        # ~18 % churn; churned accounts must be at least 3 months old
        if random.random() < 0.18 and (datetime(2025, 9, 1) - created).days > 90:
            churned = rdate(created + timedelta(days=90), datetime(2025, 9, 1))
        else:
            churned = None
        accounts.append((name, industry, country, fmt(created), fmt(churned)))

    cur.executemany(
        "INSERT INTO accounts (name, industry, country, created_at, churned_at) VALUES (?,?,?,?,?)",
        accounts,
    )
    conn.commit()
    account_rows = cur.execute(
        "SELECT id, created_at, churned_at FROM accounts"
    ).fetchall()

    # ── Subscriptions ────────────────────────────────────────────────────────
    subscriptions = []
    for acc_id, created_at_str, churned_at_str in account_rows:
        plan_id = random.choices(plan_ids, weights=plan_weights, k=1)[0]
        plan    = plan_by_id[plan_id]
        cycle   = random.choices(["monthly", "annual"], weights=[60, 40], k=1)[0]
        # Annual billing gives a ~15 % discount, but MRR is normalised monthly
        base_mrr = plan["price"] * (0.85 if cycle == "annual" else 1.0)
        mrr = round(base_mrr, 2)

        started_at = datetime.strptime(created_at_str, "%Y-%m-%d %H:%M:%S")
        if churned_at_str:
            ended_at = datetime.strptime(churned_at_str, "%Y-%m-%d %H:%M:%S")
            status = "cancelled"
        else:
            ended_at = None
            status = random.choices(
                ["active", "paused"], weights=[92, 8], k=1
            )[0]

        subscriptions.append((
            acc_id, plan_id, status, cycle, mrr,
            fmt(started_at), fmt(ended_at),
        ))

    cur.executemany(
        "INSERT INTO subscriptions "
        "(account_id, plan_id, status, billing_cycle, mrr, started_at, ended_at) "
        "VALUES (?,?,?,?,?,?,?)",
        subscriptions,
    )
    conn.commit()

    # ── Users ─────────────────────────────────────────────────────────────────
    emails_seen: set = set()
    users = []
    user_account_map: list[tuple[int, int]] = []  # (user_rownum, account_id)

    for acc_id, created_at_str, churned_at_str in account_rows:
        plan_id  = next(
            s[1] for s in subscriptions
            if s[0] == acc_id
        )
        max_seats = plan_by_id[plan_id]["seats"]
        num_users = random.randint(1, min(max_seats, 8))

        acc_created = datetime.strptime(created_at_str, "%Y-%m-%d %H:%M:%S")
        acc_end     = (
            datetime.strptime(churned_at_str, "%Y-%m-%d %H:%M:%S")
            if churned_at_str else datetime(2025, 10, 1)
        )

        for i in range(num_users):
            first = random.choice(FIRST_NAMES)
            last  = random.choice(LAST_NAMES)
            base  = f"{first.lower()}.{last.lower()}"
            email = f"{base}@example.com"
            suffix = 1
            while email in emails_seen:
                email = f"{base}{suffix}@example.com"
                suffix += 1
            emails_seen.add(email)

            role       = "admin" if i == 0 else random.choices(
                ["member", "viewer"], weights=[70, 30], k=1
            )[0]
            user_created = rdate(acc_created, min(acc_end, acc_created + timedelta(days=180)))
            last_active  = rdate(user_created, acc_end) if random.random() < 0.85 else None

            users.append((acc_id, f"{first} {last}", email, role, fmt(user_created), fmt(last_active)))
            user_account_map.append(acc_id)

    cur.executemany(
        "INSERT INTO users (account_id, name, email, role, created_at, last_active_at) "
        "VALUES (?,?,?,?,?,?)",
        users,
    )
    conn.commit()

    user_rows = cur.execute("SELECT id, account_id, created_at FROM users").fetchall()
    users_by_account: dict[int, list] = {}
    for uid, aid, ucreated in user_rows:
        users_by_account.setdefault(aid, []).append((uid, ucreated))

    # ── Events ────────────────────────────────────────────────────────────────
    events = []
    event_start = datetime(2024, 1, 1)
    event_end   = datetime(2025, 9, 30)

    for acc_id, _, churned_at_str in account_rows:
        acc_event_end = (
            datetime.strptime(churned_at_str, "%Y-%m-%d %H:%M:%S")
            if churned_at_str else event_end
        )
        acc_event_end = min(acc_event_end, event_end)
        if acc_event_end <= event_start:
            continue

        acc_users = users_by_account.get(acc_id, [])
        if not acc_users:
            continue

        num_events = random.randint(5, 40)
        for _ in range(num_events):
            uid, _ucreated = random.choice(acc_users)
            etype = random.choices(EVENT_TYPES, weights=EVENT_WEIGHTS, k=1)[0]
            occurred = rdate(event_start, acc_event_end)
            events.append((acc_id, uid, etype, fmt(occurred)))

    cur.executemany(
        "INSERT INTO events (account_id, user_id, event_type, occurred_at) VALUES (?,?,?,?)",
        events,
    )
    conn.commit()

    # ── Invoices ──────────────────────────────────────────────────────────────
    invoices = []
    for acc_id, created_at_str, churned_at_str in account_rows:
        sub = next(s for s in subscriptions if s[0] == acc_id)
        mrr, started_str, ended_str = sub[4], sub[5], sub[6]
        if mrr == 0:
            continue  # Free plan — no invoices

        started  = datetime.strptime(started_str, "%Y-%m-%d %H:%M:%S")
        inv_end  = (
            datetime.strptime(ended_str, "%Y-%m-%d %H:%M:%S")
            if ended_str else datetime(2025, 10, 1)
        )

        # One invoice per month from subscription start to end
        cursor_date = started.replace(day=1) + timedelta(days=32)
        cursor_date = cursor_date.replace(day=1)
        while cursor_date < inv_end:
            rand = random.random()
            status  = "paid" if rand < 0.88 else ("failed" if rand < 0.93 else "pending")
            paid_at = (
                fmt(cursor_date + timedelta(days=random.randint(0, 5)))
                if status == "paid" else None
            )
            invoices.append((acc_id, round(mrr, 2), status, fmt(cursor_date), paid_at))
            cursor_date = (cursor_date + timedelta(days=32)).replace(day=1)

    cur.executemany(
        "INSERT INTO invoices (account_id, amount, status, issued_at, paid_at) VALUES (?,?,?,?,?)",
        invoices,
    )
    conn.commit()

    # ── Summary ───────────────────────────────────────────────────────────────
    for table in ("plans", "accounts", "subscriptions", "users", "events", "invoices"):
        count = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table:<15} {count:>6} rows")


def main() -> None:
    if DB_PATH.exists():
        print(f"Database already exists at {DB_PATH}. Delete it and re-run to reseed.")
        return

    print(f"Creating {DB_PATH} ...")
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)
        seed(conn)
    print("Done.")


if __name__ == "__main__":
    main()
