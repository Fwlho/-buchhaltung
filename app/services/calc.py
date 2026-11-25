"""Angebots-Kalkulation – reine Funktionen mit Decimal, ohne Datenbank (siehe tests/test_calc.py).

Übernommen und verallgemeinert aus dem bewährten Angebotstool.

Rundung (kaufmännisch, ROUND_HALF_UP):
1. Jeder Einsatz-/Zeilenwert wird auf 2 Nachkommastellen gerundet.
2. Blöcke und Netto-Summe sind Summen dieser gerundeten Werte.
3. Die Umsatzsteuer wird je Steuersatz EINMAL auf die Netto-Summe berechnet.

Abrechnungsarten je Position:
- ``menge``     Menge × Einzelpreis
- ``stunden``   Stunden × Stundensatz × Personen
- ``pauschal``  Pauschalpreis

Wiederkehrende Leistungen (Dauerauftrag):
- Einsätze/Monat aus dem Intervall (überschreibbar): täglich = 21 (Arbeitstage),
  wöchentlich = 4,33 (52 Wochen / 12 Monate), monatlich = 1, individuell = Angabe.
- monatlich = Wert je Einsatz × Einsätze/Monat; jährlich = monatlich × 12;
  Vertragswert = monatlich × Laufzeit (Monate).
- In die Angebotssumme fließt der Vertragswert ein, ohne Laufzeit ersatzweise der Monatswert.

Interne Kalkulation (nie im PDF): Kostensatz je Einheit/Stunde/Pauschale, genauso
hochgerechnet wie der Preis; Marge = Netto − interne Kosten.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from app.services.betraege import TaxLine, group_by_rate, totals

TWO_PLACES = Decimal("0.01")
ZERO = Decimal("0")

INTERVALS: dict[str, tuple[str, Decimal]] = {
    "taeglich": ("täglich (Mo–Fr)", Decimal("21")),
    "woechentlich": ("wöchentlich", Decimal("4.33")),
    "monatlich": ("monatlich", Decimal("1")),
    "individuell": ("individuell", Decimal("1")),
}
BILLING_TYPES = ("menge", "stunden", "pauschal")


def q2(value) -> Decimal:
    """Auf 2 Nachkommastellen kaufmännisch runden."""
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _d(value) -> Decimal:
    if value is None or value == "":
        return ZERO
    return value if isinstance(value, Decimal) else Decimal(str(value))


@dataclass
class PositionResult:
    title: str
    billing_type: str
    vat_rate: Decimal
    net_per_visit: Decimal          # Preis für einen Einsatz bzw. die einmalige Leistung
    line_total: Decimal             # fließt in die Angebotssumme ein
    recurring: bool = False
    visits_per_month: Decimal | None = None
    monthly: Decimal | None = None
    annual: Decimal | None = None
    contract_total: Decimal | None = None
    contract_months: int | None = None
    internal_cost: Decimal | None = None


@dataclass
class CostBlock:
    label: str
    net: Decimal
    items: list[dict] = field(default_factory=list)


@dataclass
class QuoteResult:
    positions: list[PositionResult]
    block_positions: Decimal
    cost_blocks: list[CostBlock]
    net_total: Decimal
    tax_lines: list[TaxLine]
    vat_total: Decimal
    gross_total: Decimal
    monthly_net: Decimal            # Summe der wiederkehrenden Positionen je Monat
    internal_cost: Decimal | None = None
    margin_abs: Decimal | None = None
    margin_pct: Decimal | None = None


# --------------------------------------------------------------------- Position
def visits_per_month(interval: str | None, override=None) -> Decimal:
    """Einsätze pro Monat: eigener Wert hat Vorrang, sonst Standard des Intervalls."""
    if override is not None and override != "" and _d(override) > 0:
        return _d(override)
    return INTERVALS.get(interval or "", ("", Decimal("1")))[1]


def units_per_visit(billing_type: str, quantity, hours, staff_count) -> Decimal:
    """Menge, auf die sich Preis und Kostensatz beziehen (Stück/m² oder Personenstunden)."""
    if billing_type == "pauschal":
        return Decimal("1")
    if billing_type == "stunden":
        staff = _d(staff_count) if staff_count else Decimal("1")
        return _d(hours) * staff
    return _d(quantity)


def net_per_visit(billing_type: str, quantity, unit_price, hours=None, staff_count=None) -> Decimal:
    return q2(units_per_visit(billing_type, quantity, hours, staff_count) * _d(unit_price))


def compute_position(p) -> PositionResult:
    """Berechnet eine Position (ORM-Objekt oder SimpleNamespace mit gleichen Feldern)."""
    billing = p.billing_type
    per_visit = net_per_visit(billing, p.quantity, p.unit_price, p.hours, p.staff_count)
    cost_rate = getattr(p, "cost_rate", None)
    cost_per_visit = (q2(units_per_visit(billing, p.quantity, p.hours, p.staff_count) * _d(cost_rate))
                      if cost_rate is not None and cost_rate != "" else None)
    rate = _d(p.vat_rate)

    if getattr(p, "recurring", False):
        vpm = visits_per_month(p.interval, getattr(p, "visits_per_month", None))
        monthly = q2(per_visit * vpm)
        months = getattr(p, "contract_months", None)
        contract_total = q2(monthly * _d(months)) if months else None
        cost = None
        if cost_per_visit is not None:
            cost_month = q2(cost_per_visit * vpm)
            cost = q2(cost_month * _d(months)) if months else cost_month
        return PositionResult(
            title=p.title or "", billing_type=billing, vat_rate=rate, net_per_visit=per_visit,
            line_total=contract_total if contract_total is not None else monthly, recurring=True,
            visits_per_month=vpm, monthly=monthly, annual=q2(monthly * 12), contract_total=contract_total,
            contract_months=months, internal_cost=cost)
    return PositionResult(title=p.title or "", billing_type=billing, vat_rate=rate, net_per_visit=per_visit,
                          line_total=per_visit, internal_cost=cost_per_visit)


def compute_cost_item_net(item) -> Decimal:
    """Zusatzkosten: Menge × Einzelpreis."""
    return q2(_d(item.quantity) * _d(item.unit_price))


# --------------------------------------------------------------------- Angebot
def compute_quote(positions: Iterable, cost_items: Iterable, extra_internal_cost=None) -> QuoteResult:
    pos_results = [compute_position(p) for p in positions]
    block_positions = q2(sum((r.line_total for r in pos_results), ZERO))

    blocks: dict[str, CostBlock] = {}
    nets: list[tuple[Decimal, Decimal]] = [(r.vat_rate, r.line_total) for r in pos_results]
    for item in cost_items:
        net = compute_cost_item_net(item)
        label = (item.block or "Zusatzkosten").strip()
        block = blocks.setdefault(label, CostBlock(label=label, net=ZERO))
        block.net = q2(block.net + net)
        block.items.append({"description": item.description or "", "quantity": _d(item.quantity),
                            "unit": item.unit or "", "unit_price": _d(item.unit_price), "net": net})
        nets.append((_d(item.vat_rate), net))

    tax_lines = group_by_rate(nets)
    net_total, vat_total, gross_total = totals(tax_lines)
    net_total = q2(net_total)

    known = [r.internal_cost for r in pos_results if r.internal_cost is not None]
    internal = margin_abs = margin_pct = None
    if known or (extra_internal_cost not in (None, "")):
        internal = q2(sum(known, ZERO) + _d(extra_internal_cost))
        margin_abs = q2(net_total - internal)
        if net_total > 0:
            margin_pct = q2(margin_abs / net_total * 100)

    return QuoteResult(
        positions=pos_results, block_positions=block_positions, cost_blocks=list(blocks.values()),
        net_total=net_total, tax_lines=tax_lines, vat_total=q2(vat_total), gross_total=q2(gross_total),
        monthly_net=q2(sum((r.monthly for r in pos_results if r.monthly is not None), ZERO)),
        internal_cost=internal, margin_abs=margin_abs, margin_pct=margin_pct,
    )
