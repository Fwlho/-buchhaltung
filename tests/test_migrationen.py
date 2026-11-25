"""Migrationen dürfen vorhandene Daten nie verlieren.

Hintergrund: SQLite kann Tabellen nur eingeschränkt ändern; Alembic baut sie dann neu auf
(neue Tabelle, Daten kopieren, alte löschen). Ist dabei die Fremdschlüssel-Prüfung mit
ON DELETE CASCADE aktiv, löscht das Entfernen der alten Tabelle abhängige Zeilen mit
(z. B. alle Rechnungspositionen). Dieser Test spielt Daten ein und migriert darüber.
"""
import json
import sqlite3

from alembic import command
from alembic.config import Config

from app.config import BASE_DIR


def _config(url: str) -> Config:
    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    return cfg


def test_migration_behaelt_positionen_und_zahlungen(tmp_path):
    path = tmp_path / "alt.db"
    cfg = _config(f"sqlite:///{path.as_posix()}")
    command.upgrade(cfg, "0004")  # Stand vor der Migration, die `documents` neu aufbaut

    con = sqlite3.connect(path)
    now = "2026-03-01 10:00:00"
    con.execute("INSERT INTO tenants (id, name, status, setup_completed, created_at, updated_at) "
                "VALUES (1, 'Testfirma', 'aktiv', 1, ?, ?)", (now, now))
    con.execute("INSERT INTO documents (id, tenant_id, direction, kind, state, status, source, pending, needs_review, "
                "review_fields, hints, created_at, updated_at, gross_total) "
                "VALUES (1, 1, 'ausgang', 'rechnung', 'festgeschrieben', 'offen', 'manuell', 0, 0, '[]', '[]', ?, ?, 119)",
                (now, now))
    con.execute("INSERT INTO document_lines (id, tenant_id, document_id, position, title, vat_rate, net, vat, gross, "
                "created_at, updated_at) VALUES (1, 1, 1, 1, 'Leistung', 19, 100, 19, 119, ?, ?)", (now, now))
    con.execute("INSERT INTO payments (id, tenant_id, document_id, paid_on, amount, skonto, created_at, updated_at) "
                "VALUES (1, 1, 1, '2026-03-05', 50, 0, ?, ?)", (now, now))
    con.commit()
    con.close()

    command.upgrade(cfg, "head")

    con = sqlite3.connect(path)
    assert con.execute("SELECT COUNT(*) FROM document_lines").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    con.close()


def test_admin_passwort_migration_behaelt_benutzer_und_zugehoerigkeiten(tmp_path):
    path = tmp_path / "alt.db"
    cfg = _config(f"sqlite:///{path.as_posix()}")
    command.upgrade(cfg, "0009")

    con = sqlite3.connect(path)
    now = "2026-10-01 10:00:00"
    con.execute("INSERT INTO tenants (id, name, status, setup_completed, created_at, updated_at) "
                "VALUES (1, 'Testfirma', 'aktiv', 1, ?, ?)", (now, now))
    con.execute("INSERT INTO users (id, email, name, password_hash, is_platform_admin, is_active, created_at, updated_at) "
                "VALUES (1, 'a@example.org', 'A', 'x', 1, 1, ?, ?)", (now, now))
    con.execute("INSERT INTO memberships (id, user_id, tenant_id, role, permissions, extra_rights, is_active, "
                "created_at, updated_at) VALUES (1, 1, 1, 'inhaber', '{}', '[]', 1, ?, ?)", (now, now))
    con.execute("INSERT INTO sessions (id, token_hash, user_id, stage, created_at, last_seen_at, expires_at) "
                "VALUES (1, 'abc', 1, 'ok', ?, ?, ?)", (now, now, now))
    con.commit()
    con.close()

    command.upgrade(cfg, "head")

    con = sqlite3.connect(path)
    assert con.execute("SELECT COUNT(*) FROM memberships").fetchone()[0] == 1
    assert con.execute("SELECT admin_password_hash FROM users").fetchone() == (None,)
    assert con.execute("SELECT admin_until FROM sessions").fetchone() == (None,)
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    con.close()


def test_auftraege_migration_uebernimmt_rechte_von_angeboten(tmp_path):
    """0017: Bestehende Benutzer bekommen für „auftraege“ dieselbe Stufe wie für „angebote“."""
    path = tmp_path / "alt.db"
    cfg = _config(f"sqlite:///{path.as_posix()}")
    command.upgrade(cfg, "0016")

    con = sqlite3.connect(path)
    now = "2026-11-01 10:00:00"
    con.execute("INSERT INTO tenants (id, name, status, setup_completed, created_at, updated_at) "
                "VALUES (1, 'Testfirma', 'aktiv', 1, ?, ?)", (now, now))
    for uid, email in ((1, "a@example.org"), (2, "b@example.org")):
        con.execute("INSERT INTO users (id, email, name, password_hash, is_platform_admin, is_active, created_at, "
                    "updated_at) VALUES (?, ?, 'X', 'x', 0, 1, ?, ?)", (uid, email, now, now))
    con.execute("INSERT INTO memberships (id, user_id, tenant_id, role, permissions, extra_rights, is_active, "
                "created_at, updated_at) VALUES (1, 1, 1, 'mitarbeiter', '{\"angebote\": \"bearbeiten\"}', '[]', 1, ?, ?)",
                (now, now))
    con.execute("INSERT INTO memberships (id, user_id, tenant_id, role, permissions, extra_rights, is_active, "
                "created_at, updated_at) VALUES (2, 2, 1, 'buchhaltung', '{\"angebote\": \"lesen\"}', '[]', 1, ?, ?)",
                (now, now))
    con.commit()
    con.close()

    command.upgrade(cfg, "head")

    con = sqlite3.connect(path)
    perms = [json.loads(p) for (p,) in con.execute("SELECT permissions FROM memberships ORDER BY id")]
    assert perms[0]["auftraege"] == "bearbeiten" and perms[1]["auftraege"] == "lesen"
    assert con.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    con.close()


def test_ablage_migration_uebernimmt_alten_ordner(tmp_path):
    """0019: „rechnung.ablage_ordner“ wird zu „ordner.ablage_ausgang“; schon festgeschriebene Belege gelten als abgelegt."""
    path = tmp_path / "alt.db"
    cfg = _config(f"sqlite:///{path.as_posix()}")
    command.upgrade(cfg, "0018")

    con = sqlite3.connect(path)
    now = "2026-11-01 10:00:00"
    con.execute("INSERT INTO tenants (id, name, status, setup_completed, created_at, updated_at) "
                "VALUES (1, 'Testfirma', 'aktiv', 1, ?, ?)", (now, now))
    con.execute("INSERT INTO tenant_settings (id, tenant_id, key, value, created_at, updated_at) "
                "VALUES (1, 1, 'rechnung.ablage_ordner', '\"D:/Test/Ablage\"', ?, ?)", (now, now))
    for doc_id, state in ((1, "festgeschrieben"), (2, "entwurf")):
        con.execute("INSERT INTO documents (id, tenant_id, direction, kind, state, status, source, pending, "
                    "needs_review, review_fields, created_at, updated_at) VALUES (?, 1, 'eingang', 'rechnung', ?, "
                    "'offen', 'upload', 0, 0, '[]', ?, ?)", (doc_id, state, now, now))
    con.commit()
    con.close()

    command.upgrade(cfg, "head")

    con = sqlite3.connect(path)
    rows = con.execute("SELECT key, value FROM tenant_settings ORDER BY key").fetchall()
    assert rows == [("ordner.ablage_ausgang", '"D:/Test/Ablage"')]
    stamps = dict(con.execute("SELECT id, ablage_at FROM documents").fetchall())
    assert stamps[1] is not None and stamps[2] is None
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    con.close()
