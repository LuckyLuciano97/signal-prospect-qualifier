"""Render the run into the two artefacts a person actually uses.

``report.html`` — one self-contained file, no external assets, sorted
best-first. The design goal is the 30-second read: the summary strip says
how the run went (including, prominently, how much it could *not* see),
then each company is one card whose left edge answers "call them or not"
and whose body shows the receipts — every signal with its source, weight
and link, the score arithmetic, and the draft opener for PASS/MAYBE.

``results.csv`` — the same data flattened for sorting, filtering and
outreach tracking in a spreadsheet.
"""

from __future__ import annotations

import csv
import html
import time

import config
from model import CompanyResult

BAND_COLORS = {
    "PASS": ("#15803d", "#dcfce7"),
    "MAYBE": ("#b45309", "#fef3c7"),
    "FAIL": ("#4b5563", "#e5e7eb"),
}

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font: 14px/1.45 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
       color: #1f2937; background: #f8fafc; padding: 24px; }
.wrap { max-width: 980px; margin: 0 auto; }
h1 { font-size: 20px; margin-bottom: 2px; }
.sub { color: #6b7280; font-size: 12px; margin-bottom: 16px; }
.strip { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 18px; }
.stat { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px;
        padding: 8px 14px; }
.stat b { font-size: 18px; display: block; }
.stat span { font-size: 11px; color: #6b7280; text-transform: uppercase;
             letter-spacing: .04em; }
.coverage-warn { border-left: 3px solid #b45309; }
.card { background: #fff; border: 1px solid #e5e7eb; border-radius: 10px;
        margin-bottom: 12px; display: flex; overflow: hidden; }
.scorebox { flex: 0 0 110px; padding: 14px 10px; text-align: center;
            border-right: 1px solid #f1f5f9; }
.scorebox .pct { font-size: 26px; font-weight: 700; }
.band { display: inline-block; font-size: 11px; font-weight: 600;
        padding: 2px 10px; border-radius: 999px; margin-top: 4px; }
.rank { color: #9ca3af; font-size: 11px; margin-bottom: 4px; }
.body { flex: 1; padding: 14px 16px; min-width: 0; }
.name { font-size: 16px; font-weight: 600; }
.name a { color: #1d4ed8; text-decoration: none; }
.offer { font-size: 12px; color: #374151; margin: 2px 0 6px; }
.offer b { color: #111827; }
.reason { font-size: 13px; color: #374151; margin-bottom: 8px; }
.evidence { list-style: none; margin-bottom: 8px; }
.evidence li { font-size: 13px; padding: 3px 0 3px 8px; border-left: 2px solid #cbd5e1; }
.evidence .src { display: inline-block; background: #eef2ff; color: #3730a3;
                 font-size: 11px; border-radius: 4px; padding: 0 6px; margin-right: 6px; }
.evidence .w { color: #9ca3af; font-size: 11px; }
.evidence a { color: #6b7280; font-size: 11px; text-decoration: none; }
.opener { background: #f0fdf4; border: 1px dashed #86efac; border-radius: 8px;
          padding: 8px 12px; font-size: 13px; margin-bottom: 8px; }
.opener .label { font-size: 10px; text-transform: uppercase; letter-spacing: .05em;
                 color: #15803d; display: block; margin-bottom: 2px; }
.meta { font-size: 11px; color: #6b7280; }
.meta .bad { color: #b45309; font-weight: 600; }
.math { font-size: 11px; color: #9ca3af; margin-bottom: 6px; }
.notes { font-size: 11px; color: #9ca3af; margin-top: 4px; }
.contact { font-size: 12px; color: #374151; margin-bottom: 6px; }
"""

FAILED_STATES = ("blocked", "unreachable", "robots-disallowed")


def _stat(value, label, warn=False) -> str:
    cls = "stat coverage-warn" if warn else "stat"
    return f'<div class="{cls}"><b>{value}</b><span>{html.escape(label)}</span></div>'


def _card(rank: int, r: CompanyResult) -> str:
    fg, bg = BAND_COLORS[r.band]
    site_url = html.escape(f"https://{r.domain}")

    evidence_items = []
    for s in r.signals:
        weight = f'<span class="w">+{s.weight}</span>' if s.weight else \
            '<span class="w">(counted above)</span>'
        link = f' <a href="{html.escape(s.url)}">source</a>' if s.url else ""
        evidence_items.append(
            f'<li><span class="src">{html.escape(s.source)}</span>'
            f'{html.escape(s.detail)} {weight}{link}</li>')
    evidence_html = (f'<ul class="evidence">{"".join(evidence_items)}</ul>'
                     if evidence_items else
                     '<ul class="evidence"><li>No signals gathered.</li></ul>')

    opener_html = ""
    if r.opener:
        opener_html = (f'<div class="opener"><span class="label">Draft opener '
                       f'(grounded in {", ".join(r.opener_grounded_in)})</span>'
                       f'{html.escape(r.opener)}</div>')

    contact_html = ""
    if r.contact_name or r.contact_email:
        bits = " · ".join(html.escape(b) for b in
                          (r.contact_name, r.contact_title, r.contact_email) if b)
        contact_html = f'<div class="contact">Contact (from your list): {bits}</div>'

    coverage_bits = []
    for module, status in r.coverage.items():
        cls = ' class="bad"' if status in FAILED_STATES else ""
        coverage_bits.append(f"{module} <span{cls}>{html.escape(status)}</span>")
    notes_html = ""
    if r.notes:
        notes_html = '<div class="notes">' + "<br>".join(
            html.escape(n) for n in r.notes) + "</div>"

    offer_label = config.OFFERS.get(r.offer, r.offer)

    return f"""
<div class="card">
  <div class="scorebox">
    <div class="rank">#{rank}</div>
    <div class="pct" style="color:{fg}">{r.score}%</div>
    <span class="band" style="color:{fg};background:{bg}">{r.band}</span>
  </div>
  <div class="body">
    <div class="name"><a href="{site_url}">{html.escape(r.company)}</a>
      <span style="color:#9ca3af;font-weight:400;font-size:12px">{html.escape(r.domain)}</span></div>
    <div class="offer">Fits: <b>{html.escape(r.offer)}</b> — {html.escape(offer_label)}</div>
    <div class="math">score = {r.base_score} from rule weights {r.llm_adjustment:+d} model adjustment (cap ±{config.LLM_ADJUSTMENT_CAP})</div>
    <div class="reason">{html.escape(r.reasoning)}</div>
    {contact_html}
    {evidence_html}
    {opener_html}
    <div class="meta">Coverage: {" · ".join(coverage_bits)}</div>
    {notes_html}
  </div>
</div>"""


def write_report(results: list[CompanyResult], meta: dict) -> None:
    ranked = sorted(results, key=lambda r: (-r.score, r.company.lower()))
    bands = {b: sum(1 for r in results if r.band == b) for b in ("PASS", "MAYBE", "FAIL")}
    partial = sum(1 for r in results
                  if any(s in FAILED_STATES for s in r.coverage.values()))

    cards = "".join(_card(i, r) for i, r in enumerate(ranked, 1))
    strip = "".join([
        _stat(len(results), "companies"),
        _stat(bands["PASS"], "pass"),
        _stat(bands["MAYBE"], "maybe"),
        _stat(bands["FAIL"], "fail"),
        _stat(sum(len(r.signals) for r in results), "signals found"),
        _stat(partial, "partial coverage", warn=partial > 0),
    ])

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Signal — prospect qualification run</title>
<style>{CSS}</style></head>
<body><div class="wrap">
<h1>Signal — prospect qualification run</h1>
<div class="sub">{html.escape(meta.get("input", ""))} · {html.escape(meta.get("finished_at", ""))}
 · model: {html.escape(meta.get("model") or "none (rules only)")}
 · every score is the sum of the weighted signals listed under it; companies with no
 gathered evidence are 0% by construction, not judgement.</div>
<div class="strip">{strip}</div>
{cards}
</div></body></html>"""
    config.REPORT_PATH.write_text(page, encoding="utf-8")


CSV_COLUMNS = [
    "rank", "company", "domain", "industry", "score_pct", "band", "offer",
    "base_score", "llm_adjustment", "evidence_count", "evidence", "opener",
    "reasoning", "coverage", "notes",
    "contact_name", "contact_title", "contact_email", "website",
]


def write_csv(results: list[CompanyResult]) -> None:
    ranked = sorted(results, key=lambda r: (-r.score, r.company.lower()))
    with config.RESULTS_PATH.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for rank, r in enumerate(ranked, 1):
            writer.writerow({
                "rank": rank,
                "company": r.company,
                "domain": r.domain,
                "industry": r.industry,
                "score_pct": r.score,
                "band": r.band,
                "offer": r.offer,
                "base_score": r.base_score,
                "llm_adjustment": r.llm_adjustment,
                "evidence_count": len(r.signals),
                "evidence": " || ".join(f"[{s.source}] {s.detail}" for s in r.signals),
                "opener": r.opener,
                "reasoning": r.reasoning,
                "coverage": "; ".join(f"{m}={s}" for m, s in r.coverage.items()),
                "notes": " | ".join(r.notes),
                "contact_name": r.contact_name,
                "contact_title": r.contact_title,
                "contact_email": r.contact_email,
                "website": f"https://{r.domain}",
            })


def finished_at() -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
