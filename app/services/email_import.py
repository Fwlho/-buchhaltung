"""E-Mail-Rechnungsimport (IMAP) – Regeln aus dem bewährten alten Programm (KONZEPT.md 6.4).

Ablauf je Abruf:
1. Rechnungs-Ordner finden (tolerant) bzw. anlegen + abonnieren.
2. Je Mail: Message-ID prüfen (Duplikatschutz je Firma). Anhänge (PDF, ZUGFeRD-PDF, XRechnung-XML)
   werden wie ein Upload ausgelesen und landen im PRÜFBEREICH.
3. Erst nach dem Speichern in der Datenbank wird die Mail verschoben:
   - MOVE, wenn der Server es kann (verschiebt ganz oder gar nicht);
   - sonst COPY, Zielordner nachzählen, und NUR wenn die Zahl stimmt: \\Deleted + UID EXPUNGE
     (nur diese Mails). Ohne UIDPLUS wird gar nicht verschoben, damit niemals andere Mails
     endgültig gelöscht werden.
   - Leeres Feld „Ordner nach Import“ = nicht verschieben.
   Mails ohne passenden Anhang bleiben liegen, ebenso Mails, deren Anhänge alle abgelehnt wurden
   (die werden im Prüfbereich gemeldet).
4. Jede angesehene Mail kommt ins Protokoll (Status importiert/ohne_anhang/abgelehnt). Bekannte Mails
   werden beim nächsten Abruf nur am Kopf erkannt und zählen nicht zur Höchstzahl je Abruf – so kommen
   neue Mails immer an, auch wenn viele alte im Ordner liegen bleiben.
E-Mails werden NIEMALS gelöscht.
"""
import email
import email.policy
import email.utils
import threading
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import utcnow
from app.formatting import format_datetime
from app.models import EmailImportLog
from app.services import ablage, audit, belege, einstellungen, imap

MAX_MAILS_PER_RUN = 100   # höchstens so viele Mails je Abruf vollständig laden (bekannte zählen nicht)
STATUS_IMPORTED = "importiert"
STATUS_NO_ATTACHMENT = "ohne_anhang"
STATUS_REJECTED = "abgelehnt"
ATTACHMENT_TYPES = {"pdf", "xml"}
STATUS_KEY = "imap.letzter_abruf"

_tenant_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(tenant_id: int) -> threading.Lock:
    with _locks_guard:
        return _tenant_locks.setdefault(tenant_id, threading.Lock())


@dataclass
class RunResult:
    ok: bool = False
    imported_docs: int = 0
    imported_mails: int = 0
    skipped: int = 0
    without_attachment: int = 0
    rejected: int = 0
    moved: int = 0
    errors: list[str] = field(default_factory=list)
    message: str = ""

    def as_dict(self) -> dict:
        return {"ok": self.ok, "zeit": format_datetime(utcnow()), "belege": self.imported_docs,
                "mails": self.imported_mails, "uebersprungen": self.skipped, "ohne_anhang": self.without_attachment, "abgelehnt": self.rejected,
                "verschoben": self.moved, "fehler": self.errors[:20], "meldung": self.message}


# ------------------------------------------------------------------ Mail lesen
def _header_message_id(raw: bytes) -> str:
    """Kennung aus dem Mail-Kopf – dieselbe, die import_message speichert (auch ohne Message-ID)."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    return _message_key(msg)


def _message_key(msg) -> str:
    return (str(msg.get("Message-ID") or "").strip() or _fallback_id(msg))[:500]


def _fallback_id(msg) -> str:
    return f"<ohne-id-{msg.get('From', '')}-{msg.get('Date', '')}-{msg.get('Subject', '')}>"[:500]


def _attachments(msg) -> list[tuple[str, bytes]]:
    """(Dateiname, Inhalt) aller Anhänge, die PDF oder XML sein könnten. Die echte Prüfung
    (Magic Bytes, Größe) macht anschließend die Ablage."""
    found = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = part.get_filename() or ""
        ctype = (part.get_content_type() or "").lower()
        looks = (ctype in ("application/pdf", "application/xml", "text/xml")
                 or filename.lower().endswith((".pdf", ".xml")))
        if not looks:
            continue
        payload = part.get_payload(decode=True)
        if payload:
            found.append((filename or "anhang", payload))
    return found


def _mail_info(msg) -> dict:
    name, addr = email.utils.parseaddr(str(msg.get("From") or ""))
    mail_date = None
    try:
        parsed = email.utils.parsedate_to_datetime(str(msg.get("Date")))
        mail_date = parsed.date() if parsed else None
    except (TypeError, ValueError):
        pass
    return {"sender": f"{name} <{addr}>".strip() if name else addr, "sender_name": name or addr,
            "subject": str(msg.get("Subject") or "")[:500], "date": mail_date}


def known_status(db: Session, message_id: str) -> str | None:
    """Status einer schon angesehenen Mail (None = noch nie gesehen)."""
    return db.scalar(select(EmailImportLog.status).where(EmailImportLog.message_id == message_id))


def already_imported(db: Session, message_id: str) -> bool:
    return known_status(db, message_id) == STATUS_IMPORTED


def import_message(db: Session, raw: bytes, *, tenant_id: int, folder: str) -> tuple[str, list[int], list[str]]:
    """Eine Mail verarbeiten. Gibt (Status, Beleg-IDs, Probleme) zurück.
    Status: "importiert", "bekannt" (schon importiert), "ohne_anhang", "abgelehnt" (alle Anhänge unbrauchbar –
    die Mail bleibt im Postfach). Jede Mail kommt ins Protokoll. Ruft commit auf (je Mail)."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    message_id = _message_key(msg)
    known = known_status(db, message_id)
    if known == STATUS_IMPORTED:
        return "bekannt", [], []
    if known is not None:
        return known, [], []
    info = _mail_info(msg)
    doc_ids: list[int] = []
    problems: list[str] = []
    duplicates = 0
    for filename, payload in _attachments(msg):
        try:
            doc = belege.create_from_bytes(db, payload, filename, tenant_id=tenant_id, user_id=None, ip=None,
                                           source="email", allowed=ATTACHMENT_TYPES, mail=info)
            doc_ids.append(doc.id)
        except belege.DuplicateUpload:
            duplicates += 1
            problems.append(f"„{filename}“ ist bereits vorhanden und wurde übersprungen.")
        except ablage.UploadRejected as exc:
            problems.append(f"„{filename}“: {exc}")
    if doc_ids or duplicates:
        status = STATUS_IMPORTED      # Beleg angelegt bzw. schon im Programm -> Mail darf verschoben werden
    elif problems:
        status = STATUS_REJECTED      # nur unbrauchbare Anhänge -> bleibt liegen und wird gemeldet
        problems.append("Die Mail bleibt im Postfach – bitte die Rechnung von Hand hochladen.")
    else:
        status = STATUS_NO_ATTACHMENT
    db.add(EmailImportLog(tenant_id=tenant_id, message_id=message_id, status=status, subject=info["subject"],
                          sender=info["sender"][:300], mail_date=info["date"], folder=folder[:200],
                          document_ids=doc_ids, note="; ".join(problems)[:1000] or None))
    db.commit()
    return status, doc_ids, problems


# ------------------------------------------------------------------- Abruf
def _uid_list(conn) -> list[bytes]:
    typ, data = conn.uid("SEARCH", None, "ALL")
    if typ != "OK":
        raise imap.ImapError("Die Suche im Rechnungs-Ordner ist fehlgeschlagen.")
    return data[0].split() if data and data[0] else []


def _fetch(conn, uid: bytes, part: str) -> bytes | None:
    typ, data = conn.uid("FETCH", uid, f"(BODY.PEEK[{part}])")
    if typ != "OK" or not data:
        return None
    for item in data:
        if isinstance(item, tuple) and len(item) == 2:
            return item[1]
    return None


def _prepare_folders(conn, cfg: imap.ImapConfig, result: RunResult) -> tuple[str, str | None]:
    info = imap.list_folders(conn)
    wanted_inbox = imap.normalize_setting(cfg.folder_inbox) or "Rechnungen"
    inbox = imap.resolve(wanted_inbox, info.names)
    if inbox is None:
        new_name = imap.namespaced(wanted_inbox, info)
        imap.ensure_folder(conn, new_name)
        info = imap.list_folders(conn)
        inbox = imap.resolve(new_name, info.names) or imap.resolve(wanted_inbox, info.names) or new_name

    wanted_done = imap.normalize_setting(cfg.folder_done)
    if not wanted_done:
        return inbox, None
    done = imap.resolve(wanted_done, info.names)
    if done is None:
        imap.ensure_folder(conn, imap.sibling_name(inbox, wanted_done, info.delimiter))
        info = imap.list_folders(conn)
        done = imap.resolve(imap.sibling_name(inbox, wanted_done, info.delimiter), info.names) or \
            imap.resolve(wanted_done, info.names)
        if done is None:
            result.errors.append(f"Zielordner „{wanted_done}“ konnte nicht angelegt werden – die Mails bleiben "
                                 "im Eingangsordner liegen.")
    return inbox, done


def _move(conn, inbox: str, done: str, uids: list[bytes], result: RunResult) -> list[bytes]:
    """Verschiebt sicher (siehe Moduldoku). Gibt die nachweislich verschobenen UIDs zurück."""
    if not uids:
        return []
    if imap.supports(conn, "MOVE"):
        moved = []
        conn.select(imap.quote(inbox))
        for uid in uids:
            typ, _ = conn.uid("MOVE", uid, imap.quote(done))
            if typ == "OK":
                moved.append(uid)
            else:
                result.errors.append(f"Mail {uid.decode()} konnte nicht verschoben werden (bleibt liegen).")
        return moved
    if not imap.supports(conn, "UIDPLUS"):
        result.errors.append("Der Mailserver kann weder MOVE noch UID EXPUNGE – zur Sicherheit wird nicht "
                             "verschoben. Bitte die Mails im Mailprogramm selbst verschieben.")
        return []

    before = imap.count_messages(conn, done)
    if before is None:
        result.errors.append(f"Zielordner „{done}“ ist nicht lesbar – es wird nichts verschoben.")
        return []
    conn.select(imap.quote(inbox))
    copied = []
    for uid in uids:
        typ, _ = conn.uid("COPY", uid, imap.quote(done))
        if typ == "OK":
            copied.append(uid)
        else:
            result.errors.append(f"Mail {uid.decode()} konnte nicht kopiert werden (bleibt liegen).")
    if not copied:
        return []
    after = imap.count_messages(conn, done)
    conn.select(imap.quote(inbox))
    if after != before + len(copied):
        result.errors.append("Sicherheitsabbruch: Die Kopien im Zielordner konnten nicht bestätigt werden. "
                             "Es wurde nichts aus dem Eingangsordner entfernt (evtl. liegen Mails jetzt doppelt vor).")
        return []
    uid_set = b",".join(copied)
    conn.uid("STORE", uid_set, "+FLAGS", r"(\Deleted)")
    conn.uid("EXPUNGE", uid_set)
    return copied


def run(db: Session, tenant_id: int) -> RunResult:
    """Ein Abruf für die aktive Firma. Läuft für dieselbe Firma nie doppelt gleichzeitig."""
    result = RunResult()
    lock = _lock_for(tenant_id)
    if not lock.acquire(blocking=False):
        result.message = "Ein Abruf läuft bereits."
        return result
    conn = None
    try:
        cfg = imap.config_for(db)
        if cfg is None:
            result.message = "Kein Postfach eingerichtet (Einstellungen → E-Mail)."
            return result
        conn = imap.connect(cfg)
        inbox, done = _prepare_folders(conn, cfg, result)
        typ, _ = conn.select(imap.quote(inbox))
        if typ != "OK":
            raise imap.ImapError(f"Ordner „{inbox}“ konnte nicht geöffnet werden.")
        to_move: list[bytes] = []
        uids = _uid_list(conn)
        loaded = 0
        more = False
        for uid in uids:
            try:
                header = _fetch(conn, uid, "HEADER")
                message_id = _header_message_id(header) if header else None
                known = known_status(db, message_id) if message_id else None
                if known == STATUS_IMPORTED:
                    result.skipped += 1
                    to_move.append(uid)  # schon importiert, aber noch im Eingang: aufräumen
                    continue
                if known == STATUS_NO_ATTACHMENT:
                    result.without_attachment += 1  # bleibt liegen, wird nicht erneut geladen
                    continue
                if known == STATUS_REJECTED:
                    result.rejected += 1  # bleibt liegen, steht im Protokoll des Prüfbereichs
                    continue
                if loaded >= MAX_MAILS_PER_RUN:
                    more = True
                    break
                loaded += 1
                raw = _fetch(conn, uid, "")
                if raw is None:
                    result.errors.append(f"Mail {uid.decode()} konnte nicht geladen werden.")
                    continue
                status, doc_ids, problems = import_message(db, raw, tenant_id=tenant_id, folder=inbox)
                result.errors.extend(problems)
                if status == "importiert":
                    result.imported_mails += 1
                    result.imported_docs += len(doc_ids)
                    to_move.append(uid)
                elif status == "bekannt":
                    result.skipped += 1
                    to_move.append(uid)
                elif status == STATUS_REJECTED:
                    result.rejected += 1
                else:
                    result.without_attachment += 1
            except Exception as exc:  # noqa: BLE001 – eine kaputte Mail darf den Abruf nicht stoppen
                db.rollback()
                result.errors.append(f"Fehler bei Mail {uid.decode()}: {exc}")
        if done:
            result.moved = len(_move(conn, inbox, done, to_move, result))
        result.ok = True
        result.message = _summary(result, inbox, done, bool(uids), more)
    except imap.ImapError as exc:
        result.message = str(exc)
    except Exception as exc:  # noqa: BLE001
        result.message = f"E-Mail-Abruf fehlgeschlagen: {exc}"
    finally:
        if conn is not None:
            imap.logout(conn)
        lock.release()
    _save_status(db, tenant_id, result)
    return result


def _summary(r: RunResult, inbox: str, done: str | None, had_mails: bool, more: bool) -> str:
    parts = []
    if r.imported_docs:
        parts.append(f"{r.imported_docs} Beleg(e) aus {r.imported_mails} Mail(s) in den Prüfbereich übernommen")
    if r.skipped:
        parts.append(f"{r.skipped} bereits importierte Mail(s) übersprungen")
    if r.without_attachment:
        parts.append(f"{r.without_attachment} Mail(s) ohne PDF/XML-Anhang belassen")
    if r.rejected:
        parts.append(f"{r.rejected} Mail(s) mit unbrauchbarem Anhang belassen (siehe Protokoll unten)")
    if r.moved:
        parts.append(f"{r.moved} Mail(s) nach „{done}“ verschoben")
    elif not done:
        parts.append("Mails bleiben im Eingangsordner (kein Zielordner eingestellt)")
    text = "Abruf abgeschlossen: " + (", ".join(parts) if parts else "keine neuen Mails") + "."
    if not had_mails:
        text += f" Abgerufen wird nur der Ordner „{inbox}“ – Rechnungs-Mails bitte dorthin verschieben " \
                "oder beim Mail-Anbieter eine Filterregel einrichten."
    if more:
        text += f" Es lagen mehr als {MAX_MAILS_PER_RUN} neue Mails vor – der Rest folgt beim nächsten Abruf."
    if r.errors:
        text += f" ({len(r.errors)} Hinweis(e), siehe unten)"
    return text


def _save_status(db: Session, tenant_id: int, result: RunResult) -> None:
    try:
        einstellungen.set_values(db, {STATUS_KEY: result.as_dict()})
        if result.imported_docs:
            audit.log(db, action="email_import", entity="pruefbereich", tenant_id=tenant_id,
                      changes={"belege": result.imported_docs, "mails": result.imported_mails})
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


def last_status(db: Session) -> dict | None:
    return einstellungen.get(db, STATUS_KEY)


def recent_log(db: Session, limit: int = 30) -> list[EmailImportLog]:
    """Letzte Einträge für den Prüfbereich – Mails ohne Anhang (z. B. Newsletter) werden nicht gezeigt."""
    stmt = (select(EmailImportLog).where(EmailImportLog.status != STATUS_NO_ATTACHMENT)
            .order_by(EmailImportLog.id.desc()).limit(limit))
    return list(db.scalars(stmt))


def due(db: Session, now=None) -> bool:
    """Ist für die aktive Firma ein automatischer Abruf fällig?"""
    from datetime import datetime, timedelta

    values = einstellungen.load(db)
    if not values.get("imap.aktiv"):
        return False
    interval = int(values.get("imap.intervall_minuten") or 10)
    last = values.get("imap.letzter_lauf_utc")
    now = now or utcnow()
    if last:
        try:
            if datetime.fromisoformat(last) + timedelta(minutes=interval) > now:
                return False
        except ValueError:
            pass
    einstellungen.set_values(db, {"imap.letzter_lauf_utc": now.isoformat()})
    db.commit()
    return True
