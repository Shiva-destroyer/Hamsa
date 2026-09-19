"""Unit tests for the NSQ ingestion guard / validator / dedupe (synthetic text fixture only, no real PDFs, no network, no DB)."""
from datetime import date, datetime, timedelta, timezone

import pytest

from ingest import cdsco, parse, scheduler

# Page text as pdfplumber would flatten a wrapped table: columns of one row interleave line by line. Entirely made up.
PAGE = """NOT OF STANDARD QUALITY (NSQ) ALERT FOR THE MONTH OF JUNE-2025 (CDSCO/Central Laboratories)
S.No Product/Drug Name Batch No. Manufacturing Expiry Manufactured By NSQ Result Reported by
1. Testrol Tablets IP TS-1234 01/2025 12/2026 M/s. Exampleco Pharma Pvt. Dissolution CDL, Kolkata
500 mg Ltd., Plot 9, Testville
"""


def row(**kw):
    base = {"product_name": "Testrol Tablets IP 500 mg", "batch_number": "TS-1234", "manufacturer": "Exampleco Pharma Pvt. Ltd.",
            "alert_date": date(2025, 6, 30), "reason": "Dissolution", "source_document": "fixture.pdf", "lab_type": "central",
            "mfg_date": "01/2025", "expiry_date": "12/2026", "page": 1}
    base.update(kw)
    return base


# ---- page-text guard ------------------------------------------------------------------------
def test_guard_accepts_row_whose_fields_are_in_page_text():
    assert parse.guard(row(), PAGE) == []


def test_guard_rejects_batch_absent_from_page():
    assert parse.guard(row(batch_number="TS-9999"), PAGE) == ["guard: batch not found in page text"]


def test_guard_rejects_product_and_manufacturer_absent_from_page():
    why = parse.guard(row(product_name="Invented Syrup", manufacturer="Nonexistent Labs Ltd."), PAGE)
    assert "guard: product not found in page text near batch" in why
    assert "guard: manufacturer not found in page text near batch" in why


def test_guard_ignores_case_and_punctuation():
    assert parse.guard(row(manufacturer="EXAMPLECO PHARMA PVT LTD", batch_number="ts 1234"), PAGE) == []


def test_guard_field_from_another_row_is_not_enough():
    far = PAGE + "\n".join(f"filler{i} words here" for i in range(200)) + "\n2. Otherdrug Zz-7777 M/s. Farco Pvt. Ltd.\n"
    assert parse.guard(row(product_name="Otherdrug", batch_number="TS-1234", manufacturer="Farco Pvt. Ltd."), far) != []


# ---- validator ------------------------------------------------------------------------------
def test_check_row_accepts_good_row():
    assert parse.check_row(row(), PAGE) == []


@pytest.mark.parametrize("field,value,fragment", [
    ("mfg_date", "Not Mentioned", "manufacturing date does not parse"),
    ("expiry_date", "NIL", "expiry date does not parse"),
    ("expiry_date", "10/2024", "expiry date precedes manufacturing date"),
    ("lab_type", None, "lab_type undeterminable"),
    ("alert_date", None, "alert date does not parse"),
    ("batch_number", "Not Mentioned", "batch number not stated"),
    ("batch_number", "AB1, AB2", "multiple"),
    ("reason", "", "missing reason"),
    ("manufacturer", "Mkt by: Somebody Pvt. Ltd.", "several parties"),
])
def test_check_row_rejects(field, value, fragment):
    why = parse.check_row(row(**{field: value}), PAGE)
    assert any(fragment in w for w in why), why


def test_parse_month_year_formats():
    assert parse.parse_month_year("08/2024") == date(2024, 8, 1)
    assert parse.parse_month_year("Apr-24") == date(2024, 4, 1)
    assert parse.parse_month_year("S e p - 2 4") == date(2024, 9, 1)
    assert parse.parse_month_year("12-09-2024") == date(2024, 9, 1)
    assert parse.parse_month_year("NM") is None and parse.parse_month_year("13/2024") is None


def test_document_section_and_month_detection():
    assert parse.detect_lab_type(PAGE, "x.pdf") == "central"
    assert parse.detect_lab_type("B. NOT OF STANDARD QUALITY ALERT FOR THE MONTH OF MAY- 2025 (State Laboratories)", "x.pdf") == "state"
    assert parse.detect_lab_type("no section text", "stnsqapr25.pdf") == "state"      # filename fallback
    assert parse.detect_lab_type("no section text", "unknown.pdf") is None
    assert parse.detect_alert_month(PAGE) == (2025, 6)
    assert parse.month_end(2025, 2) == date(2025, 2, 28)


def test_manufacturer_name_strips_address():
    assert parse.manufacturer_name("M/s. Athens Life Sciences,\nMauza Rampur") == "Athens Life Sciences"
    assert parse.manufacturer_name("M/s. Martin & Brown Bio-\nsciences Pvt. Ltd. K.no. 918/419, Nalagarh") == "Martin & Brown Bio- sciences Pvt. Ltd."
    assert parse.manufacturer_name("M/s. Alzan Pharmaceuticals Pvt., Ltd., Baddi") == "Alzan Pharmaceuticals Pvt., Ltd."


# ---- dedupe ---------------------------------------------------------------------------------
def test_dedupe_on_batch_manufacturer_alert_date():
    a, b = row(), row(batch_number="ts-1234", manufacturer="EXAMPLECO PHARMA PVT LTD", product_name="other name")
    c, d = row(alert_date=date(2025, 5, 31)), row(batch_number="TS-1235")
    kept, dups = parse.dedupe([a, b, c, d])
    assert kept == [a, c, d] and dups == [b]


# ---- cdsco scraper (no network) -------------------------------------------------------------
LISTING = """<ul>
<li><a href='/x/download_file_division.jsp?num_id=AAA='><span class="font_black"> CDSCO NSQ ALERT FOR THE MONTH OF June 2025</span><i class="fa"></i> 2025-Jul-18</a></li>
<li><a href='/x/download_file_division.jsp?num_id=BBB='><span class="font_black"> STATE NSQ ALERT FOR THE MONTH OF June 2025</span><i class="fa"></i> 2025-Jul-18</a></li>
<li><a href='/x/download_file_division.jsp?num_id=CCC='><span class="font_black"> NSQ ALERT FOR THE MONTH OF MAY-2025</span><i class="fa"></i> 2025-Jun-20</a></li>
<li><a href='/x/download_file_division.jsp?num_id=DDD='><span class="font_black"> List of spurious Drugs for the month of June-2025</span><i class="fa"></i> 2025-Jul-18</a></li>
<li><a href='/x/download_file_division.jsp?num_id=EEE='><span class="font_black"> NSQ ALERT FOR THE MONTH OF Feb-2025</span><i class="fa"></i> 2025-Mar-29</a></li>
</ul>"""


def test_listing_selects_latest_months_and_kinds():
    items = cdsco.parse_listing(LISTING)
    assert [i.kind for i in items] == ["central", "state", "central", "spurious", "central"]
    sel = cdsco.select_latest(items, months=2)
    assert {i.href[-4:] for i in sel} == {"AAA=", "BBB=", "CCC="}          # Feb dropped; spurious excluded by default


class _Resp:
    def __init__(self, code=200, text=""):
        self.status_code, self.text = code, text

    def raise_for_status(self):
        pass


class _Session:
    def __init__(self, robots_code=404, robots_text=""):
        self.headers, self.calls, self.robots = {}, [], (robots_code, robots_text)

    def get(self, url, timeout=None):
        self.calls.append(url)
        return _Resp(*self.robots) if url.endswith("robots.txt") else _Resp(200, "ok")


def test_client_sets_user_agent_and_spaces_requests(monkeypatch):
    sleeps, clock = [], [1000.0]
    monkeypatch.setattr(cdsco.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cdsco.time, "sleep", lambda s: (sleeps.append(s), clock.__setitem__(0, clock[0] + s)))
    c = cdsco.Client(min_interval=0.1, session=_Session())                 # a smaller interval is clamped up to 2 s
    assert c.s.headers["User-Agent"] == cdsco.USER_AGENT and "Hamsa" in cdsco.USER_AGENT
    c.get(cdsco.PAGE_URL)
    c.get(cdsco.PAGE_URL)
    assert len(c.s.calls) == 3 and c.s.calls[0].endswith("/robots.txt")   # robots.txt fetched first
    assert all(s >= 1.99 for s in sleeps) and len(sleeps) >= 2


def test_client_honours_robots_disallow(monkeypatch):
    monkeypatch.setattr(cdsco.time, "sleep", lambda s: None)
    c = cdsco.Client(session=_Session(200, "User-agent: *\nDisallow: /opencms/"))
    with pytest.raises(PermissionError):
        c.get(cdsco.PAGE_URL)


# ---- health check ---------------------------------------------------------------------------
class _FakeDb:
    def __init__(self, last):
        self.last = last

    def one(self, sql, params=()):
        return {"t": self.last}


@pytest.mark.parametrize("age_h,stale", [(10, False), (47, False), (49, True), (None, True)])
def test_health_check_warns_after_48h(monkeypatch, caplog, age_h, stale):
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(scheduler, "_db", lambda: _FakeDb(None if age_h is None else now - timedelta(hours=age_h)))
    with caplog.at_level("WARNING", logger="hamsa.ingest"):
        assert scheduler.health_check(now)["stale"] is stale
    assert bool(caplog.records) is stale and scheduler.is_stale() is stale
