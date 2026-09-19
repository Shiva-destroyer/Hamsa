"""Polite CDSCO NSQ-page scraper.

Rules: fixed User-Agent, >=2 s between any two requests, robots.txt honoured, no hammering (the scheduler runs it daily).
The listing page links to download_file_division.jsp?num_id=..., which returns a tiny HTML wrapper with an
<iframe src=".../file.pdf">; the PDF is a second request.
"""
import html as htmllib
import os
import re
import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass

import requests

BASE = "https://cdsco.gov.in"
PAGE_URL = BASE + "/opencms/opencms/en/Notifications/nsq-drugs/"
USER_AGENT = "HamsaHackathon/1.0 (+https://github.com/Shiva-destroyer/Hamsa)"
MIN_INTERVAL_S = 2.0
PDF_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "real", "pdfs")

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

_LINK_RE = re.compile(
    r"<a href='(?P<href>[^']*download_file_division\.jsp\?num_id=[^']*)'>\s*<span[^>]*>(?P<title>.*?)</span>.*?</i>\s*(?P<posted>[^<]*)</a>",
    re.S)
_MONTH_RE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[\s\-,]*(20\d\d)\b", re.I)


@dataclass
class Listing:
    title: str
    href: str
    posted: str
    month: tuple | None      # (year, month) the alert covers, from the title
    kind: str                # 'central' | 'state' | 'spurious' | 'other'


def classify(title: str) -> str:
    t = title.lower()
    if "spurious" in t and "nsq" not in t:
        return "spurious"
    if "revis" in t:
        return "other"
    if not ("nsq" in t or "not of standard" in t or "drug alert" in t):
        return "other"
    if "state" in t:
        return "state"
    return "central"


def parse_listing(page_html: str) -> list[Listing]:
    out = []
    for m in _LINK_RE.finditer(page_html):
        title = re.sub(r"\s+", " ", htmllib.unescape(m.group("title"))).strip()
        mm = _MONTH_RE.search(title)
        month = (int(mm.group(2)), MONTHS[mm.group(1).lower()]) if mm else None
        out.append(Listing(title, m.group("href"), m.group("posted").strip(), month, classify(title)))
    return out


def select_latest(listings: list[Listing], months: int = 3, include_spurious: bool = False) -> list[Listing]:
    """Central + state NSQ (optionally spurious) lists for the latest `months` distinct months on the page."""
    wanted = {"central", "state"} | ({"spurious"} if include_spurious else set())
    cand = [l for l in listings if l.kind in wanted and l.month]
    latest = sorted({l.month for l in cand}, reverse=True)[:months]
    picked = [l for l in cand if l.month in latest]
    return sorted(picked, key=lambda l: (l.month, l.kind), reverse=True)


class Client:
    def __init__(self, min_interval: float = MIN_INTERVAL_S, session=None):
        self.s = session or requests.Session()
        self.s.headers["User-Agent"] = USER_AGENT
        self.min_interval = max(min_interval, MIN_INTERVAL_S)
        self._last = 0.0
        self._robots = None

    def _wait(self):
        d = self.min_interval - (time.monotonic() - self._last)
        if d > 0:
            time.sleep(d)
        self._last = time.monotonic()

    def allowed(self, url: str) -> bool:
        if self._robots is None:
            rp = urllib.robotparser.RobotFileParser()
            self._wait()
            r = self.s.get(BASE + "/robots.txt", timeout=30)
            # no robots.txt (404) = no restrictions; any other non-200 = be conservative
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
            elif 400 <= r.status_code < 500:
                rp.parse([])
            else:
                rp.parse(["User-agent: *", "Disallow: /"])
            self._robots = rp
        return self._robots.can_fetch(USER_AGENT, url)

    def get(self, url: str) -> requests.Response:
        if not self.allowed(url):
            raise PermissionError(f"robots.txt disallows {url}")
        self._wait()
        r = self.s.get(url, timeout=60)
        r.raise_for_status()
        return r

    def listing(self) -> list[Listing]:
        return parse_listing(self.get(PAGE_URL).text)

    def download(self, item: Listing, dest_dir: str = PDF_DIR) -> str:
        """Fetch one alert PDF (two requests: wrapper, then the PDF). Returns the local path; skips if already present."""
        os.makedirs(dest_dir, exist_ok=True)
        wrapper = self.get(urllib.parse.urljoin(BASE, item.href)).text
        m = re.search(r"src='([^']+\.pdf)'", wrapper, re.I)
        if not m:
            raise RuntimeError(f"no PDF iframe in wrapper for {item.title!r}; site layout changed?")
        pdf_path = htmllib.unescape(m.group(1))
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(pdf_path))
        dest = os.path.join(dest_dir, name)
        if os.path.exists(dest) and os.path.getsize(dest) > 1000:
            return dest
        r = self.get(BASE + urllib.parse.quote(pdf_path))
        if not r.content.startswith(b"%PDF"):
            raise RuntimeError(f"{item.title!r}: response is not a PDF")
        with open(dest, "wb") as f:
            f.write(r.content)
        return dest


def fetch_latest(months: int = 3, include_spurious: bool = False, dest_dir: str = PDF_DIR) -> list[str]:
    c = Client()
    return [c.download(l, dest_dir) for l in select_latest(c.listing(), months, include_spurious)]
