"""Single-file HTML report.

Hand-rolled inline SVG, no CDN and no build step -- it must open from a clone with
no network. This is a *view* over computations that already exist; it performs no
analysis of its own, which is the only thing keeping it from becoming a project.
"""

from __future__ import annotations

import html
from pathlib import Path

from .config import DEFAULT, Config
from .detectors import build_context, run_all
from .model import Dataset, fmt_ts

CSS = """
:root { --ink:#16191d; --dim:#6b7280; --line:#e5e7eb; --red:#b91c1c; --amber:#b45309;
        --green:#15803d; --violet:#7c3aed; --bg:#fff; }
* { box-sizing:border-box; }
body { font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
       color:var(--ink); background:var(--bg); margin:0 auto; padding:40px 28px; max-width:960px; }
h1 { font-size:26px; margin:0 0 4px; }
h2 { font-size:17px; margin:38px 0 12px; padding-bottom:6px; border-bottom:1px solid var(--line); }
h3 { font-size:14px; margin:22px 0 6px; }
.sub { color:var(--dim); margin:0 0 26px; }
.lede { background:#fef2f2; border-left:3px solid var(--red); padding:12px 16px; margin:20px 0; }
table { border-collapse:collapse; width:100%; margin:10px 0 18px; font-size:13px; }
th,td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--line); }
th { color:var(--dim); font-weight:600; font-size:11px; text-transform:uppercase;
     letter-spacing:.04em; }
td.num,th.num { text-align:right; font-variant-numeric:tabular-nums; }
.bad { color:var(--red); font-weight:600; }
.ok { color:var(--green); }
.warn { color:var(--amber); }
.brk { color:var(--violet); }
.finding { border:1px solid var(--line); border-left-width:3px; padding:14px 16px; margin:14px 0; }
.finding.critical { border-left-color:var(--red); }
.finding.high { border-left-color:#dc2626; }
.finding.medium { border-left-color:var(--amber); }
.finding.low { border-left-color:var(--dim); }
.tag { display:inline-block; font-size:10px; text-transform:uppercase; letter-spacing:.05em;
       padding:2px 7px; border:1px solid var(--line); border-radius:9px; margin-right:6px;
       color:var(--dim); }
.ev { font-size:12px; color:#374151; margin:3px 0 3px 14px; }
.ev code { background:#f3f4f6; padding:1px 4px; border-radius:3px; }
.alt { background:#faf5ff; border-left:2px solid var(--violet); padding:8px 12px; margin:8px 0; font-size:13px; }
.caveat { font-size:12px; color:var(--dim); }
footer { margin-top:44px; padding-top:14px; border-top:1px solid var(--line);
         font-size:12px; color:var(--dim); }
"""


def _esc(value) -> str:
    return html.escape(str(value))


def _funnel_svg(delivered: int, duplicated: int, lost: int) -> str:
    total = delivered + duplicated + lost or 1
    width = 860
    segments = [
        (delivered, "#15803d", "delivered once"),
        (duplicated, "#b45309", "duplicated"),
        (lost, "#b91c1c", "never delivered"),
    ]
    parts, x = [], 0.0
    for count, colour, label in segments:
        if not count:
            continue
        w = count / total * width
        parts.append(
            f'<rect x="{x:.1f}" y="0" width="{w:.1f}" height="42" fill="{colour}"/>'
            f'<text x="{x + w / 2:.1f}" y="26" fill="#fff" font-size="13" '
            f'text-anchor="middle" font-weight="600">{count}</text>'
            f'<text x="{x + w / 2:.1f}" y="60" fill="#6b7280" font-size="11" '
            f'text-anchor="middle">{label}</text>'
        )
        x += w
    return f'<svg viewBox="0 0 {width} 70" width="100%" height="80">{"".join(parts)}</svg>'


def _latency_svg(points: list[tuple[str, float]], deploys: list[tuple[str, str]]) -> str:
    if not points:
        return ""
    width, height, pad = 860, 200, 34
    values = [v for _, v in points]
    top = max(values) * 1.15
    step = (width - pad * 2) / max(len(points) - 1, 1)

    path = " ".join(
        f"{'M' if i == 0 else 'L'}{pad + i * step:.1f},"
        f"{height - pad - (v / top) * (height - pad * 2):.1f}"
        for i, (_, v) in enumerate(points)
    )
    dots = "".join(
        f'<circle cx="{pad + i * step:.1f}" '
        f'cy="{height - pad - (v / top) * (height - pad * 2):.1f}" r="2.5" '
        f'fill="{"#b91c1c" if v > values[0] * 3 else "#15803d"}"/>'
        for i, (_, v) in enumerate(points)
    )
    marks = ""
    for label, when in deploys:
        matches = [i for i, (ts, _) in enumerate(points) if ts >= when]
        if not matches:
            continue
        x = pad + matches[0] * step
        marks += (
            f'<line x1="{x:.1f}" y1="{pad - 12}" x2="{x:.1f}" y2="{height - pad}" '
            f'stroke="#7c3aed" stroke-width="1" stroke-dasharray="3,3"/>'
            f'<text x="{x + 3:.1f}" y="{pad - 15}" font-size="10" fill="#7c3aed">{_esc(label)}</text>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}">'
        f'<line x1="{pad}" y1="{height - pad}" x2="{width - pad}" y2="{height - pad}" '
        f'stroke="#e5e7eb"/>'
        f'<text x="{pad}" y="{pad - 16}" font-size="10" fill="#6b7280">'
        f'end-to-end ms, peak {max(values):.0f}</text>'
        f'<path d="{path}" fill="none" stroke="#16191d" stroke-width="1.5"/>{dots}{marks}</svg>'
    )


def build_html(dataset: Dataset, config: Config = DEFAULT) -> str:
    context = build_context(dataset, config)
    findings = run_all(context)
    accounting = context.accounting
    health = context.health
    errors = health.errors
    window = dataset.window

    out: list[str] = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<title>Tracelens — message pipeline analysis</title>",
        f"<style>{CSS}</style></head><body>",
        "<h1>Tracelens</h1>",
        f"<p class='sub'>Message pipeline analysis — {fmt_ts(window[0])[:10]} to "
        f"{fmt_ts(window[1])[:10]}, {accounting.total} accepted messages, "
        f"{len(dataset.spans)} spans, {len(dataset.logs)} log records.</p>",
        "<div class='lede'><strong>The platform broke its <code>202</code> promise for "
        f"{accounting.share(accounting.stopped):.1%} of messages and 100% of one channel — "
        f"and {errors.span_status_errors} of {errors.total_spans} spans report a status "
        "other than OK.</strong></div>",
        "<h2>Delivery funnel</h2>",
        _funnel_svg(accounting.delivered_once, accounting.delivered_duplicate, accounting.stopped),
        "<table><tr><th>outcome</th><th class='num'>count</th><th class='num'>share</th></tr>",
    ]
    for label, count, bad in [
        ("reached provider exactly once", accounting.delivered_once, False),
        ("reached provider more than once", accounting.delivered_duplicate, False),
        ("never reached provider", accounting.stopped, True),
    ]:
        cls = " class='bad'" if bad and count else ""
        out.append(
            f"<tr><td{cls}>{label}</td><td class='num'{cls}>{count}</td>"
            f"<td class='num'{cls}>{accounting.share(count):.1%}</td></tr>"
        )
    out.append(
        f"<tr><td>provider calls issued</td><td class='num'>{accounting.provider_calls}</td>"
        "<td class='num'></td></tr></table>"
    )

    out.append("<h2>Per channel</h2><table><tr><th>channel</th><th class='num'>accepted</th>"
               "<th class='num'>delivered</th><th class='num'>lost</th>"
               "<th class='num'>duplicated</th><th class='num'>trace intact</th></tr>")
    for name, bucket in accounting.by_channel.items():
        lost = f"<span class='bad'>{bucket.lost}</span>" if bucket.lost else "0"
        intact = f"{bucket.trace_intact}/{bucket.accepted}"
        if bucket.trace_intact < bucket.accepted:
            intact = f"<span class='brk'>{intact}</span>"
        out.append(
            f"<tr><td>{_esc(name)}</td><td class='num'>{bucket.accepted}</td>"
            f"<td class='num'>{bucket.delivered}</td><td class='num'>{lost}</td>"
            f"<td class='num'>{bucket.duplicated}</td><td class='num'>{intact}</td></tr>"
        )
    out.append("</table>")

    out.append("<h2>Error rates — three numbers, never collapsed</h2>")
    out.append(
        "<table><tr><th>signal</th><th class='num'>value</th></tr>"
        f"<tr><td>spans with status != OK</td><td class='num'>"
        f"{errors.span_status_errors}/{errors.total_spans}</td></tr>"
        f"<tr><td>provider calls with a non-2xx status</td><td class='num'>"
        f"{errors.provider_errors}/{errors.provider_calls}</td></tr>"
        f"<tr><td class='bad'>accepted but never delivered</td><td class='num bad'>"
        f"{errors.delivery_failures}/{errors.accepted}</td></tr></table>"
    )
    if errors.diverges:
        out.append(
            "<p class='caveat'>The gap between the first and last rows is itself the "
            "headline: any alert, SLO, or dashboard built on span status or log level "
            "shows this pipeline as healthy through every incident in this window.</p>"
        )

    sends = sorted(
        (
            (fmt_ts(a.send.start_time), t.end_to_end_ms)
            for t in context.traces.values()
            for a in t.attempts[:1]
            if t.channel == "email" and t.end_to_end_ms
        )
    )
    if sends:
        out.append("<h2>Email end-to-end latency, with deploy markers</h2>")
        out.append(
            _latency_svg(
                sends,
                [(d.sha, fmt_ts(d.deployed_at)) for d in dataset.deploys if d.service == "comms-sender"],
            )
        )

    out.append("<h2>Per hop</h2><table><tr><th>hop</th><th class='num'>seen</th>"
               "<th class='num'>lost</th><th class='num'>trace breaks</th>"
               "<th class='num'>median</th><th>spread</th></tr>")
    for name, hop in health.hops.items():
        latency = hop.latency
        if hop.nested:
            median, spread = "nested", "offset within parent, not latency"
        elif latency.n == 0:
            median, spread = "-", "-"
        elif not latency.has_variance:
            median, spread = f"{latency.median:.0f}ms", "variance: none"
        else:
            median = f"{latency.median:.0f}ms"
            spread = f"{latency.minimum:.0f}–{latency.maximum:.0f}ms"
        lost = f"<span class='bad'>{hop.absent}</span>" if hop.absent else "0"
        breaks = f"<span class='brk'>{hop.fallback_joins}</span>" if hop.fallback_joins else "0"
        out.append(
            f"<tr><td>{_esc(name)}</td><td class='num'>{hop.observed}</td>"
            f"<td class='num'>{lost}</td><td class='num'>{breaks}</td>"
            f"<td class='num'>{median}</td><td>{spread}</td></tr>"
        )
    out.append("</table>")

    out.append("<h2>Findings</h2>")
    for finding in findings:
        out.append(f"<div class='finding {finding.severity}'>")
        out.append(
            f"<h3>{_esc(finding.title)}</h3>"
            f"<div><span class='tag'>{finding.severity}</span>"
            f"<span class='tag'>{finding.confidence}</span>"
            f"<span class='tag'>{finding.affected_count} affected</span>"
            f"<span class='tag'>{_esc(finding.id)}</span></div>"
            f"<p>{_esc(finding.summary)}</p>"
        )
        for item in finding.evidence:
            out.append(
                f"<div class='ev'><code>{_esc(item.ref)}</code> — {_esc(item.detail)}</div>"
            )
        if finding.alternatives:
            out.append(
                "<div class='alt'><strong>Competing explanations the data cannot "
                "separate:</strong><ul>"
            )
            for alternative in finding.alternatives:
                out.append(f"<li>{_esc(alternative.summary)}</li>")
            out.append("</ul></div>")
        if finding.would_resolve:
            out.append("<div class='ev'><strong>Would resolve:</strong><ul>")
            for item in finding.would_resolve:
                out.append(f"<li>{_esc(item)}</li>")
            out.append("</ul></div>")
        if finding.low_confidence_rate:
            out.append(
                "<p class='caveat'>Rate confidence low — sample is below "
                f"min_samples={config.min_samples}; read the count, not the percentage.</p>"
            )
        out.append("</div>")

    out.append(
        "<h2>Caveats</h2><ul class='caveat'>"
        f"<li>{accounting.total} messages over "
        f"{(window[1] - window[0]).days + 1} days in a lower environment — every threshold is "
        "a parameter, not a literal, and no rate here extrapolates.</li>"
        "<li>Async hop latency has zero variance across all messages, so hop percentiles are "
        "not computed. Only the provider call carries real latency signal.</li>"
        "<li>Zero-traffic days are reported as insufficient data, never as a drop. Production "
        "sends on weekends even though this environment does not.</li>"
        "<li>No infrastructure configuration is in the export, so subscription-filter and "
        "visibility-timeout mechanisms are inferences, flagged as such.</li></ul>"
    )
    out.append(
        "<footer>Generated by <code>tracelens report</code>. Every figure is reproducible "
        "from a CLI command; <code>python scripts/verify_claims.py</code> recomputes them "
        "from the raw export.</footer></body></html>"
    )
    return "".join(out)


def write_report(dataset: Dataset, out_path: str, config: Config = DEFAULT) -> Path:
    path = Path(out_path)
    path.write_text(build_html(dataset, config), encoding="utf-8")
    return path
