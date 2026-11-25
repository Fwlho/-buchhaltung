"""IMAP-Grundlagen: Verbindung (TLS mit Zertifikatsprüfung), Ordner finden/anlegen/abonnieren,
Ordnernamen in IMAP-UTF-7, Fähigkeiten des Servers.

Bewährte Regeln aus dem alten Programm (KONZEPT.md 6.4):
- Ordner-Auflösung tolerant: 'Rechnungen' findet auch 'INBOX.Rechnungen' bzw. 'INBOX/Rechnungen',
  Groß-/Kleinschreibung egal, eingefügte imap://-Adressen werden verstanden.
- Neue Ordner mit dem echten Trennzeichen des Servers anlegen UND abonnieren.
- Ordnernamen werden bereinigt (keine Steuerzeichen -> keine eingeschleusten IMAP-Befehle).
"""
import base64
import imaplib
import re
import ssl
import urllib.parse
from dataclasses import dataclass

from sqlalchemy.orm import Session

TIMEOUT = 30


@dataclass(frozen=True)
class ImapConfig:
    host: str
    port: int
    use_ssl: bool
    user: str
    password: str
    folder_inbox: str
    folder_done: str
    folder_drafts: str


class ImapError(RuntimeError):
    pass


def config_for(db: Session) -> ImapConfig | None:
    """IMAP-Zugang der aktiven Firma aus den Einstellungen (Passwort entschlüsselt)."""
    from app.services import einstellungen

    values = einstellungen.load(db)
    if not (values.get("imap.host") and values.get("imap.benutzer") and values.get("imap.passwort")):
        return None
    return ImapConfig(
        host=values["imap.host"], port=int(values.get("imap.port") or 993), use_ssl=bool(values.get("imap.ssl")),
        user=values["imap.benutzer"], password=einstellungen.get_secret(db, "imap.passwort"),
        folder_inbox=values.get("imap.ordner_eingang") or "Rechnungen",
        folder_done=values.get("imap.ordner_nach_import") or "",
        folder_drafts=values.get("imap.ordner_entwuerfe") or "",
    )


def connect(cfg: ImapConfig):
    """Verbindung mit Zertifikatsprüfung. In Tests wird diese Funktion ersetzt."""
    context = ssl.create_default_context()
    try:
        if cfg.use_ssl:
            conn = imaplib.IMAP4_SSL(cfg.host, cfg.port, ssl_context=context, timeout=TIMEOUT)
        else:
            conn = imaplib.IMAP4(cfg.host, cfg.port, timeout=TIMEOUT)
            conn.starttls(ssl_context=context)
        conn.login(cfg.user, cfg.password)
    except (OSError, imaplib.IMAP4.error) as exc:
        raise ImapError(f"Anmeldung am Mailserver fehlgeschlagen: {exc}") from exc
    return conn


def logout(conn) -> None:
    try:
        conn.logout()
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------- IMAP-UTF-7 (RFC 3501)
def utf7_encode(text: str) -> str:
    """Ordnername -> IMAP-UTF-7, z. B. 'Entwürfe' -> 'Entw&APw-rfe'."""
    out, buf = [], []

    def flush():
        if buf:
            raw = "".join(buf).encode("utf-16-be")
            out.append("&" + base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",") + "-")
            buf.clear()

    for ch in text:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def utf7_decode(text: str) -> str:
    def repl(m: re.Match) -> str:
        chunk = m.group(1)
        if chunk == "":
            return "&"
        data = chunk.replace(",", "/")
        data += "=" * (-len(data) % 4)
        return base64.b64decode(data).decode("utf-16-be")

    return re.sub(r"&([^-]*)-", repl, text)


# ------------------------------------------------------------------ Ordner
def clean_folder(name: str) -> str:
    """Steuerzeichen entfernen – verhindert eingeschleuste IMAP-Befehle."""
    return "".join(ch for ch in (name or "") if ch.isprintable()).strip()


def normalize_setting(folder: str) -> str:
    """Erlaubt auch eingefügte Adressen wie 'imap://info%40firma.de@imap.example.de/INBOX/Eingang'."""
    folder = clean_folder(folder)
    if folder.lower().startswith(("imap://", "imaps://")):
        _, _, path = folder.split("://", 1)[1].partition("/")
        folder = clean_folder(urllib.parse.unquote(path))
    return folder.strip("/").strip()


def quote(folder: str) -> str:
    """Ordnername (Klartext) -> kodiert und in Anführungszeichen für IMAP-Befehle."""
    encoded = utf7_encode(clean_folder(folder))
    return '"' + encoded.replace("\\", "\\\\").replace('"', '\\"') + '"'


_LIST_LINE = re.compile(
    rb'\((?P<flags>[^)]*)\)\s+(?:"(?P<delim>[^"]*)"|NIL)\s+(?:"(?P<qname>(?:[^"\\]|\\.)*)"|(?P<name>\S+))'
)


@dataclass
class FolderInfo:
    names: list[str]            # Klartext (dekodiert)
    delimiter: str
    flags: dict[str, set[str]]  # Name -> Flags, z. B. {"\\Drafts"}


def list_folders(conn) -> FolderInfo:
    info = FolderInfo(names=[], delimiter="", flags={})
    typ, data = conn.list()
    if typ != "OK" or not data:
        return info
    for line in data:
        if not isinstance(line, bytes):
            continue
        m = _LIST_LINE.match(line)
        if not m:
            continue
        if not info.delimiter and m.group("delim"):
            info.delimiter = m.group("delim").decode("ascii", errors="replace")
        raw = (m.group("qname") or m.group("name") or b"").replace(b'\\"', b'"').replace(b"\\\\", b"\\")
        try:
            name = utf7_decode(raw.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            name = raw.decode("utf-8", errors="replace")
        info.names.append(name)
        info.flags[name] = {f.lower() for f in m.group("flags").decode("ascii", errors="replace").split()}
    return info


def resolve(wanted: str, folders: list[str]) -> str | None:
    """Findet den echten Server-Namen eines Ordners (siehe Moduldoku)."""
    wanted = normalize_setting(wanted)
    if not wanted:
        return None
    if wanted in folders:
        return wanted
    low = wanted.lower()
    variants = {low, low.replace("/", "."), low.replace(".", "/")}
    candidates = [c for v in variants for c in (v, f"inbox.{v}", f"inbox/{v}")]
    for candidate in candidates:
        for folder in folders:
            if folder.lower() == candidate:
                return folder
    for v in variants:
        for folder in folders:
            if folder.lower().endswith((f".{v}", f"/{v}")):
                return folder
    return None


def ensure_folder(conn, folder: str) -> None:
    """Anlegen (falls nötig) und abonnieren – sonst wirken Mails im Webmail „verschwunden“."""
    for command in (conn.create, conn.subscribe):
        try:
            command(quote(folder))
        except imaplib.IMAP4.error:
            pass


def namespaced(name: str, info: FolderInfo) -> str:
    """Viele Server (z. B. Dovecot mit Präfix, Courier) führen alle Ordner unter 'INBOX.'.
    Liegen alle vorhandenen Ordner dort, wird der neue Ordner ebenfalls dort angelegt."""
    sep = info.delimiter or "."
    others = [n for n in info.names if n.upper() != "INBOX"]
    if others and all(n.upper().startswith("INBOX" + sep) for n in others):
        return f"INBOX{sep}{name.replace('/', sep)}"
    return name.replace("/", sep)


def sibling_name(existing: str, new: str, delimiter: str) -> str:
    """Neuer Ordner neben einem bestehenden, im Namensraum des Servers (z. B. 'INBOX.Verarbeitet')."""
    sep = delimiter or "."
    if sep in existing:
        return existing.rsplit(sep, 1)[0] + sep + new
    return new


def supports(conn, capability: str) -> bool:
    caps = getattr(conn, "capabilities", ()) or ()
    return capability.upper() in {(c.decode() if isinstance(c, bytes) else str(c)).upper() for c in caps}


def count_messages(conn, folder: str) -> int | None:
    """Anzahl Mails in einem Ordner (nur lesend). None = nicht möglich."""
    try:
        typ, data = conn.select(quote(folder), readonly=True)
        if typ != "OK" or not data or not data[0]:
            return None
        return int(data[0])
    except Exception:  # noqa: BLE001
        return None


DRAFT_NAMES = ("Drafts", "Entwürfe", "Entwurf", "Draft")


def drafts_folder(conn, configured: str = "") -> str | None:
    """Entwürfe-Ordner: Einstellung, sonst Server-Kennzeichnung \\Drafts, sonst übliche Namen."""
    info = list_folders(conn)
    if configured:
        return resolve(configured, info.names)
    for name, flags in info.flags.items():
        if "\\drafts" in flags:
            return name
    for candidate in DRAFT_NAMES:
        found = resolve(candidate, info.names)
        if found:
            return found
    return None
