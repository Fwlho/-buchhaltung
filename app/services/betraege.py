"""Steuer- und Betragsberechnung für Belege (nur Decimal, kaufmännisch gerundet).

- Fehlende Werte einer Steuerzeile werden aus den vorhandenen berechnet
  (z. B. Brutto + Satz -> Netto und USt).
- Skonto wird anteilig auf die Steuersätze verteilt (Vorsteuer-Korrektur).
"""
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from app.formatting import format_money, round_money

ZERO = Decimal("0.00")
HUNDRED = Decimal(100)
# Erlaubte Abweichung der USt vom Satz (Rundung auf Rechnungen, z. B. Summe statt Zeilen)
VAT_TOLERANCE = Decimal("0.05")


@dataclass
class TaxLine:
    rate: Decimal
    net: Decimal
    vat: Decimal
    gross: Decimal


class AmountError(ValueError):
    pass


def complete_line(rate: Decimal, net: Decimal | None, vat: Decimal | None, gross: Decimal | None) -> TaxLine:
    """Ergänzt fehlende Beträge einer Steuerzeile. Mindestens ein Betrag muss angegeben sein."""
    if rate < 0 or rate >= HUNDRED:
        raise AmountError("Ungültiger Steuersatz.")
    factor = rate / HUNDRED
    if net is None and gross is None and vat is None:
        raise AmountError("Bitte mindestens Netto oder Brutto angeben.")

    if net is not None:
        if vat is None:
            vat = round_money(gross - net) if gross is not None else round_money(net * factor)
        if gross is None:
            gross = net + vat
    elif gross is not None:
        if vat is None:
            net = round_money(gross / (1 + factor))
            vat = gross - net
        else:
            net = gross - vat
    else:  # nur USt bekannt
        if rate == 0:
            raise AmountError("Bei 0 % bitte Netto oder Brutto angeben.")
        net = round_money(vat / factor)
        gross = net + vat

    net, vat, gross = round_money(net), round_money(vat), round_money(gross)
    if net + vat != gross:
        raise AmountError(f"Netto + USt ({format_money(net + vat)}) ergibt nicht Brutto ({format_money(gross)}).")
    expected = round_money(net * factor)
    tolerance = max(VAT_TOLERANCE, round_money(abs(net) * Decimal("0.001")))
    if abs(vat - expected) > tolerance:
        raise AmountError(
            f"Die USt ({format_money(vat)}) passt nicht zum Satz {rate.normalize():f} % "
            f"(erwartet ca. {format_money(expected)})."
        )
    return TaxLine(rate=rate, net=net, vat=vat, gross=gross)


def totals(lines: Iterable[TaxLine]) -> tuple[Decimal, Decimal, Decimal]:
    net = vat = gross = ZERO
    for line in lines:
        net += line.net
        vat += line.vat
        gross += line.gross
    return net, vat, gross


def position_net(quantity: Decimal, unit_price: Decimal, discount_percent: Decimal | None = None) -> Decimal:
    """Netto einer Rechnungsposition: Menge × Einzelpreis abzüglich Rabatt, kaufmännisch gerundet."""
    net = quantity * unit_price
    if discount_percent:
        net = net * (HUNDRED - discount_percent) / HUNDRED
    return round_money(net)


def group_by_rate(nets: Iterable[tuple[Decimal, Decimal]]) -> list[TaxLine]:
    """(Satz, Netto) je Position -> eine Steuerzeile je Satz. Die USt wird je Satz auf die
    Netto-Summe berechnet (übliches Verfahren, vermeidet Rundungsdifferenzen)."""
    sums: dict[Decimal, Decimal] = {}
    for rate, net in nets:
        sums[rate] = sums.get(rate, ZERO) + net
    result = []
    for rate in sorted(sums, reverse=True):
        net = round_money(sums[rate])
        vat = round_money(net * rate / HUNDRED)
        result.append(TaxLine(rate=rate, net=net, vat=vat, gross=net + vat))
    return result


def skonto_amount(gross: Decimal, percent: Decimal) -> Decimal:
    return round_money(gross * percent / HUNDRED)


def split_by_rate(amount: Decimal, lines: list[TaxLine]) -> list[TaxLine]:
    """Verteilt einen Bruttobetrag (z. B. Skonto) anteilig auf die Steuersätze.
    Rundungsrest landet in der größten Zeile, damit die Summe exakt stimmt."""
    _, _, gross_total = totals(lines)
    if not lines or gross_total == 0:
        return []
    parts: list[TaxLine] = []
    remaining = amount
    biggest = max(range(len(lines)), key=lambda i: abs(lines[i].gross))
    for i, line in enumerate(lines):
        share = round_money(amount * line.gross / gross_total) if i != biggest else None
        parts.append(TaxLine(line.rate, ZERO, ZERO, share if share is not None else ZERO))
        if share is not None:
            remaining -= share
    parts[biggest].gross = remaining
    for part in parts:
        part.net = round_money(part.gross / (1 + part.rate / HUNDRED))
        part.vat = part.gross - part.net
    return parts
