"""Deterministic "where is my money?" diagnosis for one pay line.

Pure code, no LLM and no network: the diagnosis is a function of the pay
line's stored state (plus, optionally, the run's error text and the
Airwallex transfer status/failure code). It never invents a bank credit
time: PaySilo only knows what Airwallex reported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Airwallex transfer failure codes (see backend/docs/proof-of-payment.md).
FAILURE_CODES: dict[str, tuple[str, str]] = {
    "90301": ("The name did not match the bank account",
              "The receiving bank could not match the beneficiary name to the account holder. "
              "Ask the contractor to confirm the exact account-holder name on their bank statement."),
    "90302": ("The account cannot receive this currency",
              "The destination account does not accept the payout currency. "
              "Ask the contractor for an account that can receive that currency, or switch the payout currency."),
    "90401": ("The bank details were invalid",
              "The IBAN, account number or routing/SWIFT code was rejected as invalid. "
              "Ask the contractor to re-send their bank details, and confirm them by phone before changing anything."),
    "90802": ("The receiving bank returned the payment",
              "The receiving bank sent the money back (closed account, wrong account or a compliance block). "
              "The funds return to the employer's wallet; nobody has been paid."),
}
_CODE_RE = re.compile(r"\b(90301|90302|90401|90802)\b")


def settlement_window(method: str) -> str:
    """Typical settlement window by rail (PaySilo's rule of thumb, not a bank guarantee)."""
    if (method or "").upper() == "SWIFT":
        return "SWIFT payouts typically take 1 to 5 business days to reach the receiving bank."
    return "Local payouts (SEPA, ACH, Faster Payments) typically take 0 to 2 business days."


def extract_failure_code(*texts: str | None) -> str | None:
    for t in texts:
        if t:
            m = _CODE_RE.search(str(t))
            if m:
                return m.group(1)
    return None


@dataclass(frozen=True)
class RunErrorExplained:
    title: str
    message: str
    hint: str | None = None


def explain_run_error(raw: str | None) -> RunErrorExplained | None:
    """Python twin of frontend/lib/run-errors.ts (friendly text for a run's raw error)."""
    if not raw:
        return None
    t = raw.strip()
    if m := re.search(r"Live FX rate for ([A-Z]{3}) is unavailable", t):
        return RunErrorExplained("Couldn't get a live exchange rate",
                                 f"Airwallex didn't return a live {m[1]} rate, so PaySilo stopped before paying. No money moved.",
                                 "Run the payroll again in a minute.")
    if m := re.search(r"No (?:USD )?rate supplied for ([A-Z]{3})", t):
        return RunErrorExplained("Exchange rate missing",
                                 f"There is no exchange rate for {m[1]}, so PaySilo refused to price this payroll. No money moved.")
    if m := re.search(r"No payout fee schedule for ([A-Z]{3}) via ([A-Z]+)", t):
        return RunErrorExplained(f"{m[1]} {m[2]} payouts aren't supported yet",
                                 f"PaySilo doesn't know the {m[2]} payout fee for {m[1]}, so it refused to pay rather than guess. No money moved.")
    if m := re.search(r"Missing bank details for (.+?);", t):
        return RunErrorExplained("Bank details missing",
                                 f"There are no bank details on file for {m[1]}. No money moved.",
                                 "Add an IBAN or account number, then run again.")
    if re.search(r"TAP verification failed", t, re.I):
        return RunErrorExplained("Approval signature couldn't be verified",
                                 "The approval's Visa TAP signature didn't match this exact payroll, so no money moved.")
    if re.search(r"HTTP \d{3}", t) or re.search(r"code=[a-z_]+", t, re.I):
        return RunErrorExplained("Airwallex rejected a request",
                                 "Airwallex returned an error for this payout. PaySilo stopped at that step.",
                                 "Retrying is safe: every step reuses its request id, so nobody is paid twice.")
    return RunErrorExplained("This run stopped", t[:300])


def _held_reason(run_status: str | None, run_error: str | None) -> str:
    if run_status == "HELD_SHORTFALL":
        return ("The employer's own wallet did not cover the whole payroll, so PaySilo paid the smaller lines first "
                "and held this one.")
    if run_status == "HELD_BY_HUMAN":
        return "An approver chose to hold this payroll."
    if run_status == "APPROVAL_REJECTED":
        return "The approval was rejected, so nothing was paid."
    if run_status == "REFUSED":
        exp = explain_run_error(run_error)
        return f"PaySilo refused to pay this run: {exp.message}" if exp else "PaySilo refused to pay this run."
    return "This line was held and has not been paid."


def payment_trace(line: Any, run: Any, *, transfer_status: str | None = None,
                  failure_code: str | None = None) -> dict:
    """Plain-English diagnosis + next step for one ``PayLine`` of ``run``.

    ``transfer_status`` is the last Airwallex transfer status we recorded (if any).
    Returns ``{state, paid, headline, diagnosis, next_step, expected_window, failure_code}``.
    """
    status = (line.status or "").upper()
    ts = (transfer_status or "").upper()
    code = failure_code or extract_failure_code(getattr(run, "error", None))
    window = settlement_window(line.method)
    if ts in ("RETURNED", "BOUNCED") or code == "90802":
        state = "RETURNED"
    elif status == "FAILED" or ts in ("FAILED", "CANCELLED", "REJECTED"):
        state = "FAILED"
    elif status == "HELD":
        state = "HELD"
    elif status == "PAID":
        state = "PAID"
    elif line.airwallex_transfer_id:
        state = "SENT"
    else:
        state = "SCHEDULED"

    out: dict[str, Any] = {"state": state, "paid": state == "PAID", "failure_code": None,
                           "expected_window": window}
    if state == "PAID":
        out.update(
            headline="Airwallex reports this payout as PAID.",
            diagnosis=("The money left the employer's wallet and Airwallex marked the transfer PAID. "
                       "The receiving bank's own credit time is not available to PaySilo. " + window),
            next_step=("If the contractor still cannot see it, ask them to check their bank with the Airwallex "
                       f"transfer id {line.airwallex_transfer_id or '(see the proof PDF)'}: banks can post incoming "
                       "payments with a delay and sometimes list them under the sender's name rather than the employer's. "
                       "If it is still missing after the window above, contact the agency support with the proof PDF."))
    elif state == "SENT":
        out.update(
            headline="The payout was submitted and is on its way.",
            diagnosis="Airwallex accepted the transfer but has not reported it as PAID yet. " + window,
            next_step="Nothing to do yet. If it is still not PAID after the window above, contact the agency so they can query Airwallex.")
    elif state == "SCHEDULED":
        out.update(
            headline="This payout has not been sent yet.",
            diagnosis="The payroll run is still waiting for approval or execution, so no money has left the employer's wallet.",
            next_step="An approver must approve the run. Nothing is owed to the contractor until the run is approved and paid.")
    elif state == "HELD":
        out.update(
            headline="This payout is on hold. Nobody has been paid.",
            diagnosis=_held_reason(run.status, getattr(run, "error", None)),
            next_step="Fix the cause (for example add funds), then pay the held lines in a new run. It will need a fresh approval.")
    elif state == "FAILED":
        out.update(failure_code=code, headline="This payout failed. Nobody has been paid.")
        if code in FAILURE_CODES:
            title, text = FAILURE_CODES[code]
            out.update(diagnosis=f"Airwallex code {code}: {title}. {text}",
                       next_step="Get corrected details from the contractor through a trusted channel (call-back), "
                                 "update the contractor record, then run the payment again. It needs a fresh approval.")
        else:
            exp = explain_run_error(getattr(run, "error", None))
            out.update(diagnosis=exp.message if exp else "Airwallex or PaySilo could not complete this payout.",
                       next_step=(exp.hint if exp and exp.hint else "Retry the run: retries are safe and never pay twice."))
    else:  # RETURNED
        out.update(failure_code=code, headline="The payment was returned. Nobody has been paid.")
        title, text = FAILURE_CODES["90802"]
        out.update(diagnosis=f"{title}. {text}",
                   next_step="Confirm the contractor's bank details with them by phone, correct the record, then pay again with a fresh approval.")
    return out
