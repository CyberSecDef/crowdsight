"""Phase 8 Step 3 — where reports live, and how they are rendered.

Two things happen here, and the second is where the previous two steps either
pay off or quietly disappear.

**Storage keeps one source of truth.** A report is written once as JSON, and the
Markdown and HTML are rendered from it on demand. Rendering at write time would
freeze the presentation of documents that outlive several changes to it; a
report exported next year should read the way the current renderer reads, not
the way the one from the year it was generated did.

**Rendering carries the evidence with the claim.** Each finding is followed by
the posts, agents and rounds it rests on, so a reader checking one does not
have to hunt through an appendix — and a finding with no evidence line is
visible as such at a glance. The verification record is rendered too, always,
including on a clean report: a document that quietly dropped three fabricated
claims looks identical to one that never made them, and the difference is
exactly what a reader needs when deciding how far to trust the rest.

**Everything rendered to HTML is escaped.** A report contains model-written
prose *and* agent-written post content, and an agent can be persuaded to write
anything at all. Untrusted text reaching a browser unescaped is how a
simulation becomes a cross-site scripting vector against the person reading it.
"""

from __future__ import annotations

import html
import json
import logging
import os
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

__all__ = [
    "REPORT_FILE",
    "ReportNotFound",
    "ReportStore",
    "new_report_id",
    "render_html",
    "render_markdown",
]

DEFAULT_REPORT_DIR = Path("data/reports")
REPORT_FILE = "report.json"

#: ``rep-20260807-143022-a1b2c3``. Sorts chronologically in a directory listing
#: and never collides, the same shape as a simulation id. Reports are not
#: keyed on the simulation because a run can reasonably be reported on more
#: than once — after more rounds, or with a larger tool budget.
REPORT_ID_PATTERN = re.compile(r"^rep-\d{8}-\d{6}-[0-9a-f]{6}$")


class ReportNotFound(LookupError):
    """No report with that id."""


def new_report_id(*, now: datetime | None = None,
                  rng: random.Random | None = None) -> str:
    rng = rng or random.Random()
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    return f"rep-{stamp}-{rng.randrange(16 ** 6):06x}"


class ReportStore:
    """The ``data/reports`` directory."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        self.base_dir = Path(base_dir) if base_dir else DEFAULT_REPORT_DIR

    # -- paths --------------------------------------------------------------

    def report_dir(self, report_id: str) -> Path:
        if not REPORT_ID_PATTERN.match(report_id or ""):
            # Also the path-traversal guard: report_id arrives from a URL.
            raise ReportNotFound(f"Not a report id: {report_id!r}")
        return self.base_dir / report_id

    def path(self, report_id: str) -> Path:
        return self.report_dir(report_id) / REPORT_FILE

    def exists(self, report_id: str) -> bool:
        try:
            return self.path(report_id).is_file()
        except ReportNotFound:
            return False

    # -- writing ------------------------------------------------------------

    def save(self, report: Any, *, sim_id: str = "",
             report_id: str | None = None) -> str:
        """Write a report and return its id."""
        report_id = report_id or self._unused_id()
        payload = report.model_dump() if hasattr(report, "model_dump") else dict(report)
        # The caller's sim_id wins when given: `setdefault` silently kept a
        # stale value the report was carrying, so a report saved against one
        # run could be filed under another.
        if sim_id:
            payload["sim_id"] = sim_id
        payload.setdefault("sim_id", "")
        payload["report_id"] = report_id
        payload["generated_at"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds")

        path = self.path(report_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, default=str),
                             encoding="utf-8")
        os.replace(temporary, path)
        logger.info("Report %s written for %s", report_id, payload.get("sim_id") or "-")
        return report_id

    def _unused_id(self, attempts: int = 8) -> str:
        for _ in range(attempts):
            candidate = new_report_id()
            if not (self.base_dir / candidate).exists():
                return candidate
        raise RuntimeError("Could not allocate an unused report id")

    # -- reading ------------------------------------------------------------

    def load(self, report_id: str) -> dict[str, Any]:
        path = self.path(report_id)
        if not path.is_file():
            raise ReportNotFound(f"No report {report_id!r}")
        return json.loads(path.read_text(encoding="utf-8"))

    def list(self, *, sim_id: str | None = None,
             limit: int = 100) -> list[dict[str, Any]]:
        """Newest first — the id sorts chronologically."""
        if not self.base_dir.is_dir():
            return []
        out = []
        for entry in sorted(self.base_dir.iterdir(), reverse=True):
            if not entry.is_dir() or not REPORT_ID_PATTERN.match(entry.name):
                continue
            try:
                payload = self.load(entry.name)
            except (ReportNotFound, ValueError) as exc:
                logger.warning("Skipping unreadable report %s: %s", entry.name, exc)
                continue
            if sim_id and payload.get("sim_id") != sim_id:
                continue
            grounding = payload.get("grounding") or {}
            out.append({
                "report_id": entry.name,
                "sim_id": payload.get("sim_id", ""),
                "generated_at": payload.get("generated_at", ""),
                "summary": (payload.get("executive_summary") or "")[:200],
                "citations_resolved": grounding.get("resolved", 0),
                "citations_checked": grounding.get("checked", 0),
                "claims_dropped": len(grounding.get("dropped") or []),
            })
            if len(out) >= limit:
                break
        return out

    def delete(self, report_id: str) -> bool:
        import shutil

        directory = self.report_dir(report_id)
        if not directory.is_dir():
            return False
        shutil.rmtree(directory, ignore_errors=True)
        return True


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _evidence_line(citation: dict[str, Any] | None) -> str:
    """The posts, agents and rounds a claim rests on, or nothing."""
    if not citation:
        return ""
    parts = []
    for label, key in (("post", "post_ids"), ("agent", "agent_ids"),
                       ("round", "rounds")):
        values = citation.get(key) or []
        if values:
            plural = "s" if len(values) > 1 else ""
            parts.append(f"{label}{plural} {', '.join(str(v) for v in values)}")
    return "; ".join(parts)


def _sentiment_table(trajectory: Iterable[dict[str, Any]]) -> list[str]:
    rows = list(trajectory)
    if not rows:
        return ["_No sentiment was scored for this run._", ""]
    lines = ["| Round | Mean sentiment | Posts scored | Stances |",
             "|---:|---:|---:|---|"]
    for row in rows:
        mean = row.get("mean_score")
        stances = ", ".join(f"{k} {v}" for k, v in
                            sorted((row.get("stances") or {}).items()))
        lines.append(
            f"| {row.get('round')} | "
            f"{'—' if mean is None else f'{mean:+.2f}'} | "
            f"{row.get('scored', 0)}/{row.get('posts', 0)} | {stances or '—'} |")
    lines.append("")
    return lines


def render_markdown(report: dict[str, Any]) -> str:
    """The report as Markdown, evidence beside each claim."""
    out: list[str] = []
    add = out.append

    add(f"# Simulation report — {report.get('sim_id') or 'unknown run'}")
    add("")
    meta = [f"**Report** `{report.get('report_id', '')}`"]
    if report.get("generated_at"):
        meta.append(f"**Generated** {report['generated_at']}")
    if report.get("graph_id"):
        meta.append(f"**Graph** `{report['graph_id']}`")
    add("  \n".join(meta))
    add("")
    if report.get("event"):
        add("> " + report["event"].replace("\n", " "))
        add("")

    add("## Executive summary")
    add("")
    add(report.get("executive_summary") or "_None provided._")
    add("")

    add("## Sentiment trajectory")
    add("")
    out.extend(_sentiment_table(report.get("sentiment_trajectory") or []))
    if report.get("sentiment_reading"):
        add(report["sentiment_reading"])
        add("")

    for heading, key, label_field in (
            ("Dominant narratives", "dominant_narratives", "label"),
            ("Counter-narratives", "counter_narratives", "label")):
        add(f"## {heading}")
        add("")
        items = report.get(key) or []
        if not items:
            add("_None identified._")
            add("")
            continue
        for item in items:
            add(f"### {item.get(label_field) or 'Untitled'}")
            add("")
            add(item.get("summary") or "")
            if item.get("support"):
                add("")
                add(f"*Support:* {item['support']}")
            add("")
            add(_evidence_markdown(item.get("citation")))
            add("")

    add("## Influential agents")
    add("")
    agents = report.get("influential_agents") or []
    if not agents:
        add("_None identified._")
        add("")
    else:
        for agent in agents:
            handle = agent.get("username") or f"agent {agent.get('user_id')}"
            add(f"### {handle} (agent {agent.get('user_id')})")
            add("")
            add(agent.get("why") or "")
            add("")
            add(_evidence_markdown(agent.get("citation")))
            add("")
    if report.get("influence_propagation"):
        add("### How influence propagated")
        add("")
        add(report["influence_propagation"])
        add("")

    add("## Emergent behaviour")
    add("")
    findings = report.get("emergent_behaviour") or []
    if not findings:
        add("_None identified._")
        add("")
    else:
        for finding in findings:
            add(f"- **{finding.get('claim') or ''}**"
                + (f" — {finding['detail']}" if finding.get("detail") else ""))
            evidence = _evidence_line(finding.get("citation"))
            add(f"  *Evidence:* {evidence}" if evidence
                else "  *No evidence cited.*")
        add("")

    add("## Caveats")
    add("")
    caveats = report.get("caveats") or []
    if caveats:
        out.extend(f"- {c}" for c in caveats)
    else:
        add("_None recorded._")
    add("")

    out.extend(_verification_markdown(report.get("grounding") or {}))
    return "\n".join(out).rstrip() + "\n"


def _evidence_markdown(citation: dict[str, Any] | None) -> str:
    evidence = _evidence_line(citation)
    return (f"*Evidence:* {evidence}" if evidence
            else "*No evidence cited — this rests on the analyst's reading.*")


def _verification_markdown(grounding: dict[str, Any]) -> list[str]:
    """Always rendered, including when clean.

    Omitting it on a clean report would leave a reader unable to tell
    "verified and sound" from "never verified", which is the distinction the
    section exists to provide.
    """
    out = ["## Verification", ""]
    if not grounding:
        out.extend(["_This report was not verified against its run._", ""])
        return out
    if grounding.get("empty_run"):
        out.extend(["_The run holds no data, so no citation could be checked._", ""])
        return out

    checked = grounding.get("checked", 0)
    resolved = grounding.get("resolved", 0)
    out.append(f"{resolved} of {checked} citation(s) resolved to real posts, "
               f"agents or rounds in this run.")
    out.append("")

    dropped = grounding.get("dropped") or []
    if dropped:
        out.append(f"**{len(dropped)} claim(s) were removed** because they cited "
                   f"evidence that does not exist:")
        out.append("")
        out.extend(f"- *{d.get('claim', '')}* ({d.get('section', '')}) — "
                   f"{d.get('reason', '')}" for d in dropped)
        out.append("")

    uncited = grounding.get("uncited_claims") or []
    if uncited:
        out.append(f"{len(uncited)} finding(s) cite no evidence and rest on the "
                   f"analyst's reading.")
        out.append("")

    prose = grounding.get("prose_unresolved") or []
    if prose:
        out.append("The following references in the prose do not exist in this run:")
        out.append("")
        out.extend(f"- {p.get('where', '')}: {p.get('kind', '')} {p.get('value', '')}"
                   for p in prose)
        out.append("")

    if not dropped and not prose:
        out.append("No fabricated references were found.")
        out.append("")
    return out


CSS = """\
:root { color-scheme: light dark; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
       Helvetica, Arial, sans-serif; line-height: 1.6; max-width: 46rem;
       margin: 2rem auto; padding: 0 1.25rem; }
h1 { border-bottom: 2px solid currentColor; padding-bottom: .3rem; }
h2 { margin-top: 2.5rem; border-bottom: 1px solid rgba(128,128,128,.35);
     padding-bottom: .2rem; }
h3 { margin-top: 1.75rem; }
blockquote { border-left: 3px solid rgba(128,128,128,.5); margin-left: 0;
             padding-left: 1rem; opacity: .85; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
th, td { border: 1px solid rgba(128,128,128,.35); padding: .4rem .6rem;
         text-align: left; }
th { background: rgba(128,128,128,.12); }
.evidence { font-size: .9em; opacity: .8; font-style: italic; }
.uncited { font-size: .9em; color: #b45309; font-style: italic; }
.verification { background: rgba(128,128,128,.08); padding: 1rem 1.25rem;
                border-radius: .4rem; }
.dropped { color: #b91c1c; }
"""


def render_html(report: dict[str, Any]) -> str:
    """The report as a standalone HTML document.

    Rendered from the same structure as the Markdown rather than by converting
    it, so nothing has to be re-parsed — and, more importantly, so every piece
    of text is escaped exactly once on the way in. A report carries agent-written
    post content, and an agent can be persuaded to write anything.
    """
    def esc(value: Any) -> str:
        return html.escape(str(value if value is not None else ""), quote=True)

    def paragraphs(text: Any) -> str:
        blocks = [b.strip() for b in str(text or "").split("\n\n") if b.strip()]
        return "\n".join(f"<p>{esc(b)}</p>" for b in blocks) or "<p><em>None.</em></p>"

    def evidence(citation: dict[str, Any] | None) -> str:
        line = _evidence_line(citation)
        if line:
            return f'<p class="evidence">Evidence: {esc(line)}</p>'
        return ('<p class="uncited">No evidence cited — this rests on the '
                'analyst\'s reading.</p>')

    parts: list[str] = [
        "<!DOCTYPE html>", '<html lang="en">', "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>Simulation report — {esc(report.get('sim_id') or 'run')}</title>",
        f"<style>{CSS}</style>", "</head>", "<body>",
        f"<h1>Simulation report — {esc(report.get('sim_id') or 'unknown run')}</h1>",
    ]
    meta = [f"Report <code>{esc(report.get('report_id', ''))}</code>"]
    if report.get("generated_at"):
        meta.append(f"Generated {esc(report['generated_at'])}")
    if report.get("graph_id"):
        meta.append(f"Graph <code>{esc(report['graph_id'])}</code>")
    parts.append(f'<p class="evidence">{" · ".join(meta)}</p>')
    if report.get("event"):
        parts.append(f"<blockquote>{esc(report['event'])}</blockquote>")

    parts.append("<h2>Executive summary</h2>")
    parts.append(paragraphs(report.get("executive_summary")))

    parts.append("<h2>Sentiment trajectory</h2>")
    trajectory = report.get("sentiment_trajectory") or []
    if not trajectory:
        parts.append("<p><em>No sentiment was scored for this run.</em></p>")
    else:
        parts.append("<table><thead><tr><th>Round</th><th>Mean sentiment</th>"
                     "<th>Posts scored</th><th>Stances</th></tr></thead><tbody>")
        for row in trajectory:
            mean = row.get("mean_score")
            stances = ", ".join(f"{k} {v}" for k, v in
                                sorted((row.get("stances") or {}).items()))
            parts.append(
                f"<tr><td>{esc(row.get('round'))}</td>"
                f"<td>{'—' if mean is None else esc(f'{mean:+.2f}')}</td>"
                f"<td>{esc(row.get('scored', 0))}/{esc(row.get('posts', 0))}</td>"
                f"<td>{esc(stances or '—')}</td></tr>")
        parts.append("</tbody></table>")
    if report.get("sentiment_reading"):
        parts.append(paragraphs(report["sentiment_reading"]))

    for heading, key in (("Dominant narratives", "dominant_narratives"),
                         ("Counter-narratives", "counter_narratives")):
        parts.append(f"<h2>{heading}</h2>")
        items = report.get(key) or []
        if not items:
            parts.append("<p><em>None identified.</em></p>")
            continue
        for item in items:
            parts.append(f"<h3>{esc(item.get('label') or 'Untitled')}</h3>")
            parts.append(paragraphs(item.get("summary")))
            if item.get("support"):
                parts.append(f"<p><em>Support:</em> {esc(item['support'])}</p>")
            parts.append(evidence(item.get("citation")))

    parts.append("<h2>Influential agents</h2>")
    agents = report.get("influential_agents") or []
    if not agents:
        parts.append("<p><em>None identified.</em></p>")
    for agent in agents:
        handle = agent.get("username") or f"agent {agent.get('user_id')}"
        parts.append(f"<h3>{esc(handle)} "
                     f"<small>(agent {esc(agent.get('user_id'))})</small></h3>")
        parts.append(paragraphs(agent.get("why")))
        parts.append(evidence(agent.get("citation")))
    if report.get("influence_propagation"):
        parts.append("<h3>How influence propagated</h3>")
        parts.append(paragraphs(report["influence_propagation"]))

    parts.append("<h2>Emergent behaviour</h2>")
    findings = report.get("emergent_behaviour") or []
    if not findings:
        parts.append("<p><em>None identified.</em></p>")
    for finding in findings:
        detail = f" — {esc(finding['detail'])}" if finding.get("detail") else ""
        parts.append(f"<p><strong>{esc(finding.get('claim'))}</strong>{detail}</p>")
        parts.append(evidence(finding.get("citation")))

    parts.append("<h2>Caveats</h2>")
    caveats = report.get("caveats") or []
    if caveats:
        parts.append("<ul>" + "".join(f"<li>{esc(c)}</li>" for c in caveats) + "</ul>")
    else:
        parts.append("<p><em>None recorded.</em></p>")

    parts.append('<div class="verification">')
    parts.append("<h2>Verification</h2>")
    grounding = report.get("grounding") or {}
    if not grounding:
        parts.append("<p><em>This report was not verified against its run.</em></p>")
    elif grounding.get("empty_run"):
        parts.append("<p><em>The run holds no data, so no citation could be "
                     "checked.</em></p>")
    else:
        parts.append(
            f"<p>{esc(grounding.get('resolved', 0))} of "
            f"{esc(grounding.get('checked', 0))} citation(s) resolved to real "
            f"posts, agents or rounds in this run.</p>")
        dropped = grounding.get("dropped") or []
        if dropped:
            parts.append(f'<p class="dropped"><strong>{len(dropped)} claim(s) '
                         f"were removed</strong> because they cited evidence that "
                         f"does not exist:</p><ul>")
            parts.extend(
                f"<li><em>{esc(d.get('claim'))}</em> ({esc(d.get('section'))}) — "
                f"{esc(d.get('reason'))}</li>" for d in dropped)
            parts.append("</ul>")
        uncited = grounding.get("uncited_claims") or []
        if uncited:
            parts.append(f"<p>{len(uncited)} finding(s) cite no evidence and rest "
                         f"on the analyst's reading.</p>")
        prose = grounding.get("prose_unresolved") or []
        if prose:
            parts.append("<p>The following references in the prose do not exist "
                         "in this run:</p><ul>")
            parts.extend(
                f"<li>{esc(p.get('where'))}: {esc(p.get('kind'))} "
                f"{esc(p.get('value'))}</li>" for p in prose)
            parts.append("</ul>")
        if not dropped and not prose:
            parts.append("<p>No fabricated references were found.</p>")
    parts.append("</div>")

    parts.extend(["</body>", "</html>"])
    return "\n".join(parts)
