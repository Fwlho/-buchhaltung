"""Prüfungen für Eingaben: IBAN, BIC, USt-IdNr., E-Mail, Farbe."""
import re


def normalize_iban(text: str) -> str:
    return re.sub(r"\s+", "", text or "").upper()


def iban_valid(text: str) -> bool:
    """Prüfziffer nach ISO 13616 (Modulo 97)."""
    iban = normalize_iban(text)
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", iban):
        return False
    if iban.startswith("DE") and len(iban) != 22:
        return False
    rearranged = iban[4:] + iban[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(digits) % 97 == 1


def format_iban(text: str) -> str:
    """'DE89370400440532013000' -> 'DE89 3704 0044 0532 0130 00'"""
    iban = normalize_iban(text)
    return " ".join(iban[i:i + 4] for i in range(0, len(iban), 4))


def bic_valid(text: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?", (text or "").strip().upper()))


def ustid_valid(text: str) -> bool:
    """Deutsche USt-IdNr.: DE + 9 Ziffern. Andere EU-Länder: Ländercode + 2–12 Zeichen."""
    value = re.sub(r"\s+", "", text or "").upper()
    if value.startswith("DE"):
        return bool(re.fullmatch(r"DE\d{9}", value))
    return bool(re.fullmatch(r"[A-Z]{2}[A-Z0-9+*]{2,12}", value))


def leitweg_valid(text: str) -> bool:
    """Leitweg-ID (E-Rechnung an Behörden): Grobadressierung (2–12 Ziffern),
    optional Feinadressierung (bis 30 Zeichen), Prüfziffer (2 Ziffern), mit Bindestrichen.
    Die Prüfziffer selbst wird hier (noch) nicht nachgerechnet."""
    return bool(re.fullmatch(r"\d{2,12}(-[A-Za-z0-9]{1,30})?-\d{2}", (text or "").strip()))


def email_valid(text: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", (text or "").strip()))


def color_valid(text: str) -> bool:
    return bool(re.fullmatch(r"#[0-9a-fA-F]{6}", (text or "").strip()))
