"""
Seed script — creates and populates data/sample.db with a generic e-commerce dataset.

Schema
------
  categories   — product categories
  products     — SKUs with price, cost, stock
  customers    — buyers with city / country
  orders       — order header (status, dates)
  order_items  — line items linking orders ↔ products

Run once:
  python data/seed_db.py
"""

import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

random.seed(42)

DB_PATH = Path(__file__).parent / "sample.db"

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT    NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS products (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    category_id INTEGER REFERENCES categories(id),
    price      REAL    NOT NULL,
    cost       REAL    NOT NULL,
    stock_qty  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS customers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    email      TEXT NOT NULL UNIQUE,
    city       TEXT,
    country    TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    status      TEXT    NOT NULL CHECK(status IN ('pending','shipped','delivered','cancelled')),
    created_at  TEXT    NOT NULL,
    shipped_at  TEXT
);

CREATE TABLE IF NOT EXISTS order_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL REFERENCES orders(id),
    product_id  INTEGER NOT NULL REFERENCES products(id),
    quantity    INTEGER NOT NULL,
    unit_price  REAL    NOT NULL
);
"""

# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------
CATEGORIES = [
    "Electronics",
    "Clothing",
    "Home & Garden",
    "Sports & Outdoors",
    "Books",
    "Beauty & Personal Care",
    "Toys & Games",
    "Food & Grocery",
]

PRODUCTS_BY_CATEGORY = {
    "Electronics": [
        ("Wireless Earbuds Pro",          79.99,  32.00),
        ("USB-C Hub 7-in-1",              34.99,  12.00),
        ("Mechanical Keyboard TKL",       89.99,  38.00),
        ("27\" 4K Monitor",              349.99, 180.00),
        ("Webcam 1080p",                  49.99,  18.00),
        ("Smart Plug 4-pack",             24.99,   8.00),
        ("Portable Charger 20000mAh",     39.99,  14.00),
        ("Bluetooth Speaker Waterproof",  59.99,  22.00),
    ],
    "Clothing": [
        ("Classic Crew-Neck T-Shirt",     19.99,   5.00),
        ("Slim-Fit Chino Trousers",       44.99,  14.00),
        ("Lightweight Running Jacket",    69.99,  25.00),
        ("Merino Wool Sweater",           79.99,  30.00),
        ("Everyday Sneakers",             64.99,  22.00),
        ("Cotton Hoodie",                 49.99,  16.00),
    ],
    "Home & Garden": [
        ("Bamboo Cutting Board Set",      27.99,   9.00),
        ("Stainless Steel Knife Set 6pc", 54.99,  20.00),
        ("Ceramic Plant Pot 3-pack",      22.99,   7.00),
        ("LED Desk Lamp Dimmable",        34.99,  12.00),
        ("Cotton Throw Blanket",          39.99,  14.00),
        ("Herb Garden Starter Kit",       19.99,   6.00),
    ],
    "Sports & Outdoors": [
        ("Yoga Mat Non-Slip",             29.99,   9.00),
        ("Resistance Bands Set 5pc",      18.99,   5.00),
        ("Adjustable Dumbbell 20kg",     129.99,  55.00),
        ("Trail Running Shoes",           89.99,  34.00),
        ("Hydration Backpack 15L",        54.99,  19.00),
        ("Jump Rope Speed Cable",         14.99,   4.00),
    ],
    "Books": [
        ("Python for Data Analysis",      39.99,  12.00),
        ("Designing Data-Intensive Apps", 44.99,  14.00),
        ("Atomic Habits",                 16.99,   5.00),
        ("The Pragmatic Programmer",      49.99,  15.00),
        ("Clean Code",                    38.99,  12.00),
    ],
    "Beauty & Personal Care": [
        ("Vitamin C Serum 30ml",          24.99,   7.00),
        ("Hyaluronic Acid Moisturiser",   19.99,   6.00),
        ("Electric Toothbrush",           39.99,  13.00),
        ("Shea Butter Body Lotion",       14.99,   4.00),
        ("Natural Deodorant",              9.99,   2.50),
    ],
    "Toys & Games": [
        ("Strategy Board Game",           34.99,  11.00),
        ("STEM Building Blocks 200pc",    44.99,  15.00),
        ("Remote Control Car",            29.99,   9.00),
        ("Watercolour Paint Set",         19.99,   6.00),
        ("Puzzle 1000 Pieces",            14.99,   4.00),
    ],
    "Food & Grocery": [
        ("Organic Green Tea 100 bags",    12.99,   4.00),
        ("Dark Chocolate 85% 6-pack",     18.99,   7.00),
        ("Extra Virgin Olive Oil 1L",     14.99,   5.00),
        ("Mixed Nuts 1kg",               19.99,   7.00),
        ("Artisan Coffee Beans 500g",     16.99,   6.00),
    ],
}

FIRST_NAMES = [
    "James","Mary","John","Patricia","Robert","Jennifer","Michael","Linda",
    "William","Barbara","David","Susan","Richard","Jessica","Joseph","Sarah",
    "Thomas","Karen","Charles","Lisa","Emma","Noah","Olivia","Liam","Ava",
    "Sophie","Ethan","Chloe","Lucas","Isabella","Mia","Amelia","Harper","Ella",
    "Aria","Scarlett","Grace","Zoe","Hannah","Nora",
]

LAST_NAMES = [
    "Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis",
    "Wilson","Taylor","Anderson","Thomas","Jackson","White","Harris","Martin",
    "Thompson","Young","Allen","King","Wright","Scott","Green","Baker","Adams",
    "Nelson","Hill","Ramirez","Campbell","Mitchell","Roberts","Carter","Evans",
]

CITIES_BY_COUNTRY = {
    "United States": ["New York","Los Angeles","Chicago","Houston","Phoenix","Philadelphia","San Antonio","San Diego"],
    "United Kingdom": ["London","Birmingham","Manchester","Leeds","Glasgow","Liverpool","Bristol","Edinburgh"],
    "Canada":         ["Toronto","Vancouver","Montreal","Calgary","Ottawa","Edmonton","Quebec City"],
    "Australia":      ["Sydney","Melbourne","Brisbane","Perth","Adelaide","Gold Coast","Canberra"],
    "Germany":        ["Berlin","Hamburg","Munich","Cologne","Frankfurt","Stuttgart","Düsseldorf"],
    "France":         ["Paris","Lyon","Marseille","Toulouse","Nice","Nantes","Bordeaux"],
}

STATUS_WEIGHTS = [
    ("delivered", 60),
    ("shipped",   20),
    ("pending",   12),
    ("cancelled",  8),
]
STATUSES, WEIGHTS = zip(*STATUS_WEIGHTS)


def random_date(start: datetime, end: datetime) -> datetime:
    delta = end - start
    return start + timedelta(seconds=random.randint(0, int(delta.total_seconds())))


def seed(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()

    # ── Categories ──────────────────────────────────────────────────────────
    cur.executemany("INSERT OR IGNORE INTO categories (name) VALUES (?)",
                    [(c,) for c in CATEGORIES])
    conn.commit()

    cat_ids = {row[0]: row[1] for row in cur.execute("SELECT name, id FROM categories")}

    # ── Products ─────────────────────────────────────────────────────────────
    products_to_insert = []
    for cat_name, items in PRODUCTS_BY_CATEGORY.items():
        for name, price, cost in items:
            stock = random.randint(0, 500)
            products_to_insert.append((name, cat_ids[cat_name], price, cost, stock))

    cur.executemany(
        "INSERT OR IGNORE INTO products (name, category_id, price, cost, stock_qty) VALUES (?,?,?,?,?)",
        products_to_insert,
    )
    conn.commit()

    product_ids = [row[0] for row in cur.execute("SELECT id FROM products")]

    # ── Customers ────────────────────────────────────────────────────────────
    countries = list(CITIES_BY_COUNTRY.keys())
    emails_seen: set[str] = set()
    customers_to_insert = []

    for i in range(300):
        first = random.choice(FIRST_NAMES)
        last  = random.choice(LAST_NAMES)
        name  = f"{first} {last}"

        base_email = f"{first.lower()}.{last.lower()}"
        email = f"{base_email}@example.com"
        # Ensure uniqueness
        suffix = 1
        while email in emails_seen:
            email = f"{base_email}{suffix}@example.com"
            suffix += 1
        emails_seen.add(email)

        country = random.choice(countries)
        city    = random.choice(CITIES_BY_COUNTRY[country])
        created = random_date(
            datetime(2023, 1, 1), datetime(2025, 12, 31)
        ).strftime("%Y-%m-%d %H:%M:%S")
        customers_to_insert.append((name, email, city, country, created))

    cur.executemany(
        "INSERT OR IGNORE INTO customers (name, email, city, country, created_at) VALUES (?,?,?,?,?)",
        customers_to_insert,
    )
    conn.commit()

    customer_ids = [row[0] for row in cur.execute("SELECT id FROM customers")]

    # ── Orders + Order Items ─────────────────────────────────────────────────
    orders_to_insert    = []
    order_items_buffer  = []
    order_date_start    = datetime(2023, 1, 1)
    order_date_end      = datetime(2025, 12, 31)

    for _ in range(1200):
        cust_id    = random.choice(customer_ids)
        status     = random.choices(STATUSES, weights=WEIGHTS, k=1)[0]
        created_dt = random_date(order_date_start, order_date_end)
        created    = created_dt.strftime("%Y-%m-%d %H:%M:%S")

        shipped = None
        if status in ("shipped", "delivered"):
            shipped_dt = created_dt + timedelta(days=random.randint(1, 5))
            shipped    = shipped_dt.strftime("%Y-%m-%d %H:%M:%S")

        orders_to_insert.append((cust_id, status, created, shipped))

    cur.executemany(
        "INSERT INTO orders (customer_id, status, created_at, shipped_at) VALUES (?,?,?,?)",
        orders_to_insert,
    )
    conn.commit()

    order_ids = [row[0] for row in cur.execute("SELECT id FROM orders")]

    for order_id in order_ids:
        num_items = random.randint(1, 5)
        chosen_products = random.sample(product_ids, min(num_items, len(product_ids)))
        for prod_id in chosen_products:
            price = cur.execute("SELECT price FROM products WHERE id=?", (prod_id,)).fetchone()[0]
            qty   = random.randint(1, 4)
            # Small random price variation to simulate discounts/promotions
            unit_price = round(price * random.uniform(0.85, 1.0), 2)
            order_items_buffer.append((order_id, prod_id, qty, unit_price))

    cur.executemany(
        "INSERT INTO order_items (order_id, product_id, quantity, unit_price) VALUES (?,?,?,?)",
        order_items_buffer,
    )
    conn.commit()

    # ── Summary ──────────────────────────────────────────────────────────────
    for table in ("categories", "products", "customers", "orders", "order_items"):
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
