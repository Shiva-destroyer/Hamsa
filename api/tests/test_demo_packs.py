"""every generated demo pack gives its expected verdict through vision.verify_photo; the print sheet has the expected page count.
Needs the seeded DB (DATABASE_URL), e.g. a scratch database."""
import os, re, sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import make_demo_packs as m, print_sheet


@pytest.fixture(scope="module")
def packs(tmp_path_factory):
    d = tmp_path_factory.mktemp("packs")
    return str(d), {n: m.make(n, spec, str(d)) for n, spec in m.PACKS.items()}


def test_every_pack_matches_appendix_d(packs):
    assert m.verify(packs[1]) == []


def test_pack_set_covers_required_scenarios():
    assert {"AX2291", "EN3302", "MQ7756", "GH6625", "CT5510", "BQ7743", "LP2098", "NS3340_DM", "MQ7756_CROP", "MALFORMED"} == set(m.PACKS)


def test_print_sheet_is_a4_two_per_page(packs):
    out, pages = print_sheet.build(packs[0])
    assert pages == 5 and os.path.getsize(out) > 10_000
    pdf = open(out, "rb").read()
    assert len(re.findall(rb"/Type\s*/Page[^s]", pdf)) == 5 and b"595" in pdf and b"841" in pdf
