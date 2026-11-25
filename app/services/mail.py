"""System-Mails (Login-Code, Einladung) – NIE Mails an Kunden.

Versandweg:
1. Hat die Firma einen eigenen SMTP-Server eingetragen, wird dieser benutzt.
2. Sonst der SMTP-Server der Installation aus der .env.
3. Lokal ohne SMTP: Die Mail wird als .eml-Datei unter data/mail_ausgang/ abgelegt
   und der Inhalt im Konsolenfenster angezeigt (praktisch für die Testversion).
"""
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import tenant_scope, utcnow


class MailError(RuntimeError):
    pass


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
    sender: str
    use_ssl: bool


def platform_smtp() -> SmtpConfig | None:
    s = get_settings()
    if not s.smtp_host:
        return None
    return SmtpConfig(s.smtp_host, s.smtp_port, s.smtp_user, s.smtp_password, s.smtp_from or s.smtp_user, s.smtp_ssl)


def tenant_smtp(db: Session, tenant_id: int) -> SmtpConfig | None:
    from app.services import einstellungen

    with tenant_scope(db, tenant_id):
        values = einstellungen.load(db)
        if not values.get("smtp.host"):
            return None
        password = einstellungen.get_secret(db, "smtp.passwort")
    return SmtpConfig(
        values["smtp.host"], int(values.get("smtp.port") or 465), values.get("smtp.benutzer") or "",
        password, values.get("smtp.absender") or values.get("smtp.benutzer") or "", bool(values.get("smtp.ssl")),
    )


def build_message(to: str, subject: str, text: str, html: str | None, sender: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def _send_smtp(cfg: SmtpConfig, msg: EmailMessage) -> None:
    context = ssl.create_default_context()  # Zertifikat wird geprüft
    try:
        if cfg.use_ssl:
            with smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=20, context=context) as server:
                server.login(cfg.user, cfg.password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=20) as server:
                server.starttls(context=context)
                server.login(cfg.user, cfg.password)
                server.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        raise MailError(f"E-Mail-Versand fehlgeschlagen: {exc}") from exc


def _write_outbox(msg: EmailMessage, text: str) -> Path:
    outbox = get_settings().mail_outbox_dir
    outbox.mkdir(parents=True, exist_ok=True)
    path = outbox / f"{utcnow():%Y%m%d-%H%M%S-%f}.eml"
    path.write_bytes(bytes(msg))
    print("\n" + "=" * 70)
    print(f"E-MAIL (lokal, nicht versendet) an {msg['To']}: {msg['Subject']}")
    print("-" * 70)
    print(text)
    print(f"(gespeichert unter {path})")
    print("=" * 70 + "\n", flush=True)
    return path


def send_system_mail(
    db: Session, *, to: str, subject: str, text: str, html: str | None = None, tenant_id: int | None = None
) -> str:
    """Verschickt eine System-Mail. Gibt zurück, wie sie zugestellt wurde ("smtp" oder "datei")."""
    cfg = tenant_smtp(db, tenant_id) if tenant_id else None
    cfg = cfg or platform_smtp()
    if cfg:
        _send_smtp(cfg, build_message(to, subject, text, html, cfg.sender))
        return "smtp"
    if get_settings().is_local:
        _write_outbox(build_message(to, subject, text, html, "buchhaltung@localhost"), text)
        return "datei"
    raise MailError("Kein Mailserver eingerichtet (SMTP_HOST in der .env oder SMTP in den Firmen-Einstellungen).")
