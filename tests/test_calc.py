"""Kalkulation der Angebote – Tests aus dem bewährten Angebotstool übernommen und erweitert.

Platzhalter-Objekte (SimpleNamespace) statt Datenbank, damit die reine Rechenlogik geprüft wird.
"""
from decimal import Decimal as D
from types import SimpleNamespace

from app.services import calc


def pos(**kw):
    base = dict(title="Test", billing_type="stunden", quantity=None, unit_price=D("0"), hours=D("0"),
                staff_count=1, vat_rate=D("19"), recurring=False, interval=None, visits_per_month=None,
                contract_months=None, cost_rate=None)
    base.update(kw)
    return SimpleNamespace(**base)


def cost(block, quantity, unit_price, unit="", description="", vat_rate="19"):
    return SimpleNamespace(block=block, quantity=D(str(quantity)), unit_price=D(str(unit_price)), unit=unit,
                           description=description, vat_rate=D(vat_rate))


# ---------------------------------------------------------------- Rundung
def test_q2_rounds_half_up():
    assert calc.q2(D("0.005")) == D("0.01")
    assert calc.q2(D("2.345")) == D("2.35")
    assert calc.q2("10") == D("10.00")


# ---------------------------------------------------------- Einzelposition
def test_stundenbasis_mit_personal():
    r = calc.compute_position(pos(unit_price="30.00", hours="2", staff_count=2))
    assert r.net_per_visit == D("120.00")
    assert r.line_total == D("120.00")
    assert r.monthly is None


def test_pauschal():
    r = calc.compute_position(pos(billing_type="pauschal", unit_price="250.00"))
    assert r.net_per_visit == D("250.00") and r.line_total == D("250.00")


def test_menge_mal_preis_vier_nachkommastellen():
    r = calc.compute_position(pos(billing_type="menge", quantity="420", unit_price="0.125"))
    assert r.line_total == D("52.50")


def test_dauerauftrag_woechentlich():
    r = calc.compute_position(pos(recurring=True, interval="woechentlich", unit_price="25.00", hours="3",
                                  staff_count=1, contract_months=12))
    assert r.net_per_visit == D("75.00")
    assert r.visits_per_month == D("4.33")
    assert r.monthly == D("324.75")      # 75 × 4,33
    assert r.annual == D("3897.00")      # 324,75 × 12
    assert r.contract_total == D("3897.00")
    assert r.line_total == D("3897.00")


def test_dauerauftrag_taeglich():
    r = calc.compute_position(pos(recurring=True, interval="taeglich", billing_type="pauschal", unit_price="40"))
    assert r.monthly == D("840.00")  # 21 Arbeitstage


def test_dauerauftrag_override_einsaetze():
    r = calc.compute_position(pos(recurring=True, interval="individuell", billing_type="pauschal",
                                  unit_price="100.00", visits_per_month="2", contract_months=6))
    assert r.monthly == D("200.00")
    assert r.contract_total == D("1200.00")


def test_dauerauftrag_ohne_laufzeit_nutzt_monatswert():
    r = calc.compute_position(pos(recurring=True, interval="monatlich", billing_type="pauschal",
                                  unit_price="500.00"))
    assert r.monthly == D("500.00")
    assert r.contract_total is None
    assert r.line_total == D("500.00")


# --------------------------------------------------------------- Zusatzkosten
def test_cost_item_net():
    assert calc.compute_cost_item_net(cost("Anfahrt", 20, "0.30")) == D("6.00")
    assert calc.compute_cost_item_net(cost("Material", 5, "3.50")) == D("17.50")


# ------------------------------------------------------------ Gesamtkalkulation
def test_compute_quote_full():
    positions = [
        pos(unit_price="30.00", hours="2", staff_count=2),           # 120,00
        pos(billing_type="pauschal", unit_price="80.00"),            # 80,00
    ]
    cost_items = [cost("Anfahrt", 1, "25.00"), cost("Entsorgung", 1, "80.00"), cost("Material", 5, "3.50")]
    r = calc.compute_quote(positions, cost_items)
    assert r.block_positions == D("200.00")
    assert [(b.label, b.net) for b in r.cost_blocks] == [
        ("Anfahrt", D("25.00")), ("Entsorgung", D("80.00")), ("Material", D("17.50"))]
    assert r.net_total == D("322.50")
    assert r.vat_total == D("61.28")   # 322,50 × 19 % = 61,275 -> 61,28
    assert r.gross_total == D("383.78")


def test_frei_benannte_bloecke_werden_zusammengefasst():
    r = calc.compute_quote([], [cost("Gerüst", 1, "100"), cost("Gerüst", 2, "50"), cost("Anfahrt", 1, "10")])
    assert [(b.label, b.net, len(b.items)) for b in r.cost_blocks] == [("Gerüst", D("200.00"), 2),
                                                                       ("Anfahrt", D("10.00"), 1)]


def test_mehrere_steuersaetze():
    r = calc.compute_quote([pos(billing_type="pauschal", unit_price="100"),
                            pos(billing_type="pauschal", unit_price="50", vat_rate=D("7"))], [])
    assert [(t.rate, t.net, t.vat) for t in r.tax_lines] == [(D("19"), D("100.00"), D("19.00")),
                                                              (D("7"), D("50.00"), D("3.50"))]
    assert r.gross_total == D("172.50")


def test_compute_quote_margin():
    r = calc.compute_quote([pos(billing_type="pauschal", unit_price="1000.00")], [], extra_internal_cost="600.00")
    assert r.net_total == D("1000.00")
    assert r.margin_abs == D("400.00")
    assert r.margin_pct == D("40.00")


def test_marge_aus_kostensaetzen():
    positions = [pos(unit_price="30", hours="2", staff_count=2, cost_rate="18"),             # Kosten 72
                 pos(recurring=True, interval="monatlich", billing_type="pauschal", unit_price="500",
                     cost_rate="300", contract_months=12)]                                    # Kosten 3.600
    r = calc.compute_quote(positions, [])
    assert r.internal_cost == D("3672.00")
    assert r.net_total == D("6120.00")
    assert r.margin_abs == D("2448.00") and r.margin_pct == D("40.00")
    assert r.monthly_net == D("500.00")


def test_ohne_kostendaten_keine_marge():
    r = calc.compute_quote([pos(billing_type="pauschal", unit_price="100")], [])
    assert r.internal_cost is None and r.margin_abs is None


def test_compute_quote_zero_tax():
    r = calc.compute_quote([pos(billing_type="pauschal", unit_price="100.00", vat_rate=D("0"))], [])
    assert r.vat_total == D("0.00")
    assert r.gross_total == D("100.00")


def test_tax_rounding_edge():
    # 52,63 × 19 % = 9,9997 -> 10,00
    r = calc.compute_quote([pos(billing_type="pauschal", unit_price="52.63")], [])
    assert r.vat_total == D("10.00")
    assert r.gross_total == D("62.63")
