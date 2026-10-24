"""Growth equity metrics from a company's line items. Pure arithmetic: the formulas from the
Financial Analyst spec, no judgement. A metric whose inputs are missing is left out."""

from dataclasses import dataclass, field

PER_YEAR = {"month": 12, "quarter": 4, "year": 1}

SALES, GROWTH, EFFICIENCY, PROFIT, REPORTED = (
    "Sales", "Growth & Retention", "Operational Efficiency", "Profitability & Cash Flow", "회사 제공 지표")
CATEGORIES = [SALES, GROWTH, EFFICIENCY, PROFIT, REPORTED]

# How a value is shown: money (in the file's currency), pct, x (multiple), months, count.
MONEY, PCT, X, MONTHS, COUNT = "money", "pct", "x", "months", "count"


@dataclass(frozen=True, order=True)
class Period:
    key: tuple  # (year, month or quarter, 0 for a year); (9999, column) when the header couldn't be read
    gran: str  # month | quarter | year
    label: str


@dataclass
class Metric:
    category: str
    name: str
    fmt: str
    how: str  # the formula, in words
    values: dict[Period, float] = field(default_factory=dict)


class Book:
    """Line items over one granularity's periods, with the lookups the formulas need."""

    def __init__(self, series: dict[str, dict[Period, float]], periods: list[Period]):
        self.series = series
        self.periods = sorted(periods)
        self.per_year = PER_YEAR.get(self.periods[0].gran, 1) if self.periods else 1

    def get(self, item: str, p: Period | None) -> float | None:
        if p is None:
            return None
        return self.series.get(item, {}).get(p)

    def has(self, item: str) -> bool:
        return bool(self.series.get(item))

    def prev(self, p: Period) -> Period | None:
        i = self.periods.index(p)
        return self.periods[i - 1] if i > 0 else None

    def year_ago(self, p: Period) -> Period | None:
        if p.key[0] != 9999:
            want = (p.key[0] - 1, *p.key[1:])
            return next((q for q in self.periods if q.key == want), None)
        i = self.periods.index(p) - self.per_year  # headers we couldn't date: assume consecutive columns
        return self.periods[i] if i >= 0 else None


def _div(a, b):
    if a is None or b is None or b == 0:
        return None
    return a / b


def _sum(*xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) if xs else None


def compute(series: dict[str, dict[Period, float]], periods: list[Period]) -> list[Metric]:
    """Every metric the line items allow, per period. Rows with no value at all are dropped."""
    b = Book(series, periods)
    out: list[Metric] = []

    def add(category, name, fmt, how, fn):
        m = Metric(category, name, fmt, how)
        for p in b.periods:
            try:
                v = fn(p)
            except (TypeError, ZeroDivisionError):
                v = None
            if v is not None:
                m.values[p] = v
        if m.values:
            out.append(m)

    # Building blocks, each falling back to the next-best inputs the file has.
    def arr(p):
        v = b.get("arr", p)
        return v if v is not None else (b.get("mrr", p) * 12 if b.get("mrr", p) is not None else None)

    def bop(p):
        v = b.get("bop_arr", p)
        return v if v is not None else arr(b.prev(p))

    def churned(p):  # gross churned ARR: cancellations plus downgrades, as a positive number
        c, d = b.get("churned_arr", p), b.get("contraction_arr", p)
        return _sum(abs(c) if c is not None else None, abs(d) if d is not None else None)

    def gross_new(p):
        return _sum(b.get("new_logo_arr", p), b.get("expansion_arr", p))

    def net_new(p):
        return arr(p) - bop(p)

    def revenue(p):
        return b.get("revenue", p)

    def gross_profit(p):
        gp = b.get("gross_profit", p)
        if gp is None and revenue(p) is not None and b.get("cogs", p) is not None:
            gp = revenue(p) - abs(b.get("cogs", p))
        return gp

    def gross_margin(p):
        return _div(gross_profit(p), revenue(p))

    def fcf(p):
        v = b.get("fcf", p)
        if v is None and b.get("operating_cf", p) is not None and b.get("capex", p) is not None:
            v = b.get("operating_cf", p) - abs(b.get("capex", p))
        return v

    def burn(p):
        v = b.get("net_burn", p)
        if v is not None:
            return abs(v)
        f = fcf(p)
        return -f if f is not None else None

    def sm(p):
        v = b.get("sm_expense", p)
        return abs(v) if v is not None else None

    def acv(p):
        v = b.get("acv", p)
        return v if v is not None else _div(arr(p), b.get("customers", p))

    def gross_churn_rate(p):
        return _div(churned(p), bop(p))

    def yoy(p):
        return _div(arr(p), arr(b.year_ago(p))) - 1

    # --- Sales -------------------------------------------------------------------------------
    add(SALES, "ARR", MONEY, "ARR" if b.has("arr") else "MRR × 12", arr)
    add(SALES, "MRR", MONEY, "MRR", lambda p: b.get("mrr", p))
    add(SALES, "Revenue", MONEY, "Revenue", revenue)
    add(SALES, "ACV", MONEY, "ACV" if b.has("acv") else "ARR ÷ 고객 수", acv)
    add(SALES, "Customers", COUNT, "고객 수", lambda p: b.get("customers", p))
    add(SALES, "GMV", MONEY, "GMV", lambda p: b.get("gmv", p))
    add(SALES, "Take Rate", PCT, "Take rate" if b.has("take_rate") else "Revenue ÷ GMV",
        lambda p: b.get("take_rate", p) if b.has("take_rate") else _div(revenue(p), b.get("gmv", p)))
    add(SALES, "ARPU", MONEY, "ARPU", lambda p: b.get("arpu", p))

    # --- Growth & Retention ------------------------------------------------------------------
    add(GROWTH, "YoY ARR Growth", PCT, "ARR ÷ 1년 전 ARR − 1", yoy)
    add(GROWTH, "Net New ARR", MONEY, "기말 ARR − 기초 ARR", net_new)
    add(GROWTH, "Gross New ARR", MONEY, "New Logo ARR + Expansion ARR", gross_new)
    add(GROWTH, "New Logo % of Gross New ARR", PCT, "New Logo ARR ÷ Gross New ARR",
        lambda p: _div(b.get("new_logo_arr", p), gross_new(p)))
    add(GROWTH, "Expansion % of Gross New ARR", PCT, "Expansion ARR ÷ Gross New ARR",
        lambda p: _div(b.get("expansion_arr", p), gross_new(p)))
    add(GROWTH, "New Logo Velocity", PCT, "New Logo ARR ÷ 직전 기간 New Logo ARR − 1",
        lambda p: _div(b.get("new_logo_arr", p), b.get("new_logo_arr", b.prev(p))) - 1)
    add(GROWTH, "Quick Ratio", X, "Gross New ARR ÷ Gross Churned ARR (Churn + Contraction)",
        lambda p: _div(gross_new(p), churned(p)))
    add(GROWTH, "Gross Dollar Retention", PCT, "1 − Gross Churned ARR ÷ AVERAGE(기초 ARR, 기말 ARR)",
        lambda p: 1 - _div(churned(p), (bop(p) + arr(p)) / 2))

    def ndr(p):
        if arr(p) is not None and bop(p) is not None and b.get("new_logo_arr", p) is not None:
            return 1 + _div(net_new(p) - b.get("new_logo_arr", p), (bop(p) + arr(p)) / 2)
        # Second form in the spec: (Starting ARR + Expansion − Contraction − Churn) ÷ Starting ARR
        return _div(bop(p) + b.get("expansion_arr", p) - churned(p), bop(p))

    add(GROWTH, "Net Dollar Retention", PCT,
        "1 + (Net New ARR − New Logo ARR) ÷ AVERAGE(기초 ARR, 기말 ARR)", ndr)
    add(GROWTH, "DAU", COUNT, "DAU", lambda p: b.get("dau", p))
    add(GROWTH, "MAU", COUNT, "MAU", lambda p: b.get("mau", p))
    add(GROWTH, "DAU / MAU", PCT, "DAU ÷ MAU", lambda p: _div(b.get("dau", p), b.get("mau", p)))
    add(GROWTH, "Gross Churn Rate", PCT, "(Downgrade + Cancellation) ÷ 기초 ARR", gross_churn_rate)
    add(GROWTH, "Net Churn Rate", PCT, "(Downgrade + Cancellation − Expansion) ÷ 기초 ARR",
        lambda p: _div(churned(p) - b.get("expansion_arr", p), bop(p)))

    # --- Operational Efficiency --------------------------------------------------------------
    add(EFFICIENCY, "Magic Number", X, "Net New ARR ÷ S&M 비용 (같은 기간)", lambda p: _div(net_new(p), sm(p)))
    add(EFFICIENCY, "Rule of 40", PCT, "YoY ARR Growth % + FCF Margin %",
        lambda p: yoy(p) + _div(fcf(p), revenue(p)))
    cac = lambda p: _div(sm(p), b.get("new_customers", p))  # noqa: E731
    # Customer life = 1 ÷ annual churn rate; the period's churn rate is annualised.
    ltv = lambda p: acv(p) * gross_margin(p) / (gross_churn_rate(p) * b.per_year)  # noqa: E731
    add(EFFICIENCY, "LTV", MONEY, "ACV × Gross Margin ÷ 연환산 Gross Churn Rate", ltv)
    add(EFFICIENCY, "CAC", MONEY, "S&M 비용 ÷ 신규 고객 수", cac)
    add(EFFICIENCY, "LTV / CAC", X, "LTV ÷ CAC", lambda p: _div(ltv(p), cac(p)))
    add(EFFICIENCY, "CAC Payback", MONTHS, "CAC ÷ (고객당 ARR × Gross Margin), 개월",
        lambda p: _div(cac(p), acv(p) * gross_margin(p)) * 12)
    add(EFFICIENCY, "Burn Multiple", X, "Net Burn ÷ Net New ARR", lambda p: _div(burn(p), net_new(p)))
    add(EFFICIENCY, "Net Burn", MONEY, "Net burn" if b.has("net_burn") else "−FCF", burn)
    add(EFFICIENCY, "Cash", MONEY, "현금 잔액", lambda p: b.get("cash", p))
    add(EFFICIENCY, "Cash Runway", MONTHS, "현금 잔액 ÷ 기간당 Burn, 개월 (Burn > 0일 때만)",
        lambda p: _div(b.get("cash", p), burn(p)) * 12 / b.per_year if (burn(p) or 0) > 0 else None)
    for item, name in [("sm_expense", "S&M"), ("rd_expense", "R&D"), ("ga_expense", "G&A"), ("total_opex", "Total OpEx")]:
        add(EFFICIENCY, f"{name} % of Revenue", PCT, f"{name} 비용 ÷ Revenue",
            lambda p, item=item: _div(abs(b.get(item, p)), revenue(p)))

    # --- Profitability & Cash Flow -----------------------------------------------------------
    add(PROFIT, "Gross Margin", PCT, "Gross Profit ÷ Revenue", gross_margin)
    add(PROFIT, "FCF", MONEY, "FCF" if b.has("fcf") else "Operating CF − CapEx", fcf)
    add(PROFIT, "FCF Margin", PCT, "FCF ÷ Revenue", lambda p: _div(fcf(p), revenue(p)))

    # --- Metrics the company computed itself, shown as given ---------------------------------
    for item, (name, fmt) in REPORTED_ITEMS.items():
        add(REPORTED, f"{name} (회사 제공)", fmt, "엑셀에 적힌 값 그대로", lambda p, item=item: b.get(item, p))
    return out


REPORTED_ITEMS = {
    "reported_ndr": ("Net Dollar Retention", PCT),
    "reported_gdr": ("Gross Dollar Retention", PCT),
    "reported_gross_margin": ("Gross Margin", PCT),
    "reported_magic_number": ("Magic Number", X),
    "reported_burn_multiple": ("Burn Multiple", X),
    "reported_cac_payback": ("CAC Payback (개월)", MONTHS),
    "reported_ltv_cac": ("LTV / CAC", X),
    "reported_rule_of_40": ("Rule of 40", PCT),
}


def fmt(value: float, kind: str) -> str:
    if value is None:
        return ""
    if kind == MONEY:
        return f"{value / 1e6:,.2f}M" if abs(value) >= 1e5 else f"{value:,.0f}"
    if kind == PCT:
        return f"{value * 100:.1f}%"
    if kind == X:
        return f"{value:.2f}x"
    if kind == MONTHS:
        return f"{value:.1f}"
    return f"{value:,.0f}"
