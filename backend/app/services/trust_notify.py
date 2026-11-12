"""Thin hook for contractor-trust emails (onboarding link, bank-change callback request).

The outbox/Mailer notifications service (Brevo SMTP) is being added separately. Until it lands this
helper only records that a notification WOULD be queued: it never logs the link, token or bank values.
Swap the body of :func:`notify` for an outbox enqueue when the service is available.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("paysilo.trust_notify")


def notify(kind: str, *, to_email: str | None, organization_id, context: dict | None = None) -> None:
    """``kind``: ``contractor_onboarding_link`` | ``bank_change_callback_request``. ``context`` may carry the
    one-time link; it must go to the mailer only, never to logs."""
    logger.info("trust notification pending (no mailer wired): kind=%s has_recipient=%s", kind, bool(to_email))
