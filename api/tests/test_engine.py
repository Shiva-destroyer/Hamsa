"""Section 20 QA items as executable tests. Run:  pytest -q   (needs the seeded DB for the last few)"""
import engine
from engine import Signal, resolve_conflicts, normalise

def S(t, status, official=False): return Signal(t, status, "x", "decoded_code", "ref", "medium", official=official)

def test_official_nsq_always_red():
    v = resolve_conflicts([S("nsq_status", "FAIL", True), S("label_consistency", "PASS")])
    assert (v.verdict, v.reason) == ("RED", "nsq_match")

def test_single_non_official_caps_at_amber():
    for t in ("label_consistency", "code_validity", "visual_heuristic", "replay_pattern"):
        assert resolve_conflicts([S(t, "FAIL")]).verdict == "AMBER"

def test_two_independent_categories_escalate():
    v = resolve_conflicts([S("label_consistency", "FAIL"), S("replay_pattern", "FLAGGED")])
    assert (v.verdict, v.reason) == ("RED", "escalated_conflict")

def test_same_category_does_not_escalate():
    # two label_consistency rows (e.g. two OCR passes) are one category, not two signals
    assert resolve_conflicts([S("label_consistency", "FAIL"), S("label_consistency", "FAIL")]).verdict == "AMBER"

def test_blurry_photo_is_not_independent_of_decode_or_ocr():
    # v2 clarification of rule 3: photo quality is upstream of decoding/OCR -> symptoms of one cause
    assert resolve_conflicts([S("code_validity", "FAIL"), S("visual_heuristic", "FAIL")]).verdict == "AMBER"
    assert resolve_conflicts([S("label_consistency", "FAIL"), S("visual_heuristic", "FAIL")]).verdict == "AMBER"
    assert resolve_conflicts([S("visual_heuristic", "FAIL"), S("replay_pattern", "FLAGGED")]).verdict == "RED"

def test_expired_is_distinct_and_red_wins_with_also_expired():
    assert resolve_conflicts([S("expiry_status", "EXPIRED")]).verdict == "EXPIRED"
    v = resolve_conflicts([S("nsq_status", "FAIL", True), S("expiry_status", "EXPIRED")])
    assert v.verdict == "RED" and v.also_expired

def test_default_inconclusive_replay_row_is_not_a_signal():
    assert resolve_conflicts([S("label_consistency", "FAIL"), S("replay_pattern", "INCONCLUSIVE")]).verdict == "AMBER"

def test_open_dispute_never_changes_verdict():
    v = resolve_conflicts([S("nsq_status", "FAIL", True)], dispute_open=True)
    assert v.verdict == "RED" and v.dispute_open

def test_fuzzy_aliases_do_not_false_alarm():
    assert normalise("CIPLA LIMITED") == normalise("Cipla Ltd.")
    assert engine.manufacturer_match("CIPLA LIMITED", "Cipla Ltd.")[0]
    assert engine.manufacturer_match("M/s Cipla Ltd.", "Cipla Ltd.")[0]
    assert not engine.manufacturer_match("Zenith Labs Pvt. Ltd.", "Cipla Ltd.")[0]

def test_seed_demo_batches():
    exp = {"AX2291": "RED", "EN3302": "EXPIRED", "MQ7756": "GREEN", "FM9184": "RED", "PT8813": "RED", "RV5567": "GREEN", "SW1123": "GREEN", "TX6690": "RED"}
    for b, want in exp.items():
        v, _ = engine.verify_manual(b); assert v.verdict == want, (b, v.verdict)
    assert engine.verify_manual("FM9184")[0].also_expired
    assert engine.verify_manual("PT8813")[0].dispute_open          # open dispute -> banner, verdict unchanged

def test_unknown_batch_is_never_a_confident_green_without_caveat():
    v, rec = engine.verify_manual("ZZ0000")
    assert rec is None and v.verdict == "GREEN"                      # allowed, but flow.render adds 'Not checked' + typed-expiry path


# =============================================================================================
# Additions: rules 1-7, independence matrix, alias table, injection safety, degraded mode, replay delegation
# =============================================================================================
import inspect
import itertools
import sys
from datetime import date, timedelta

import psycopg
import pytest

import db
import snapshot
import vision


def types(v): return {s.signal_type for s in v.signals}
def sig(v, t): return next(s for s in v.signals if s.signal_type == t)


# ---- rules 1-7 ------------------------------------------------------------------------------
def test_rule1_only_active_rows_count_and_history_stays_visible():
    for b in ("RV5567", "SW1123"):                                   # corrected/superseded rows only
        v, _ = engine.verify_manual(b)
        s = sig(v, "nsq_status")
        assert v.verdict == "GREEN" and s.status == "PASS" and not s.official
        assert "earlier NSQ listing" in s.evidence_text and s.source_reference.endswith(".pdf")   # rule 4: correction is shown, not hidden


def test_rule1_nsq_beats_everything_even_with_clean_other_signals():
    v = resolve_conflicts([S("label_consistency", "PASS"), S("code_validity", "PASS"), S("nsq_status", "FAIL", True), S("expiry_status", "PASS")])
    assert (v.verdict, v.reason) == ("RED", "nsq_match") and not v.also_expired


def test_rule2_each_non_official_signal_alone_is_amber_with_single_signal_reason():
    for t, st in (("label_consistency", "FAIL"), ("code_validity", "FAIL"), ("visual_heuristic", "FAIL"), ("replay_pattern", "FLAGGED")):
        v = resolve_conflicts([S(t, st), S("nsq_status", "PASS", True)])
        assert (v.verdict, v.reason) == ("AMBER", "single_signal")


def test_rule3_escalation_lists_the_two_signals():
    v = resolve_conflicts([S("label_consistency", "FAIL"), S("code_validity", "FAIL")])
    assert (v.verdict, v.reason) == ("RED", "escalated_conflict") and set(v.escalated_from) == {"label_consistency", "code_validity"}


def test_rule5_community_reports_never_change_a_verdict():
    assert resolve_conflicts([S("community_reports", "FAIL"), S("community_reports", "FLAGGED")]).verdict == "GREEN"
    assert resolve_conflicts([S("label_consistency", "FAIL"), S("community_reports", "FAIL")]).verdict == "AMBER"


def test_rule6_red_plus_expired_keeps_red_and_flags_also_expired():
    v = resolve_conflicts([S("nsq_status", "FAIL", True), S("expiry_status", "EXPIRED")])
    assert v.verdict == "RED" and v.also_expired
    v, _ = engine.verify_manual("FM9184")
    assert v.verdict == "RED" and v.also_expired


def test_rule6b_expired_with_only_amber_level_warnings_is_expired_primary():
    v = resolve_conflicts([S("expiry_status", "EXPIRED"), S("label_consistency", "FAIL")])
    assert (v.verdict, v.reason, v.secondary_warnings) == ("EXPIRED", "expired", 1)
    v = resolve_conflicts([S("expiry_status", "EXPIRED"), S("label_consistency", "FAIL"), S("visual_heuristic", "FAIL")])
    assert v.verdict == "EXPIRED" and v.secondary_warnings == 2       # visual is upstream of label -> still only amber-level warnings
    v = resolve_conflicts([S("expiry_status", "EXPIRED"), S("label_consistency", "FAIL"), S("replay_pattern", "FLAGGED")])
    assert (v.verdict, v.reason, v.also_expired) == ("RED", "escalated_conflict", True)     # two independent warnings outrank EXPIRED


def test_rule7_open_dispute_adds_banner_flag_but_never_changes_verdict():
    base = [S("nsq_status", "FAIL", True), S("expiry_status", "EXPIRED")]
    for signals in (base, [S("label_consistency", "FAIL")], [S("label_consistency", "FAIL"), S("replay_pattern", "FLAGGED")], [], [S("expiry_status", "EXPIRED")]):
        a, b = resolve_conflicts(signals, False), resolve_conflicts(signals, True)
        assert (a.verdict, a.reason) == (b.verdict, b.reason) and b.dispute_open and not a.dispute_open
    v, _ = engine.verify_manual("PT8813")
    assert v.verdict == "RED" and v.dispute_open and sig(v, "nsq_status").dispute == "open"


def test_appendix_d_manual_matrix():
    want = {"AX2291": "RED", "EN3302": "EXPIRED", "MQ7756": "GREEN", "NS3340": "GREEN", "FM9184": "RED", "PT8813": "RED", "RV5567": "GREEN",
            "SW1123": "GREEN", "TX6690": "RED", "JK4471": "AMBER", "CT5510": "AMBER", "BQ7743": "AMBER", "DR8820": "AMBER",
            "GH6625": "GREEN", "LP2098": "GREEN"}
    for b, verdict in want.items():
        v, _ = engine.verify_manual(b)
        assert v.verdict == verdict, (b, v.verdict, [(s.signal_type, s.status) for s in v.signals])
    assert engine.verify_manual("AX2291")[0].signals[0].synthetic          # demo-data honesty: synthetic row is flagged


def test_manual_typed_expiry_is_low_confidence_and_unverified():
    v, rec = engine.verify_manual("UNK0001", date.today() - timedelta(days=90))
    e = sig(v, "expiry_status")
    assert rec is None and v.verdict == "EXPIRED" and (e.confidence, e.source_reference) == ("low", "Typed by you (unverified)")
    assert "replay_pattern" not in types(v)                              # unknown batch: no preview row
    assert "code_validity" not in types(v) and "label_consistency" not in types(v)   # manual entry can never yield these


# ---- independence matrix (every pair) --------------------------------------------------------
_W = {"label_consistency": "FAIL", "code_validity": "FAIL", "visual_heuristic": "FAIL", "replay_pattern": "FLAGGED"}
_ESCALATES = {frozenset(p) for p in [("label_consistency", "code_validity"), ("label_consistency", "replay_pattern"),
                                     ("code_validity", "replay_pattern"), ("visual_heuristic", "replay_pattern")]}


@pytest.mark.parametrize("a,b", list(itertools.permutations(_W, 2)))
def test_independence_matrix_all_ordered_pairs(a, b):
    v = resolve_conflicts([S(a, _W[a]), S(b, _W[b])])
    if frozenset((a, b)) in _ESCALATES:
        assert (v.verdict, v.reason) == ("RED", "escalated_conflict")
    else:                                                                # visual_heuristic is upstream of code_validity / label_consistency
        assert (v.verdict, v.reason) == ("AMBER", "single_signal")


@pytest.mark.parametrize("a", list(_W))
def test_same_type_twice_and_inconclusive_partner_never_escalate(a):
    assert resolve_conflicts([S(a, _W[a]), S(a, _W[a])]).verdict == "AMBER"
    for b in _W:
        if b != a:
            assert resolve_conflicts([S(a, _W[a]), S(b, "INCONCLUSIVE")]).verdict == "AMBER"
            assert resolve_conflicts([S(a, _W[a]), S(b, "PASS")]).verdict == "AMBER"


def test_independence_helper_is_symmetric_and_official_types_do_not_count():
    for a, b in itertools.product(_W, _W):
        assert engine._independent(a, b) == engine._independent(b, a)
    # nsq_status / expiry_status are not "non-official signals": they never form an escalation pair
    assert resolve_conflicts([S("expiry_status", "EXPIRED"), S("label_consistency", "FAIL")]).reason == "expired"


def test_all_four_non_official_signals_escalate():
    assert resolve_conflicts([S(t, st) for t, st in _W.items()]).reason == "escalated_conflict"


# ---- fuzzy manufacturer aliases (table) ------------------------------------------------------
@pytest.mark.parametrize("printed,canonical,ok", [
    ("Cipla Ltd.", "Cipla Ltd.", True),
    ("CIPLA LIMITED", "Cipla Ltd.", True),
    ("cipla limited", "Cipla Ltd.", True),
    ("M/s Cipla Ltd.", "Cipla Ltd.", True),
    ("M/S CIPLA LIMITED", "Cipla Ltd.", True),
    ("Cipla Ltd", "Cipla Ltd.", True),
    ("Cipla", "Cipla Ltd.", True),
    ("सिप्ला लिमिटेड", "Cipla Ltd.", True),                                  # Devanagari alias of the same group
    ("Sun Pharmaceutical Industries Limited", "Sun Pharmaceutical Industries Ltd.", True),
    ("M/s Sun Pharmaceutical Industries Ltd.", "Sun Pharmaceutical Industries Ltd.", True),
    ("सन फार्मास्युटिकल इंडस्ट्रीज़ लिमिटेड", "Sun Pharmaceutical Industries Ltd.", True),
    ("Zenith Labs Pvt. Ltd.", "Cipla Ltd.", False),
    ("Sun Pharmaceutical Industries Ltd.", "Cipla Ltd.", False),
    ("मैनकाइंड फार्मा लिमिटेड", "Cipla Ltd.", False),                         # a different Devanagari name
    ("सिप्ला लिमिटेड", "Sun Pharmaceutical Industries Ltd.", False),
    ("", "Cipla Ltd.", False),
])
def test_fuzzy_alias_table(printed, canonical, ok):
    assert engine.manufacturer_match(printed, canonical)[0] is ok


def test_fuzzy_match_without_alias_group_uses_canonical_only():
    assert engine.manufacturer_match("Private Limited Test Co", "Private Limited Test Co")[0]
    assert not engine.manufacturer_match("Other Co", "Private Limited Test Co")[0]


@pytest.mark.parametrize("raw,norm", [
    ("M/s Cipla Limited", "cipla ltd"), ("CIPLA  LTD.", "cipla ltd"), ("Bhoomi Remedies Private Limited", "bhoomi remedies pvt ltd"),
    ("Bhoomi Remedies Pvt. Ltd.", "bhoomi remedies pvt ltd"), ("Dr. Reddy's Laboratories", "dr reddy s laboratories"), ("", ""), (None, ""),
])
def test_normalise_rules(raw, norm):
    assert engine.normalise(raw) == norm


def test_normalise_keeps_devanagari_and_kannada_vowel_signs():
    assert engine.normalise("सिप्ला लिमिटेड") == "सिप्ला लिमिटेड"          # matras/virama are marks, not \w: they must survive
    assert engine.normalise("ಸಿಪ್ಲಾ ಲಿಮಿಟೆಡ್") == "ಸಿಪ್ಲಾ ಲಿಮಿಟೆಡ್"


# ---- injection-shaped input -----------------------------------------------------------------
INJECTIONS = ["AX2291'; DROP TABLE nsq_alerts;--", "' OR '1'='1", "Robert'); DROP TABLE batches;--", "AX2291\" OR 1=1 --",
              "x'; UPDATE nsq_alerts SET status='corrected' WHERE '1'='1", "%' UNION SELECT * FROM consent_log --", "\\'; --", "𝔸X2291"]


@pytest.mark.parametrize("bad", INJECTIONS)
def test_sql_injection_shaped_batch_is_just_an_unknown_batch(bad):
    before = db.one("SELECT count(*) AS n, count(*) FILTER (WHERE status='active') AS a FROM nsq_alerts")
    v, rec = engine.verify_manual(bad)
    assert rec is None and v.verdict == "GREEN" and not snapshot.is_degraded()
    assert "match found" in sig(v, "nsq_status").evidence_text
    assert engine.nsq_signal(bad).status == "PASS"
    assert engine.open_dispute(bad) is False and engine.batch_record(bad) is None
    after = db.one("SELECT count(*) AS n, count(*) FILTER (WHERE status='active') AS a FROM nsq_alerts")
    assert before == after
    assert db.one("SELECT to_regclass('batches') IS NOT NULL AS ok")["ok"]


def test_nul_byte_in_batch_is_not_mistaken_for_a_db_outage():
    v, rec = engine.verify_manual("UNK\x000003")
    assert rec is None and v.verdict == "GREEN" and not snapshot.is_degraded()


# ---- degraded mode --------------------------------------------------------------------------
@pytest.fixture
def snap(monkeypatch):
    """Isolated snapshot module state, restored after the test."""
    monkeypatch.setattr(snapshot, "_rows", [])
    monkeypatch.setattr(snapshot, "_date", None)
    monkeypatch.setattr(snapshot, "_degraded", False)
    return snapshot


@pytest.fixture
def db_down(monkeypatch):
    def boom(*a, **k):
        raise psycopg.OperationalError("connection refused (simulated)")
    for fn in ("one", "all_", "run"):
        monkeypatch.setattr(db, fn, boom)


def test_snapshot_loads_all_nsq_rows(snap):
    assert snap.get_snapshot_date() is None and snap.nsq_lookup("AX2291") == []
    snap.load_snapshot()
    assert snap.get_snapshot_date() == date.today() and not snap.is_degraded()
    n = db.one("SELECT count(*) AS n FROM nsq_alerts")["n"]
    assert len(snap._rows) == n
    rows = snap.nsq_lookup("AX2291")
    assert rows and rows[0]["status"] == "active" and rows[0]["source_document"].endswith(".pdf")
    assert snap.nsq_lookup("RV5567")[0]["alert_date"] >= snap.nsq_lookup("RV5567")[-1]["alert_date"]     # newest first, like the live query


def test_degraded_red_from_snapshot_has_prefix_and_flag(snap, monkeypatch):
    snap.load_snapshot()                                                  # loaded while the DB is healthy...
    down = lambda *a, **k: (_ for _ in ()).throw(psycopg.OperationalError("down"))
    for fn in ("one", "all_", "run"):                                     # ...then the DB goes away
        monkeypatch.setattr(db, fn, down)
    v, rec = engine.verify_manual("AX2291")
    s = sig(v, "nsq_status")
    assert (v.verdict, v.reason) == ("RED", "nsq_match") and s.from_cache and s.official
    assert s.evidence_text.startswith(f"Data as of {date.today():%d %b %Y} — live records unavailable.")
    assert snap.is_degraded()


def test_degraded_clean_batch_is_green_only_with_prefix_and_lower_confidence(snap, monkeypatch):
    snap.load_snapshot()
    down = lambda *a, **k: (_ for _ in ()).throw(psycopg.OperationalError("down"))
    for fn in ("one", "all_", "run"):
        monkeypatch.setattr(db, fn, down)
    v, rec = engine.verify_manual("MQ7756")
    s = sig(v, "nsq_status")
    assert v.verdict == "GREEN" and s.from_cache and s.confidence == "medium"
    assert s.evidence_text.startswith("Data as of ") and "live records unavailable." in s.evidence_text
    assert "cached snapshot" in s.source_reference


def test_degraded_without_any_snapshot_is_amber_never_green_never_an_error(snap, db_down):
    for b in ("MQ7756", "AX2291", "UNK0001"):
        v, rec = engine.verify_manual(b)                                  # must not raise
        assert v.verdict == "AMBER" and v.reason == "data_unavailable", b
        s = sig(v, "nsq_status")
        assert s.status == "INCONCLUSIVE" and "unavailable" in s.evidence_text and s.from_cache


def test_degraded_expired_still_wins_over_data_unavailable(snap, db_down):
    v, _ = engine.verify_manual("UNK0001", date.today() - timedelta(days=30))
    assert v.verdict == "EXPIRED"


def test_degraded_recovers_when_db_returns(snap, monkeypatch):
    snap.load_snapshot()
    real_all = db.all_
    monkeypatch.setattr(db, "all_", lambda *a, **k: (_ for _ in ()).throw(psycopg.OperationalError("down")))
    engine.nsq_signal("AX2291")
    assert snap.is_degraded()
    monkeypatch.setattr(db, "all_", real_all)
    s = engine.nsq_signal("AX2291")
    assert not snap.is_degraded() and not s.from_cache and not s.evidence_text.startswith("Data as of")


def test_load_snapshot_failure_keeps_previous_copy_and_marks_degraded(snap, monkeypatch):
    snap.load_snapshot()
    n = len(snap._rows)
    monkeypatch.setattr(db, "all_", lambda *a, **k: (_ for _ in ()).throw(psycopg.OperationalError("down")))
    snap.load_snapshot()                                                  # must not raise
    assert len(snap._rows) == n and snap.is_degraded()


def test_photo_path_degraded_red_from_snapshot(snap, monkeypatch):
    import os
    snap.load_snapshot()
    down = lambda *a, **k: (_ for _ in ()).throw(psycopg.OperationalError("down"))
    for fn in ("one", "all_", "run"):
        monkeypatch.setattr(db, fn, down)
    pack = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "packs", "AX2291.png")
    v, rec, notes = vision.verify_photo(open(pack, "rb").read())        # must not raise
    assert v.verdict == "RED" and sig(v, "nsq_status").from_cache
    assert sig(v, "label_consistency").status == "INCONCLUSIVE" and "OCR_LOW_CONF" in notes    # registry unreachable -> not guessed
    assert rec["batch_number"] == "AX2291"


def test_log_scan_returns_empty_string_when_db_down_and_real_id_otherwise(snap, monkeypatch):
    v, _ = engine.verify_manual("UNK0002")
    sid = engine.log_scan(v, "UNK0002", "")
    try:
        assert len(sid) == 36
        assert db.one("SELECT count(*) AS n FROM scan_evidence WHERE scan_id = %s", (sid,))["n"] == len(v.signals)
    finally:
        db.run("DELETE FROM scan_evidence WHERE scan_id = %s", (sid,))
        db.run("DELETE FROM scan_events WHERE id = %s", (sid,))
    monkeypatch.setattr(db, "one", lambda *a, **k: (_ for _ in ()).throw(psycopg.OperationalError("down")))
    assert engine.log_scan(v, "UNK0002", "") == ""


# ---- replay delegation ------------------------------------------------------------------
def test_replay_delegates_to_replay_module_when_present(monkeypatch):
    import types as _t
    calls = []
    fake = _t.ModuleType("replay")
    fake.replay_signal = lambda batch, serial=None: (calls.append((batch, serial)) or Signal("replay_pattern", "FLAGGED", "[Preview] ext", "scan_log", "x", "low", synthetic=True))
    monkeypatch.setitem(sys.modules, "replay", fake)
    s = engine.replay_signal("AX2291", "SN1")
    assert calls == [("AX2291", "SN1")] and s.evidence_text == "[Preview] ext"


def test_replay_falls_back_to_builtin_when_module_absent(monkeypatch):
    monkeypatch.setitem(sys.modules, "replay", None)                      # `from replay import ...` -> ImportError
    assert engine.replay_signal("CT5510").status == "FLAGGED"            # seed tag impossible_travel_demo
    assert engine.replay_signal("MQ7756", "SN9").status == "INCONCLUSIVE"
    assert engine.replay_signal("MQ7756").synthetic


def test_replay_module_failure_never_breaks_a_verdict(monkeypatch):
    import types as _t
    fake = _t.ModuleType("replay")
    fake.replay_signal = lambda batch, serial=None: (_ for _ in ()).throw(psycopg.OperationalError("down"))
    monkeypatch.setitem(sys.modules, "replay", fake)
    s = engine.replay_signal("CT5510")
    assert s.status == "INCONCLUSIVE" and "unavailable" in s.evidence_text


def test_replay_module_recursing_into_engine_does_not_loop(monkeypatch):
    import types as _t
    fake = _t.ModuleType("replay")
    fake.replay_signal = lambda batch, serial=None: engine.replay_signal(batch, serial)     # a "fall back to engine" implementation
    monkeypatch.setitem(sys.modules, "replay", fake)
    assert engine.replay_signal("CT5510").status == "FLAGGED"


def test_public_signatures_unchanged():
    P = lambda f: [(p.name, p.default) for p in inspect.signature(f).parameters.values()]
    assert P(engine.verify_manual) == [("batch", inspect._empty), ("expiry_typed", None)]
    assert P(engine.resolve_conflicts) == [("signals", inspect._empty), ("dispute_open", False)]
    assert P(engine.log_scan) == [("v", inspect._empty), ("batch", inspect._empty), ("product_code", "")]
    assert P(engine.replay_signal) == [("batch", inspect._empty), ("serial", None)]
    assert P(vision.verify_photo) == [("raw", inspect._empty)]
    for fn in ("load_snapshot", "get_snapshot_date", "nsq_lookup", "is_degraded"):
        assert callable(getattr(snapshot, fn))
