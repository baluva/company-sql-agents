"""Create the demo CompanyDB (a fictional outdoor-gear retailer) plus the agent logins.

Run once after `docker compose up -d`:  uv run scripts/seed.py
Not needed against the real company database: there, ask the DBA for the two
logins described in README.md.
"""

import datetime as dt
import os
import random
from pathlib import Path

import pymssql
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
random.seed(42)

HOST, PORT, DB = os.getenv("DB_HOST"), int(os.getenv("DB_PORT")), os.getenv("DB_NAME")
SA_PASSWORD = os.environ["MSSQL_SA_PASSWORD"]

SCHEMA = """
CREATE SCHEMA sales;
GO
CREATE SCHEMA inventory;
GO
CREATE SCHEMA hr;
GO
CREATE SCHEMA audit;
GO
CREATE TABLE hr.employees (
    employee_id INT IDENTITY PRIMARY KEY,
    full_name NVARCHAR(100) NOT NULL,
    role NVARCHAR(50) NOT NULL,
    region NVARCHAR(50) NOT NULL,
    hired_on DATE NOT NULL
);
CREATE TABLE sales.customers (
    customer_id INT IDENTITY PRIMARY KEY,
    company_name NVARCHAR(120) NOT NULL,
    segment NVARCHAR(30) NOT NULL,          -- Retail, Wholesale, Online
    city NVARCHAR(60) NOT NULL,
    country NVARCHAR(60) NOT NULL,
    account_manager_id INT REFERENCES hr.employees(employee_id),
    created_at DATE NOT NULL,
    is_active BIT NOT NULL DEFAULT 1
);
CREATE TABLE inventory.products (
    product_id INT IDENTITY PRIMARY KEY,
    sku NVARCHAR(20) NOT NULL UNIQUE,
    name NVARCHAR(120) NOT NULL,
    category NVARCHAR(40) NOT NULL,
    unit_price DECIMAL(10,2) NOT NULL,
    unit_cost DECIMAL(10,2) NOT NULL,
    stock_qty INT NOT NULL,
    reorder_level INT NOT NULL
);
CREATE TABLE sales.orders (
    order_id INT IDENTITY PRIMARY KEY,
    customer_id INT NOT NULL REFERENCES sales.customers(customer_id),
    order_date DATE NOT NULL,
    status NVARCHAR(20) NOT NULL,           -- Delivered, Shipped, Pending, Cancelled
    shipping_country NVARCHAR(60) NOT NULL
);
CREATE TABLE sales.order_items (
    order_item_id INT IDENTITY PRIMARY KEY,
    order_id INT NOT NULL REFERENCES sales.orders(order_id),
    product_id INT NOT NULL REFERENCES inventory.products(product_id),
    quantity INT NOT NULL,
    unit_price DECIMAL(10,2) NOT NULL,
    discount DECIMAL(4,2) NOT NULL DEFAULT 0
);
CREATE TABLE audit.agent_write_log (
    log_id INT IDENTITY PRIMARY KEY,
    executed_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    table_name NVARCHAR(128) NOT NULL,
    sql_text NVARCHAR(MAX) NOT NULL,
    reason NVARCHAR(400) NOT NULL,
    rows_affected INT NOT NULL,
    approved_by NVARCHAR(100) NOT NULL
);
GO
EXEC sp_addextendedproperty 'MS_Description', 'Client accounts and their segment', 'SCHEMA', 'sales', 'TABLE', 'customers';
EXEC sp_addextendedproperty 'MS_Description', 'Order headers, one row per order', 'SCHEMA', 'sales', 'TABLE', 'orders';
EXEC sp_addextendedproperty 'MS_Description', 'Order lines; revenue = quantity * unit_price * (1 - discount)', 'SCHEMA', 'sales', 'TABLE', 'order_items';
EXEC sp_addextendedproperty 'MS_Description', 'Product catalogue with stock levels', 'SCHEMA', 'inventory', 'TABLE', 'products';
EXEC sp_addextendedproperty 'MS_Description', 'Staff, including account managers', 'SCHEMA', 'hr', 'TABLE', 'employees';
EXEC sp_addextendedproperty 'MS_Description', 'Every write made by an AI agent, with who approved it', 'SCHEMA', 'audit', 'TABLE', 'agent_write_log';
"""

PRODUCTS = [
    ("Tents", ["Ridge 2P Tent", "Summit 4P Tent", "Ultralight Bivy", "Basecamp 6P Tent"], (180, 650)),
    ("Backpacks", ["Trail 30L Pack", "Expedition 65L Pack", "City Day Pack", "Hydration Vest"], (45, 260)),
    ("Footwear", ["Granite Hiking Boot", "Trail Runner X", "Approach Shoe", "Camp Sandal"], (55, 220)),
    ("Clothing", ["Storm Shell Jacket", "Merino Base Layer", "Down Parka", "Softshell Pants", "Fleece Hoodie"], (35, 340)),
    ("Climbing", ["Dynamic Rope 60m", "Harness Pro", "Carabiner Set", "Chalk Bag"], (15, 230)),
    ("Camp Kitchen", ["Titan Stove", "Cook Set Duo", "Water Filter", "Insulated Mug"], (12, 140)),
]
CITIES = [
    ("Paris", "France"), ("Lyon", "France"), ("Marseille", "France"), ("Grenoble", "France"),
    ("Geneva", "Switzerland"), ("Zurich", "Switzerland"), ("Brussels", "Belgium"),
    ("Barcelona", "Spain"), ("Madrid", "Spain"), ("Milan", "Italy"), ("Turin", "Italy"),
    ("Munich", "Germany"), ("Berlin", "Germany"), ("Tunis", "Tunisia"),
]
PREFIXES = ["Alpine", "Summit", "Trail", "Peak", "Granite", "Nordic", "Canyon", "Wild", "Ridge", "Basecamp", "Vertical", "Glacier"]
SUFFIXES = ["Outfitters", "Sports", "Adventure Co", "Mountain Shop", "Outdoor", "Gear", "Expeditions", "Store"]
FIRST = ["Camille", "Hugo", "Léa", "Nora", "Yanis", "Sofia", "Lucas", "Inès", "Malik", "Chloé", "Adam", "Emma"]
LAST = ["Martin", "Bernard", "Haddad", "Rossi", "Garcia", "Muller", "Dubois", "Ben Ali", "Moreau", "Lambert"]


def run_batches(cur, script: str) -> None:
    for batch in script.split("GO\n"):
        if batch.strip():
            cur.execute(batch)


def main() -> None:
    conn = pymssql.connect(server=HOST, port=PORT, user="sa", password=SA_PASSWORD, autocommit=True)
    cur = conn.cursor()
    cur.execute(f"IF DB_ID('{DB}') IS NOT NULL BEGIN ALTER DATABASE [{DB}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{DB}]; END")
    cur.execute(f"CREATE DATABASE [{DB}]")
    conn.close()

    conn = pymssql.connect(server=HOST, port=PORT, user="sa", password=SA_PASSWORD, database=DB, autocommit=True)
    cur = conn.cursor()
    run_batches(cur, SCHEMA)

    # employees
    employees = []
    roles = ["Account Manager"] * 6 + ["Sales Director", "Buyer", "Warehouse Lead"]
    for i, role in enumerate(roles):
        employees.append((f"{random.choice(FIRST)} {random.choice(LAST)}", role,
                          random.choice(["South", "North", "International"]),
                          dt.date(2018, 1, 1) + dt.timedelta(days=random.randint(0, 2000))))
    cur.executemany("INSERT INTO hr.employees (full_name, role, region, hired_on) VALUES (%s,%s,%s,%s)", employees)

    # products
    products = []
    for category, names, (lo, hi) in PRODUCTS:
        for name in names:
            price = round(random.uniform(lo, hi), 2)
            products.append((f"{category[:3].upper()}-{len(products) + 1:04d}", name, category, price,
                             round(price * random.uniform(0.45, 0.65), 2), random.randint(0, 400), random.choice([20, 30, 50])))
    cur.executemany("INSERT INTO inventory.products (sku, name, category, unit_price, unit_cost, stock_qty, reorder_level) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)", products)

    # customers
    customers, used = [], set()
    while len(customers) < 120:
        name = f"{random.choice(PREFIXES)} {random.choice(SUFFIXES)}"
        city, country = random.choice(CITIES)
        if (name, city) in used:
            continue
        used.add((name, city))
        customers.append((f"{name} {city}", random.choices(["Retail", "Wholesale", "Online"], [5, 2, 3])[0], city, country,
                          random.randint(1, 6), dt.date(2023, 1, 1) + dt.timedelta(days=random.randint(0, 700)),
                          1 if random.random() > 0.08 else 0))
    cur.executemany("INSERT INTO sales.customers (company_name, segment, city, country, account_manager_id, created_at, is_active) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)", customers)

    # orders, with seasonality (summer camping, winter clothing) and growth over time
    start, end = dt.date(2024, 1, 1), dt.date(2026, 9, 30)
    orders, items = [], []
    day = start
    while day <= end:
        growth = 1 + (day - start).days / 900
        season = 1.5 if day.month in (5, 6, 7) else 1.25 if day.month in (11, 12) else 1.0
        for _ in range(random.choices([0, 1, 2, 3], [3, 4, 2, 1])[0] if random.random() < 0.6 * growth * season / 1.6 else 0):
            cid = random.randint(1, len(customers))
            status = "Delivered" if day < end - dt.timedelta(days=20) else random.choice(["Shipped", "Pending"])
            if random.random() < 0.05:
                status = "Cancelled"
            orders.append((cid, day, status, customers[cid - 1][3]))
            oid = len(orders)
            wholesale = customers[cid - 1][1] == "Wholesale"
            for pid in random.sample(range(1, len(products) + 1), random.randint(1, 4)):
                cat = products[pid - 1][2]
                qty = random.randint(5, 40) if wholesale else random.randint(1, 4)
                if cat == "Tents" and day.month in (5, 6, 7):
                    qty += 2
                if cat == "Clothing" and day.month in (11, 12, 1):
                    qty += 2
                items.append((oid, pid, qty, products[pid - 1][3],
                              random.choice([0, 0, 0, 0.05, 0.10]) + (0.10 if wholesale else 0)))
        day += dt.timedelta(days=1)
    cur.executemany("INSERT INTO sales.orders (customer_id, order_date, status, shipping_country) VALUES (%s,%s,%s,%s)", orders)
    cur.executemany("INSERT INTO sales.order_items (order_id, product_id, quantity, unit_price, discount) VALUES (%s,%s,%s,%s,%s)", items)

    # logins: reader can only SELECT; writer can change the writable tables and append to the audit log
    rpw, wpw = os.environ["DB_READER_PASSWORD"], os.environ["DB_WRITER_PASSWORD"]
    for login, pw in (("agent_reader", rpw), ("agent_writer", wpw)):
        cur.execute(f"USE master; IF SUSER_ID('{login}') IS NULL CREATE LOGIN {login} WITH PASSWORD = '{pw}' "
                    f"ELSE ALTER LOGIN {login} WITH PASSWORD = '{pw}'")
        cur.execute(f"USE [{DB}]; CREATE USER {login} FOR LOGIN {login}")
    cur.execute(f"USE [{DB}]; ALTER ROLE db_datareader ADD MEMBER agent_reader; ALTER ROLE db_datareader ADD MEMBER agent_writer;")
    for table in os.getenv("WRITABLE_TABLES", "").split(","):
        if table.strip():
            cur.execute(f"GRANT INSERT, UPDATE, DELETE ON {table.strip()} TO agent_writer")
    cur.execute("GRANT INSERT ON audit.agent_write_log TO agent_writer")
    conn.close()

    print(f"{DB}: {len(employees)} employees, {len(products)} products, {len(customers)} customers, "
          f"{len(orders)} orders, {len(items)} order lines. Logins agent_reader / agent_writer ready.")


if __name__ == "__main__":
    main()
