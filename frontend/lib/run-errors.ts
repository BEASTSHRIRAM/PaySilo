// Turns a payroll run's raw `error` (policy refusals, Airwallex failures) into
// something an operator can act on. The raw text stays available as
// "technical details" — never hidden, just not the headline.

export interface RunErrorInfo {
  title: string;
  message: string;
  hint?: string;
  technical: string;
}

const AIRWALLEX_CODE = /code=([a-z_]+)/i;

/** Plain-language text for the risk gate's hold reasons (codes are decided by the backend, never by a model). */
export const HOLD_REASON_TEXT: Record<string, { label: string; message: string }> = {
  BANK_UNVERIFIED: {
    label: "Bank details not verified",
    message: "Bank details changed/not verified yet — a second person must verify them.",
  },
  RECENT_BANK_CHANGE_LARGE_PAYMENT: {
    label: "New bank details, large payment",
    message: "Bank details changed in the last 7 days and this payment is unusually large — a second person must verify them by a callback first.",
  },
};

export function explainRunError(raw: string | null | undefined): RunErrorInfo | null {
  if (!raw) return null;
  const technical = raw.trim();
  let m: RegExpMatchArray | null;

  if ((m = technical.match(/Live FX rate for ([A-Z]{3}) is unavailable/))) {
    return {
      title: "Couldn't get a live exchange rate",
      message: `Airwallex didn't return a live ${m[1]} rate, so PaySilo stopped before pricing this payroll. It never prices salaries with a guessed rate. No money moved.`,
      hint: "Run the payroll again in a minute. If it keeps failing, Airwallex may not support this currency for the employer.",
      technical,
    };
  }
  if ((m = technical.match(/No (?:USD )?rate supplied for ([A-Z]{3})/))) {
    return {
      title: "Exchange rate missing",
      message: `There is no exchange rate for ${m[1]}, so PaySilo refused to price this payroll instead of guessing. No money moved.`,
      hint: "Run the payroll again. If it repeats, remove or change the contractor paid in that currency.",
      technical,
    };
  }
  if ((m = technical.match(/No payout fee schedule for ([A-Z]{3}) via ([A-Z]+)/))) {
    return {
      title: `${m[1]} ${m[2]} payouts aren't supported yet`,
      message: `PaySilo doesn't know the ${m[2]} payout fee for ${m[1]}, so it refused to price this payroll rather than guess the fee. No money moved.`,
      hint: `Pay this contractor by another method (e.g. ${m[1]} LOCAL or EUR SWIFT), or ask the team to add a ${m[1]} ${m[2]} fee.`,
      technical,
    };
  }
  if ((m = technical.match(/Missing bank details for (.+?);/))) {
    return {
      title: "Bank details missing",
      message: `PaySilo can't pay ${m[1]} because there are no bank details on file. No money moved.`,
      hint: "Add an IBAN or account number (and SWIFT/BIC for SWIFT) on the employer's contractor list, then run again.",
      technical,
    };
  }
  if (/held by the risk gate|bank details changed|second person/i.test(technical)) {
    return {
      title: "Held until bank details are verified",
      message: "PaySilo did not pay any line that has unverified or recently changed bank details. No money moved.",
      hint: "Ask a second person (owner, admin or approver) to call the contractor on a number you already know and verify the details on the employer page, then start the payroll again or pay the held lines.",
      technical,
    };
  }
  if (/Nothing to pay/i.test(technical)) {
    return {
      title: "Nothing to pay",
      message: "This run had no pay lines, so there was nothing to price or approve.",
      hint: "Give contractors a default amount, or start the run with amounts.",
      technical,
    };
  }
  if (/TAP verification failed/i.test(technical)) {
    return {
      title: "Approval signature couldn't be verified",
      message: "The approval's Visa TAP signature didn't match this exact payroll, so no money moved.",
      hint: "Start a new run and approve it again.",
      technical,
    };
  }
  if (/HTTP \d{3}/.test(technical) || AIRWALLEX_CODE.test(technical)) {
    const code = technical.match(AIRWALLEX_CODE)?.[1];
    const fields = technical.match(/\[fields: ([^\]]+)\]/)?.[1];
    const payee = /beneficiar/i.test(technical);
    return {
      title: payee ? "Airwallex rejected a payee's details" : "Airwallex rejected a request",
      message: `Airwallex returned an error${code ? ` (${code.replace(/_/g, " ")})` : ""}${fields ? ` on ${fields}` : ""}. PaySilo stopped at that step; payouts already sent are marked PAID below.`,
      hint: "Check the technical details, then retry. Retries are safe — every step reuses its request id, so nobody is paid twice.",
      technical,
    };
  }
  return { title: "This run stopped", message: technical, technical };
}
