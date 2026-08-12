"""Render the public, JSON-backed civic website.

This renderer is intentionally separate from :mod:`ames_digest.render`: email
clients need inline styles, while the public site uses one cached stylesheet.
"""

from __future__ import annotations

import html
import re
from datetime import date
from pathlib import Path

from .record import MeetingRecord

SITE_CSS = """\
@font-face { font-family: 'Barlow'; src: url('/fonts/barlow.woff2') format('woff2'); font-display: swap; }
@font-face { font-family: 'Barlow Condensed'; src: url('/fonts/barlow-condensed.woff2') format('woff2'); font-display: swap; }
@font-face { font-family: 'IBM Plex Mono'; src: url('/fonts/plex-mono.woff2') format('woff2'); font-display: swap; }
:root { --ink:#202b2e; --muted:#687477; --paper:#f4f1ea; --line:#aeb8b5; --accent:#d35432; --wash:#e8e4da; --max:960px; }
* { box-sizing:border-box; }
body { margin:0; background:var(--paper); color:var(--ink); font-family:Barlow,Arial,sans-serif; line-height:1.5; }
a { color:inherit; }
.site-header { border-bottom:1px solid var(--line); padding:20px max(24px,calc((100% - var(--max))/2)); display:flex; justify-content:space-between; gap:24px; align-items:center; }
.brand { font-family:'Barlow Condensed',Arial Narrow,sans-serif; font-size:1.35rem; font-weight:700; letter-spacing:.04em; text-decoration:none; text-transform:uppercase; }
nav { display:flex; gap:20px; font-family:'IBM Plex Mono',monospace; font-size:.7rem; text-transform:uppercase; }
nav a { text-decoration:none; } nav a:hover { color:var(--accent); }
main { max-width:var(--max); margin:0 auto; padding:64px 24px 80px; }
.kicker,.mono { font-family:'IBM Plex Mono',monospace; font-size:.7rem; letter-spacing:.08em; text-transform:uppercase; }
h1,h2,h3 { margin:0; font-family:'Barlow Condensed',Arial Narrow,sans-serif; line-height:.95; text-transform:uppercase; }
h1 { font-size:clamp(3rem,9vw,6.5rem); max-width:800px; letter-spacing:-.03em; }
h2 { font-size:2.3rem; margin-bottom:20px; } h3 { font-size:1.65rem; }
.lede { color:var(--muted); font-size:1.15rem; max-width:620px; }
.blueprint { position:relative; border:1px solid var(--line); padding:28px; background:rgba(255,255,255,.16); }
.blueprint:before,.blueprint:after,.blueprint > .corner:before,.blueprint > .corner:after { content:''; position:absolute; width:9px; height:9px; border-color:var(--accent); }
.blueprint:before { top:-1px; left:-1px; border-top:1px solid; border-left:1px solid; } .blueprint:after { top:-1px; right:-1px; border-top:1px solid; border-right:1px solid; }
.blueprint > .corner:before { bottom:-1px; left:-1px; border-bottom:1px solid; border-left:1px solid; } .blueprint > .corner:after { bottom:-1px; right:-1px; border-bottom:1px solid; border-right:1px solid; }
.hero { margin-bottom:54px; } .hero .lede { margin:20px 0 26px; }
.button { display:inline-block; background:var(--accent); color:#fff; padding:11px 17px; text-decoration:none; font-family:'IBM Plex Mono',monospace; font-size:.72rem; text-transform:uppercase; }
.rail { border-left:1px solid var(--line); margin:36px 0 52px 11px; padding-left:30px; }
.node { position:relative; margin:0 0 28px; } .node:before { content:''; position:absolute; width:13px; height:13px; border-radius:50%; background:var(--accent); left:-38px; top:9px; }
.node.routine:before,.node.consent:before { background:var(--paper); border:1px solid var(--line); }
.node-card { padding:22px; } .node-card.major { border-color:var(--accent); } .node-card.standard { border-color:var(--line); }
.node-title { margin:6px 0 10px; } .summary { max-width:680px; }
.facts { display:grid; grid-template-columns:repeat(3,1fr); border-top:1px solid var(--line); margin-top:18px; padding-top:13px; gap:14px; }
.fact label { display:block; color:var(--muted); font-family:'IBM Plex Mono',monospace; font-size:.62rem; text-transform:uppercase; } .fact span { font-size:.9rem; }
.why { border-top:1px solid var(--line); margin-top:18px; padding-top:14px; } .why strong { color:var(--accent); font-family:'IBM Plex Mono',monospace; font-size:.7rem; text-transform:uppercase; }
.routine-list { margin:8px 0 0; padding:0; list-style:none; color:var(--muted); } .routine-list li { margin:4px 0; }
.footer-note { margin-top:42px; color:var(--muted); font-size:.8rem; }
.meeting-list { display:grid; gap:18px; margin-top:32px; } .meeting-list a { text-decoration:none; } .meeting-list .blueprint:hover { border-color:var(--accent); }
.meeting-date { color:var(--accent); } .meeting-summary { color:var(--muted); margin:8px 0 0; }
.steps { display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin:32px 0; } .step b { color:var(--accent); font:2rem 'IBM Plex Mono',monospace; }
@media (max-width:650px) { .site-header { align-items:flex-start; flex-direction:column; } nav { gap:12px; } main { padding-top:42px; } .blueprint { padding:20px; } .facts,.steps { grid-template-columns:1fr; } .rail { padding-left:20px; margin-left:7px; } .node:before { left:-28px; } }
"""


def _e(value: object) -> str:
    return html.escape(str(value or ""))


def _layout(title: str, body: str) -> str:
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="description" content="Public summaries of Ames city council meetings"><title>{_e(title)} | Ames Council</title><link rel="stylesheet" href="/styles.css"></head><body><header class="site-header"><a class="brand" href="/">Ames Council / Digest</a><nav><a href="/">Meetings</a><a href="/about.html">About</a><a href="mailto:subscribe@mpdavis.com">Subscribe</a></nav></header>{body}<footer class="site-header"><span class="mono">Not affiliated with the City of Ames</span><span class="mono">Every summary links to its source.</span></footer></body></html>'''


def _facts(item: dict) -> str:
    facts = item.get("facts") or []
    if not facts:
        return ""
    return '<div class="facts">' + "".join(f'<div class="fact"><label>{_e(f.get("label"))}</label><span>{_e(f.get("value"))}</span></div>' for f in facts[:3]) + "</div>"


def _item(item: dict) -> str:
    weight = str(item.get("weight") or "routine")
    title = _e(item.get("title") or "Untitled agenda item")
    number = _e(item.get("item_number") or item.get("code") or "")
    if weight in {"routine", "consent"}:
        return f'<div class="node {weight}"><div class="kicker">{number} {"Consent" if weight == "consent" else "Routine"}</div><ul class="routine-list"><li>{title}</li></ul></div>'
    details = f'<div class="blueprint node-card {weight}"><span class="corner"></span><div class="kicker">{number} · {_e(item.get("item_type"))}</div><h3 class="node-title">{title}</h3><p class="summary">{_e(item.get("summary"))}</p>'
    if weight == "major" and item.get("why_it_matters"):
        details += f'<div class="why"><strong>Why it matters</strong><p>{_e(item.get("why_it_matters"))}</p></div>'
    return f'<div class="node">{details}{_facts(item)}</div></div>'


def render_meeting(record: MeetingRecord) -> str:
    selected = record.outcome or record.preview or {}
    meeting = record.meeting
    date_text = date.fromisoformat(record.meeting_date).strftime("%B %-d, %Y")
    items = "".join(_item(item) for item in record.items)
    if not items:
        items = '<p class="lede">No item records are available for this meeting.</p>'
    source = " · ".join(_e(meeting.get(key)) for key in ("agenda_url", "packet_url", "minutes_url") if meeting.get(key))
    short_version = str(selected.get("body", "")).splitlines()[0] if selected.get("body") else ""
    venue = meeting.get("meeting_time") or meeting.get("venue") or record.agenda.get("meeting_time")
    packet_url = meeting.get("packet_url") or meeting.get("agenda_url") or "#"
    body = f'''<main><div class="hero"><div class="kicker">Preview · published {_e(selected.get("generated_at", ""))}</div><h1>{_e(record.board)}<br><span class="meeting-date">{_e(date_text)}</span></h1><p class="lede">{_e(venue)} · {_e(record.agenda.get("location"))}<br><a href="{_e(packet_url)}">Agenda packet and source documents</a></p></div><section class="blueprint"><span class="corner"></span><div class="kicker">The short version</div><p class="lede">{_e(short_version)}</p></section><section class="rail">{items}</section><section class="blueprint"><span class="corner"></span><h2>Public forum</h2><p>Email the council with a question or correction about a source document.</p><a class="button" href="mailto:council@cityofames.org">Email the council</a></section><p class="footer-note mono">AI-assisted summaries are not official records. Verify decisions against the source minutes and packet. {source}</p></main>'''
    return _layout(f"{record.board} {date_text}", body)


def render_home(records: list[MeetingRecord]) -> str:
    cards = []
    for record in sorted(records, key=lambda r: r.meeting_date, reverse=True):
        selected = record.outcome or record.preview or {}
        date_text = date.fromisoformat(record.meeting_date).strftime("%b %-d, %Y")
        summary = re.sub(r"[#*_]", "", str(selected.get("body") or "")).split("\n")[0]
        cards.append(f'<a href="/{_e(record.key)}.html"><article class="blueprint"><span class="corner"></span><div class="kicker meeting-date">{_e(date_text)} · {"published" if record.preview else "scheduled"}</div><h2>{_e(record.board)}</h2><p class="meeting-summary">{_e(summary[:220])}</p><span class="mono">Read the summary →</span></article></a>')
    body = f'<main><section class="hero"><div class="kicker">A civic reading room</div><h1>Know what your council is doing.</h1><p class="lede">Clear, source-linked summaries of Ames city council meetings, built from the public agenda, packet, and minutes.</p><a class="button" href="mailto:subscribe@mpdavis.com">Email updates <span class="mono">(coming soon)</span></a></section><section><div class="kicker">Meetings / chronological</div><div class="meeting-list">{"".join(cards) or "<p class=lede>No meetings published yet.</p>"}</div></section></main>'
    return _layout("Meetings", body)


def render_about() -> str:
    body = '''<main><section class="hero"><div class="kicker">About the digest</div><h1>Public records, made readable.</h1><p class="lede">A meeting packet is hundreds of pages. This site gives residents a quick starting point without pretending a summary replaces the record.</p></section><section class="steps"><div class="step"><b>01</b><h3>Packet pulled</h3><p>The public city repository is checked for the agenda and packet.</p></div><div class="step"><b>02</b><h3>Items summarized</h3><p>Each agenda item is read independently and linked to its source.</p></div><div class="step"><b>03</b><h3>Important items surfaced</h3><p>Cards are weighted structurally so the docket can be scanned.</p></div><div class="step"><b>04</b><h3>Minutes matched back</h3><p>Once published, the official minutes add what council decided.</p></div></section><section class="blueprint"><span class="corner"></span><div class="kicker">Limits</div><h2>A summary is a map, not the record.</h2><p>AI can miss context, misread a scanned page, or flatten a complicated discussion. Follow the source links, especially before making decisions based on a meeting.</p><a class="button" href="mailto:report@mpdavis.com">Report an error</a> <a href="https://www.cityofames.org/">Official City of Ames site</a></section></main>'''
    return _layout("About", body)


def publish(output_dir: Path) -> None:
    records = [r for path in output_dir.glob("*.json") if (r := MeetingRecord.load(output_dir, path.stem))]
    (output_dir / "styles.css").write_text(SITE_CSS, encoding="utf-8")
    (output_dir / "index.html").write_text(render_home(records), encoding="utf-8")
    (output_dir / "about.html").write_text(render_about(), encoding="utf-8")
    for stored in records:
        (output_dir / f"{stored.key}.html").write_text(render_meeting(stored), encoding="utf-8")
