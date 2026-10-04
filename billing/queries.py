"""Schema and reporting queries retained from the existing shop database."""

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT UNIQUE NOT NULL
);

-- "subtypes" is a generic second-level grouping under a category.
-- For Jeans this holds brands (LP, Mufti, US Polo...).
-- For T-Shirts this holds styles (Round Neck, Collar).
-- For categories that don't need it, it's simply left empty.
CREATE TABLE IF NOT EXISTS subtypes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id   INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    UNIQUE(category_id, name)
);

CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    category_id   INTEGER NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
    subtype_id    INTEGER REFERENCES subtypes(id) ON DELETE SET NULL,
    barcode       TEXT UNIQUE,
    size          TEXT,
    color         TEXT,
    rate          REAL NOT NULL DEFAULT 0,
    stock_qty     INTEGER NOT NULL DEFAULT 0,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS customers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    phone         TEXT UNIQUE,
    address       TEXT,
    notes         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- What a customer says they want next time ("wishlist" / follow-up notes)
CREATE TABLE IF NOT EXISTS customer_wishlist (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id     INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    item_description TEXT NOT NULL,
    date_added      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    fulfilled       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bills (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_no           TEXT UNIQUE NOT NULL,
    customer_id       INTEGER REFERENCES customers(id) ON DELETE SET NULL,
    bill_date         TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    subtotal          REAL NOT NULL,
    discount_percent  REAL NOT NULL DEFAULT 0,
    discount_amount   REAL NOT NULL DEFAULT 0,
    total             REAL NOT NULL,
    payment_mode      TEXT DEFAULT 'Cash'
);

CREATE TABLE IF NOT EXISTS bill_items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id             INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    item_id             INTEGER REFERENCES items(id) ON DELETE SET NULL,
    item_name_snapshot  TEXT NOT NULL,
    category_snapshot   TEXT,
    quantity            INTEGER NOT NULL,
    rate                REAL NOT NULL,
    subtotal            REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_items_category ON items(category_id);
CREATE INDEX IF NOT EXISTS idx_items_barcode ON items(barcode);
CREATE INDEX IF NOT EXISTS idx_bills_date ON bills(bill_date);
CREATE INDEX IF NOT EXISTS idx_bill_items_bill ON bill_items(bill_id);
CREATE INDEX IF NOT EXISTS idx_customers_phone ON customers(phone);

-- Performance fix: bills.customer_id and bill_items.item_id had no index,
-- so every customer-history / balances lookup and every "top selling
-- item" stat was a full table scan. Harmless at a handful of rows, but
-- this is exactly what turns into visible lag once a shop has a few
-- hundred bills.
CREATE INDEX IF NOT EXISTS idx_bills_customer ON bills(customer_id);
CREATE INDEX IF NOT EXISTS idx_bill_items_item ON bill_items(item_id);
CREATE INDEX IF NOT EXISTS idx_customers_name ON customers(name);
"""


class Queries:
    def get_categories(self):
        with self._conn() as conn:
            return conn.execute("SELECT * FROM categories ORDER BY name").fetchall()

    def get_bill_payment_history(self, bill_id):
        with self._conn() as conn:
            return conn.execute(
                """
                SELECT *
                FROM payments
                WHERE bill_id=?
                ORDER BY payment_date DESC, id DESC
                """,
                (bill_id,),
            ).fetchall()

    def get_customer_balances(self):
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT
                    c.id,
                    c.name,
                    c.phone,

                    COALESCE(SUM(b.total), 0) AS total_billed,

                    COALESCE(
                        (
                            SELECT SUM(p.amount)
                            FROM payments p
                            WHERE p.customer_id = c.id
                        ),
                        0
                    ) AS total_paid

                FROM customers c
                LEFT JOIN bills b
                    ON b.customer_id = c.id

                GROUP BY c.id
                ORDER BY c.name
                """
            ).fetchall()

            result = []
            for row in rows:
                data = dict(row)
                data["balance"] = max(data["total_billed"] - data["total_paid"], 0)
                result.append(data)

            return result

    def _column_exists(self, conn, table_name, column_name):
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return any(row["name"] == column_name for row in rows)

    def get_expenses(self, date_from=None, date_to=None):
        q = """
            SELECT *
            FROM expenses
            WHERE 1=1
        """

        params = []

        if date_from:
            q += " AND date(expense_date) >= date(?)"
            params.append(date_from)

        if date_to:
            q += " AND date(expense_date) <= date(?)"
            params.append(date_to)

        q += " ORDER BY expense_date DESC, id DESC"

        with self._conn() as conn:
            return conn.execute(q, params).fetchall()

    def add_category(self, name: str):
        with self._conn() as conn:
            conn.execute("INSERT INTO categories(name) VALUES (?)", (name.strip(),))

    def rename_category(self, cat_id: int, new_name: str):
        with self._conn() as conn:
            conn.execute(
                "UPDATE categories SET name=? WHERE id=?", (new_name.strip(), cat_id)
            )

    def delete_category(self, cat_id: int):
        with self._conn() as conn:
            conn.execute("DELETE FROM categories WHERE id=?", (cat_id,))

    def get_subtypes(self, category_id: int):
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM subtypes WHERE category_id=? ORDER BY name",
                (category_id,),
            ).fetchall()

    def add_subtype(self, category_id: int, name: str):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO subtypes(category_id, name) VALUES (?,?)",
                (category_id, name.strip()),
            )

    def get_item_by_id(self, item_id: int):
        with self._conn() as conn:
            return conn.execute(
                """SELECT items.*, categories.name AS category_name,
                          subtypes.name AS subtype_name
                   FROM items JOIN categories ON categories.id = items.category_id
                   LEFT JOIN subtypes ON subtypes.id = items.subtype_id
                   WHERE items.id=?""",
                (item_id,),
            ).fetchone()

    def get_customer_purchase_history(self, cid):
        # Performance fix: this used to return bare bill rows, and
        # customers_tab.py would then call get_bill(b["id"]) in a loop
        # (one extra full query per bill, every time a customer with a
        # long history was opened) purely to count pieces sold. Folding
        # the piece count into this single query removes that N+1 --
        # this is one of the things that made the app feel laggy once a
        # regular customer had dozens of bills.
        with self._conn() as conn:
            return conn.execute(
                """
                SELECT bills.*,
                       COALESCE(
                           (SELECT SUM(quantity) FROM bill_items
                            WHERE bill_items.bill_id = bills.id),
                           0
                       ) AS piece_count
                FROM bills
                WHERE customer_id=?
                ORDER BY bill_date DESC
                """,
                (cid,),
            ).fetchall()

    def add_wishlist(self, customer_id, description):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO customer_wishlist(customer_id, item_description) VALUES (?,?)",
                (customer_id, description.strip()),
            )

    def get_wishlist(self, customer_id):
        with self._conn() as conn:
            return conn.execute(
                """SELECT * FROM customer_wishlist WHERE customer_id=?
                   ORDER BY fulfilled ASC, date_added DESC""",
                (customer_id,),
            ).fetchall()

    def set_wishlist_fulfilled(self, wishlist_id, fulfilled=1):
        with self._conn() as conn:
            conn.execute(
                "UPDATE customer_wishlist SET fulfilled=? WHERE id=?",
                (fulfilled, wishlist_id),
            )

    def stat_totals(self, date_from=None, date_to=None):
        q = """
            SELECT
                COALESCE(SUM(total), 0) AS revenue,
                COUNT(*) AS bill_count
            FROM bills
            WHERE 1=1
        """
        params = []

        if date_from:
            q += " AND date(bill_date) >= date(?)"
            params.append(date_from)
        if date_to:
            q += " AND date(bill_date) <= date(?)"
            params.append(date_to)

        with self._conn() as conn:
            row = conn.execute(q, params).fetchone()

            qp = """
                SELECT COALESCE(SUM(bi.quantity), 0) AS pieces
                FROM bill_items bi
                JOIN bills b ON b.id = bi.bill_id
                WHERE 1=1
            """
            pparams = []

            if date_from:
                qp += " AND date(b.bill_date) >= date(?)"
                pparams.append(date_from)
            if date_to:
                qp += " AND date(b.bill_date) <= date(?)"
                pparams.append(date_to)

            pieces = conn.execute(qp, pparams).fetchone()["pieces"]

            expense_q = """
                SELECT COALESCE(SUM(amount), 0) AS expenses
                FROM expenses
                WHERE 1=1
            """
            expense_params = []

            if date_from:
                expense_q += " AND date(expense_date) >= date(?)"
                expense_params.append(date_from)
            if date_to:
                expense_q += " AND date(expense_date) <= date(?)"
                expense_params.append(date_to)

            expenses = conn.execute(expense_q, expense_params).fetchone()["expenses"]

            # Outstanding credit is based on bills in the selected period.
            outstanding_q = """
                SELECT COALESCE(SUM(
                    CASE
                        WHEN b.total - COALESCE(
                            (SELECT SUM(p.amount)
                             FROM payments p
                             WHERE p.bill_id = b.id), 0
                        ) > 0
                        THEN b.total - COALESCE(
                            (SELECT SUM(p.amount)
                             FROM payments p
                             WHERE p.bill_id = b.id), 0
                        )
                        ELSE 0
                    END
                ), 0) AS outstanding
                FROM bills b
                WHERE 1=1
            """
            outstanding_params = []

            if date_from:
                outstanding_q += " AND date(b.bill_date) >= date(?)"
                outstanding_params.append(date_from)
            if date_to:
                outstanding_q += " AND date(b.bill_date) <= date(?)"
                outstanding_params.append(date_to)

            outstanding = conn.execute(outstanding_q, outstanding_params).fetchone()[
                "outstanding"
            ]

            # Payments collected during the selected period.
            payment_q = """
                SELECT COALESCE(SUM(amount), 0) AS collected
                FROM payments
                WHERE 1=1
            """
            payment_params = []

            if date_from:
                payment_q += " AND date(payment_date) >= date(?)"
                payment_params.append(date_from)
            if date_to:
                payment_q += " AND date(payment_date) <= date(?)"
                payment_params.append(date_to)

            collected = conn.execute(payment_q, payment_params).fetchone()["collected"]

            revenue = row["revenue"]

            return {
                "revenue": revenue,
                "expenses": expenses,
                "net_profit": revenue - expenses,
                "payments_collected": collected,
                "outstanding": outstanding,
                "bill_count": row["bill_count"],
                "pieces": pieces,
                "avg_bill": (revenue / row["bill_count"]) if row["bill_count"] else 0,
            }

    def stat_monthly_sales(self):
        """Returns list of (month 'YYYY-MM', revenue, bill_count) for every
        month that had at least one sale, oldest first. Used for the
        Monthly Sales chart, which always shows the shop's full history
        regardless of the Statistics page's Period filter."""
        with self._conn() as conn:
            return conn.execute(
                """SELECT strftime('%Y-%m', bill_date) AS month,
                          SUM(total) AS revenue, COUNT(*) AS bill_count
                   FROM bills
                   GROUP BY month
                   ORDER BY month"""
            ).fetchall()

    def stat_top_items(self, date_from=None, date_to=None, limit=10, by="quantity"):
        # Prefer the item's *current* name/category (via item_id) so that
        # renaming an item or its category in Inventory is reflected in
        # past statistics too. Falls back to the name recorded at the time
        # of sale only if the original item can no longer be found (e.g.
        # it was part of very old data with no linked item record).
        order_col = "total_qty" if by == "quantity" else "total_revenue"
        q = """SELECT COALESCE(items.name, bi.item_name_snapshot) AS name,
                       COALESCE(categories.name, bi.category_snapshot) AS category,
                       SUM(bi.quantity) AS total_qty, SUM(bi.subtotal) AS total_revenue
                FROM bill_items bi
                JOIN bills b ON b.id = bi.bill_id
                LEFT JOIN items ON items.id = bi.item_id
                LEFT JOIN categories ON categories.id = items.category_id
                WHERE 1=1"""
        params = []
        if date_from:
            q += " AND date(b.bill_date) >= date(?)"
            params.append(date_from)
        if date_to:
            q += " AND date(b.bill_date) <= date(?)"
            params.append(date_to)
        q += f"""
                GROUP BY COALESCE(items.name, bi.item_name_snapshot),
                         COALESCE(categories.name, bi.category_snapshot)
                ORDER BY {order_col} DESC LIMIT ?"""
        params.append(limit)
        with self._conn() as conn:
            return conn.execute(q, params).fetchall()
