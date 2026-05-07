"""
База данных SQLite для хранения репортов и адресов.
"""

import sqlite3
import json
import logging
from datetime import datetime, date

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """Создаёт таблицы если их нет."""
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS reports (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     INTEGER NOT NULL,
                    username    TEXT,
                    text        TEXT,
                    scam_type   TEXT,
                    summary     TEXT,
                    confidence  INTEGER DEFAULT 0,
                    created_at  TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS addresses (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    address      TEXT UNIQUE NOT NULL,
                    network      TEXT,
                    report_count INTEGER DEFAULT 1,
                    risk_score   INTEGER DEFAULT 50,
                    scam_type    TEXT,
                    first_seen   TEXT DEFAULT (datetime('now')),
                    last_seen    TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS report_addresses (
                    report_id  INTEGER REFERENCES reports(id),
                    address_id INTEGER REFERENCES addresses(id),
                    PRIMARY KEY (report_id, address_id)
                );

                CREATE TABLE IF NOT EXISTS confirmations (
                    report_id INTEGER REFERENCES reports(id),
                    user_id   INTEGER,
                    created_at TEXT DEFAULT (datetime('now')),
                    PRIMARY KEY (report_id, user_id)
                );

                CREATE INDEX IF NOT EXISTS idx_addresses_address ON addresses(address);
                CREATE INDEX IF NOT EXISTS idx_reports_user ON reports(user_id);
            """)
        logger.info(f"База данных инициализирована: {self.db_path}")

    def save_report(self, user_id: int, username: str, original_text: str, result: dict) -> int:
        """Сохраняет репорт и все найденные адреса. Возвращает ID репорта."""
        with self._connect() as conn:
            # Сохраняем репорт
            cur = conn.execute(
                """INSERT INTO reports (user_id, username, text, scam_type, summary, confidence)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    username,
                    original_text[:5000],
                    result.get("scam_type"),
                    result.get("summary"),
                    result.get("confidence", 0),
                )
            )
            report_id = cur.lastrowid

            # Сохраняем каждый адрес
            for addr_info in result.get("addresses", []):
                address = addr_info.get("address", "").strip()
                if not address:
                    continue

                network = addr_info.get("network", "Unknown")
                scam_type = result.get("scam_type")

                # Upsert адреса
                existing = conn.execute(
                    "SELECT id, report_count, risk_score FROM addresses WHERE address = ?",
                    (address,)
                ).fetchone()

                if existing:
                    new_count = existing["report_count"] + 1
                    # Увеличиваем risk_score с каждой жалобой (максимум 95)
                    new_score = min(95, existing["risk_score"] + 5)
                    conn.execute(
                        """UPDATE addresses
                           SET report_count = ?, risk_score = ?, last_seen = datetime('now')
                           WHERE address = ?""",
                        (new_count, new_score, address)
                    )
                    addr_id = existing["id"]
                    addr_info["already_in_db"] = True
                else:
                    cur2 = conn.execute(
                        """INSERT INTO addresses (address, network, scam_type, risk_score)
                           VALUES (?, ?, ?, ?)""",
                        (address, network, scam_type, 55)
                    )
                    addr_id = cur2.lastrowid
                    addr_info["already_in_db"] = False

                # Связываем репорт с адресом
                conn.execute(
                    "INSERT OR IGNORE INTO report_addresses (report_id, address_id) VALUES (?, ?)",
                    (report_id, addr_id)
                )

            conn.commit()
            return report_id

    def get_address(self, address: str) -> dict | None:
        """Ищет адрес в базе."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM addresses WHERE address = ?", (address,)
            ).fetchone()
            return dict(row) if row else None
    def save_related_addresses(self, related_addresses: list):
        """Сохраняет связанные адреса из on-chain анализа."""
        with self._connect() as conn:
            for rel in related_addresses:
                existing = conn.execute(
                    "SELECT id, report_count, risk_score FROM addresses WHERE address = ?",
                    (rel.address,)
                ).fetchone()

                if existing:
                    new_count = existing["report_count"] + 1
                    new_score = max(existing["risk_score"], rel.risk_score)

                    conn.execute("""
                        UPDATE addresses
                        SET report_count = ?,
                            risk_score = ?,
                            last_seen = datetime('now')
                        WHERE address = ?
                    """, (new_count, new_score, rel.address))
                else:
                    conn.execute("""
                        INSERT INTO addresses
                        (address, network, report_count, risk_score, scam_type)
                        VALUES (?, ?, ?, ?, ?)
                    """, (
                        rel.address,
                        rel.network,
                        rel.tx_count,
                        rel.risk_score,
                        "related scam wallet"
                    ))

            conn.commit()
    def confirm_report(self, report_id: int, user_id: int):
        """Пользователь подтверждает что тоже пострадал от этого скама."""
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO confirmations (report_id, user_id) VALUES (?, ?)",
                (report_id, user_id)
            )
            # Повышаем risk score связанных адресов
            conn.execute("""
                UPDATE addresses SET risk_score = MIN(95, risk_score + 3)
                WHERE id IN (
                    SELECT address_id FROM report_addresses WHERE report_id = ?
                )
            """, (report_id,))
            conn.commit()

    def get_stats(self) -> dict:
        """Общая статистика базы."""
        today = date.today().isoformat()
        with self._connect() as conn:
            total_addresses = conn.execute("SELECT COUNT(*) FROM addresses").fetchone()[0]
            total_reports = conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
            total_users = conn.execute("SELECT COUNT(DISTINCT user_id) FROM reports").fetchone()[0]
            today_reports = conn.execute(
                "SELECT COUNT(*) FROM reports WHERE created_at LIKE ?", (f"{today}%",)
            ).fetchone()[0]
            top_network = conn.execute(
                "SELECT network, COUNT(*) as c FROM addresses GROUP BY network ORDER BY c DESC LIMIT 1"
            ).fetchone()

            return {
                "total_addresses": total_addresses,
                "total_reports": total_reports,
                "total_users": total_users,
                "today_reports": today_reports,
                "top_network": top_network["network"] if top_network else "—",
            }

    def get_top_addresses(self, limit: int = 10) -> list:
        """Адреса с наибольшим числом жалоб."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT address, network, report_count, risk_score, scam_type
                   FROM addresses
                   ORDER BY report_count DESC, risk_score DESC
                   LIMIT ?""",
                (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def export_csv(self, path: str):
        """Экспортирует базу адресов в CSV (для инвесторов / партнёров)."""
        import csv
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT address, network, report_count, risk_score, scam_type, first_seen FROM addresses ORDER BY report_count DESC"
            ).fetchall()

        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["address", "network", "report_count", "risk_score", "scam_type", "first_seen"])
            for row in rows:
                writer.writerow(list(row))

        logger.info(f"Экспортировано {len(rows)} адресов в {path}")
        return len(rows)
