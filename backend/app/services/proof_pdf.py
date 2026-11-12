"""Render proof-of-payment / status-notice / statement PDFs with reportlab (pure Python).

Input is the facts dict from :mod:`app.services.proof`; this module only lays
it out. A line that is not PAID always renders as a "Payment status notice"
that says NOT PAID, never as a proof.
"""

from __future__ import annotations

import io
from xml.sax.saxutils import escape

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate, Flowable, Frame, KeepTogether, PageTemplate, Paragraph, Spacer, Table, TableStyle,
)

INK = colors.HexColor("#244855")
INK_DARK = colors.HexColor("#11262E")
CLAY = colors.HexColor("#874F41")
SAGE = colors.HexColor("#90AEAD")
SAGE_LIGHT = colors.HexColor("#E4EEED")
SAND = colors.HexColor("#FFF4E4")
CORAL = colors.HexColor("#A42F1F")
GREEN = colors.HexColor("#3A8C6B")

W, H = A4
MARGIN = 18 * mm
CONTENT_W = W - 2 * MARGIN

_base = dict(fontName="Helvetica", fontSize=9.2, leading=12.6, textColor=INK_DARK, alignment=TA_LEFT)
S = {
    "body": ParagraphStyle("body", **_base),
    "small": ParagraphStyle("small", **{**_base, "fontSize": 7.8, "leading": 10.4, "textColor": colors.HexColor("#4A5E64")}),
    "label": ParagraphStyle("label", **{**_base, "fontSize": 8, "textColor": colors.HexColor("#557271")}),
    "h": ParagraphStyle("h", **{**_base, "fontName": "Helvetica-Bold", "fontSize": 10.5, "textColor": INK,
                                "spaceBefore": 10, "spaceAfter": 4}),
    "mono": ParagraphStyle("mono", **{**_base, "fontName": "Courier", "fontSize": 7.6, "leading": 10}),
    "title": ParagraphStyle("title", **{**_base, "fontName": "Helvetica-Bold", "fontSize": 17, "leading": 21,
                                        "textColor": colors.white}),
    "sub": ParagraphStyle("sub", **{**_base, "fontSize": 9, "textColor": SAGE}),
}


def _t(v: object) -> str:
    """Escape + make encodable by the built-in Helvetica (WinAnsi) so no glyph turns into a black box."""
    s = "-" if v is None or v == "" else str(v)
    s = s.encode("cp1252", "replace").decode("cp1252")
    return escape(s)


def _p(text: str, style: str = "body") -> Paragraph:
    return Paragraph(text, S[style])


class _Band(Flowable):
    def __init__(self, title: str, subtitle: str, accent: colors.Color):
        super().__init__()
        self.title, self.subtitle, self.accent = title, subtitle, accent
        self.height = 24 * mm

    def wrap(self, aw, ah):
        self.width = aw
        return aw, self.height

    def draw(self):
        c = self.canv
        c.setFillColor(INK)
        c.roundRect(0, 0, self.width, self.height, 3 * mm, stroke=0, fill=1)
        c.setFillColor(self.accent)
        c.rect(0, 0, 2.2 * mm, self.height, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 16.5)
        c.drawString(7 * mm, self.height - 10 * mm, self.title)
        c.setFillColor(SAGE)
        c.setFont("Helvetica", 8.8)
        c.drawString(7 * mm, self.height - 16 * mm, self.subtitle)


def _kv(rows: list[tuple[str, object]], widths=(48 * mm, None)) -> Table:
    data = [[_p(_t(k), "label"), _p(_t(v))] for k, v in rows]
    t = Table(data, colWidths=[widths[0], CONTENT_W - widths[0]])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, SAGE_LIGHT),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
    ]))
    return t


def _qr(url: str, size: float = 30 * mm) -> Drawing:
    w = QrCodeWidget(url)
    x0, y0, x1, y1 = w.getBounds()
    d = Drawing(size, size, transform=[size / (x1 - x0), 0, 0, size / (y1 - y0), 0, 0])
    d.add(w)
    return d


def _integrity(facts: dict, sig: str, url: str, kind_note: str) -> KeepTogether:
    left = [
        _p("Integrity check", "h"),
        _p(_t(kind_note), "small"),
        Spacer(1, 2),
        _p("HMAC-SHA256 over the canonical record", "label"),
        _p(_t(sig[:32]) + "<br/>" + _t(sig[32:]), "mono"),
        Spacer(1, 3),
        _p("Verify (no login needed)", "label"),
        _p(_t(url), "mono"),
    ]
    qr = _qr(url)
    t = Table([[left, qr]], colWidths=[CONTENT_W - 36 * mm, 34 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOX", (0, 0), (-1, -1), 0.6, SAGE),
                           ("BACKGROUND", (0, 0), (-1, -1), SAND), ("LEFTPADDING", (0, 0), (-1, -1), 6),
                           ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    return KeepTogether([t])


def _receipt_rows(facts: dict) -> list[tuple[str, object]]:
    r = facts.get("receipt")
    if not r:
        return [("Ledger receipt", "No settlement receipt was recorded for this run.")]
    rows = [("Ledger receipt hash", f"{r['receipt_hash']} ({r['chain']} chain, #{r['index']})")]
    if r.get("tx_hash"):
        rows.append(("Metal L2 transaction", r["tx_hash"]))
    else:
        rows.append(("Metal L2 anchor", "Not anchored on-chain."))
    return rows


def _approval_rows(facts: dict) -> list[tuple[str, object]]:
    a = facts.get("approval")
    if not a:
        return [("Approval", "No approval is recorded for this run.")]
    return [("Approved by", f"{a['by']} at {a['at']}"),
            ("Visa TAP signature", f"key id {a['tap_key_id'] or 'n/a'} - "
                                   f"{'signature VERIFIED before any money moved' if a['tap_verified'] else 'NOT verified'}")]


def _bank_rows(c: dict) -> list[tuple[str, object]]:
    b = c["bank"]
    rows = [(f"Destination {b['kind']}", f"{b['masked'] or 'not on file'} (last 4 only)")]
    if b.get("bic"):
        rows.append(("Bank identifier (SWIFT/BIC)", b["bic"]))
    rows.append(("Destination country", b.get("country")))
    rows.append(("Bank name", "Not stored by PaySilo"))
    return rows


def _fx_rows(c: dict) -> list[tuple[str, object]]:
    fx = c.get("fx")
    if not fx:
        return [("FX conversion", "None recorded for this line (paid from funds already held in this currency, or no conversion needed).")]
    return [("FX conversion", f"{fx['scope']}: sold {fx['sold']} for {fx['bought']}"),
            ("FX rate (USD per 1 unit)", fx["rate"])]


def _timeline(facts: dict) -> Table:
    data = [[_p("Time (UTC)", "label"), _p("Event", "label")]]
    for ev in facts["timeline"]:
        data.append([_p(_t(ev["at"]), "mono"), _p(_t(ev["label"]))])
    t = Table(data, colWidths=[42 * mm, CONTENT_W - 42 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.4, SAGE_LIGHT),
                           ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                           ("LEFTPADDING", (0, 0), (-1, -1), 2)]))
    return t


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(colors.HexColor("#557271"))
    canvas.drawString(MARGIN, 10 * mm, "Issued by PaySilo. Nothing in this document is a bank statement; see the limits section.")
    canvas.drawRightString(W - MARGIN, 10 * mm, f"Page {doc.page}")
    canvas.restoreState()


def _build(story: list, title: str) -> bytes:
    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN,
                          bottomMargin=18 * mm, title=title, author="PaySilo", subject=title)
    doc.addPageTemplates([PageTemplate(id="p", frames=[Frame(MARGIN, 18 * mm, CONTENT_W, H - MARGIN - 18 * mm, id="f",
                                                             leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)],
                                       onPage=_footer)])
    doc.build(story)
    return buf.getvalue()


LIMITS = ("This document records what Airwallex reported to PaySilo and what PaySilo stored. It does not prove that the "
          "contractor's bank credited the account, and it is not a bank statement. It is accurate only as long as the "
          "integrity check below validates.")


def render_line_pdf(facts: dict, *, sig: str, url: str) -> bytes:
    c = facts["line"]
    proof = facts["doc"] == "proof"
    tr = facts["trace"]
    amount = f"{c['currency']} {c['amount']}"
    story: list = [
        _Band("Proof of payment" if proof else "Payment status notice",
              f"{facts['agency']}  |  on behalf of {facts['employer']}", GREEN if proof else CORAL),
        Spacer(1, 6),
    ]
    if proof:
        story += [_p(f"<b>Airwallex reports this payout of {_t(amount)} to {_t(c['contractor'])} as PAID.</b>")]
    else:
        story += [Table([[_p(f"<b>NOT PAID.</b> {_t(tr['headline'])}")]], colWidths=[CONTENT_W],
                        style=TableStyle([("BOX", (0, 0), (-1, -1), 1, CORAL), ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FBEAE6")),
                                          ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 6),
                                          ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))]
        story += [Spacer(1, 4), _p(_t(facts["notice"]["why"])), Spacer(1, 2),
                  _p("<b>Next step:</b> " + _t(facts["notice"]["next"]))]

    story += [_p("Payment", "h"), _kv([
        ("Contractor", c["contractor"]), ("Payout amount", amount),
        ("Payout method", c["method"] + (" (international wire)" if c["method"] == "SWIFT" else " (local rail)")),
        ("Payout fee", f"{c['currency']} {c['payout_fee']}" if c["payout_fee"] is not None else "Not recorded"),
        ("Status at issue", f"{c['status']} ({tr['state']})"),
        ("Employer", facts["employer"]), ("Agency", facts["agency"]),
        ("Payroll run", f"{facts['run_id']}" + (f" - {facts['period']}" if facts["period"] else "")),
        ("Pay date / created", f"{facts['pay_date'] or '-'} / {facts['created_at']}"),
    ])]
    story += [_p("Destination and FX", "h"), _kv(_bank_rows(c) + _fx_rows(c))]
    aw = c["airwallex"]
    story += [_p("Airwallex references", "h"), _kv([
        ("Airwallex transfer id", aw["transfer_id"] or "None: no transfer was created"),
        ("Request id (idempotency key)", aw["request_id"]),
        ("Last transfer status", aw["transfer_status"] or "-"),
    ])]
    story += [_p("Status timeline (UTC, from PaySilo's records)", "h"), _timeline(facts)]
    if proof:
        story += [Spacer(1, 3), _p(
            f"Airwallex reports PAID at {_t(aw['paid_at'] or 'a time that was not recorded')}; the receiving bank's own "
            "credit time is not available to PaySilo. Typical settlement: SEPA / ACH / FPS 0-2 business days, SWIFT 1-5.", "small")]
    story += [_p("Approval and ledger", "h"), _kv(_approval_rows(facts) + _receipt_rows(facts))]

    if proof:
        story += [_p("What to tell the contractor", "h"), _p(
            f"1. Your payment of <b>{_t(amount)}</b> was sent by Airwallex on behalf of {_t(facts['employer'])}. "
            f"2. Ask your bank to look for an incoming {'international (SWIFT) transfer' if c['method'] == 'SWIFT' else 'local transfer'} "
            f"and give them the Airwallex transfer id <b>{_t(aw['transfer_id'])}</b> as the reference. "
            "3. Check the account ending " + _t(c["bank"]["masked"]) + " and the sender name: it may show as Airwallex or the agency, not the employer. "
            f"4. {_t(tr['expected_window'])} 5. Status PAID means Airwallex completed its side. If your bank still cannot find it, "
            f"send this PDF to {_t(facts['agency'])} so they can ask Airwallex to trace it.")]
    else:
        story += [_p("What to tell the contractor", "h"), _p(
            f"No money has been sent to you for this line. {_t(tr['diagnosis'])} {_t(tr['next_step'])}")]

    story += [Spacer(1, 6), _integrity(facts, sig, url,
                                       "Re-computed from PaySilo's records when you open the link or scan the code. VALID means this "
                                       "document matches the stored record; INVALID means it was altered or the record changed."),
              Spacer(1, 4), _p("What this does not prove", "h"), _p(_t(LIMITS), "small")]
    return _build(story, "Proof of payment" if proof else "Payment status notice")


def render_statement_pdf(facts: dict, *, sig: str, url: str) -> bytes:
    lines = facts["lines"]
    paid = [ln for ln in lines if ln["status"] == "PAID"]
    story: list = [
        _Band("Payment statement", f"{facts['agency']}  |  on behalf of {facts['employer']}", SAGE), Spacer(1, 6),
        _p(f"Run <b>{_t(facts['run_id'])}</b>" + (f" - {_t(facts['period'])}" if facts["period"] else "")
           + f". Status: <b>{_t(facts['status'])}</b>. {len(paid)} of {len(lines)} lines PAID per Airwallex."),
    ]
    if facts.get("run_error"):
        story += [Spacer(1, 3), _p(f"<b>{_t(facts['run_error']['title'])}.</b> {_t(facts['run_error']['message'])}")]
    story += [_p("Payment lines", "h")]
    data = [[_p(x, "label") for x in ("Contractor", "Amount", "Method", "Status", "Account", "Airwallex transfer")]]
    for ln in lines:
        data.append([_p(_t(ln["contractor"])), _p(_t(f"{ln['currency']} {ln['amount']}")), _p(_t(ln["method"])),
                     _p(("<b>%s</b>" % _t(ln["status"])) if ln["status"] != "PAID" else _t(ln["status"])),
                     _p(_t(ln["bank"]["masked"])), _p(_t(ln["airwallex"]["transfer_id"] or "not sent"), "mono")])
    t = Table(data, colWidths=[40 * mm, 28 * mm, 16 * mm, 18 * mm, 22 * mm, CONTENT_W - 124 * mm], repeatRows=1)
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (-1, 0), SAGE_LIGHT),
                           ("LINEBELOW", (0, 0), (-1, -1), 0.4, SAGE_LIGHT), ("TOPPADDING", (0, 0), (-1, -1), 3),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 3), ("LEFTPADDING", (0, 0), (-1, -1), 3)]))
    story += [t]
    fees = [ln for ln in paid if ln["payout_fee"] is not None]
    story += [_p("Run summary", "h"), _kv([
        ("Run created / settled", f"{facts['created_at']} / {facts['settled_at'] or 'not settled'}"),
        ("Payout fees recorded", ", ".join(f"{ln['currency']} {ln['payout_fee']}" for ln in fees) or "None"),
        ("PaySilo run fee", f"USD {facts['fee_charged_usd']}"),
    ] + _approval_rows(facts) + _receipt_rows(facts))]
    story += [_p("Reading this statement", "h"), _p(
        "PAID means Airwallex reported the transfer PAID; the receiving bank's own credit time is not available to PaySilo. "
        "Typical settlement: SEPA / ACH / FPS 0-2 business days, SWIFT 1-5. Lines that are not PAID were not sent: "
        "open the per-contractor status notice for the reason and next step.", "small")]
    story += [Spacer(1, 6), _integrity(facts, sig, url,
                                       "Covers every line above. Re-computed from PaySilo's records when you open the link or scan the code."),
              Spacer(1, 4), _p("What this does not prove", "h"), _p(_t(LIMITS), "small")]
    return _build(story, "Payment statement")
