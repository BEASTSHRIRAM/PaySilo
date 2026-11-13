# PaySilo emails

Every mail goes through the `Notification` outbox (`services/notify.py`): queued with a dedupe key, delivered
asynchronously by Brevo SMTP, retried with backoff. Mails that carry a code or private link are SENSITIVE: the
text is kept only until delivery and is never shown by the notifications API, audit or logs.

## House style (enforced by `tests/test_email_complete.py`, checker in `services/email_style.py`)

1. Short and professional. Body at most 90 words, not counting figure rows, URLs, greeting and closing.
2. No em dashes, en dashes, or hyphens as punctuation, bullets or in compound words. Hyphens inside URLs,
   emails, ids, masks and dates are fine. Lists are numbered lines.
3. Subject at most 60 characters, no ALL CAPS words (currency codes are fine), no exclamation marks, no emojis.
4. `Hello <name>,` (or `Hello,`), one clear action with one button, closing `Thank you,` plus the organisation.
5. Amounts always with a currency code and two decimals. Dates as `13 Nov 2026`. Bank details: last 4 only.
6. Where money is involved the mail states that nothing is paid until an approver signs.
7. Layout: one shared table based HTML (560px, inline CSS, text wordmark, ink header, coral button with dark text,
   muted footer with the reason and "<Org> uses PaySilo to pay contractors", no images, no tracking). Every link
   is an app URL built from `APP_BASE_URL` by code. A plain text twin always exists.

## Coverage

| Feature | Trigger | Recipient | Template | Wired in | Tested |
|---|---|---|---|---|---|
| Account | Signup with verification on | The new owner | `verify_email_otp` (SENSITIVE) | `routes/auth.py` signup, login, resend | yes |
| Account | Email verified | The new owner | `account_welcome` | `auth.verify_email` | yes |
| Account | Forgot password | The account | `password_reset_otp` (SENSITIVE) | `auth.forgot_password` | yes |
| Account | Password reset or changed | The account | `password_changed` | `auth.reset_password`, `change_password` | yes |
| Account | Invitation sent | Invited person | `invite` (SENSITIVE) | `routes/team.py` | yes |
| Account | Invitation accepted | The inviter | `invite_accepted` | `team.accept_invitation` | yes |
| Account | Role changed, member removed | The member | `role_changed`, `member_removed` | `team.py` | yes |
| Employers | Employer added | Employer contact | `employer_welcome` | `routes/employers.py` (shares the key with the seeded workflow) | yes |
| Contractors | Onboarding link sent | Contractor | `onboarding_link` (SENSITIVE) | `trust_notify.onboarding_link` | existing |
| Contractors | Contractor submits details | Owners, admins, approvers | `onboarding_submitted` | `contractor_trust.submit_onboarding` | render only |
| Contractors | Bank details changed or submitted | Approvers, employer contact | `bank_change_callback` | `trust_notify` | existing |
| Contractors | Bank details verified | Contractor (last 4 only) | `bank_verified` | `contractor_trust.verify_bank` | render only |
| Compliance | Weekly, form missing, expired or expiring | Owners, admins | `tax_form_alert` | `reminders.sweep_organization` via `compliance_mail` | render only |
| Funding | 3, 2, 1, 0 business days before fund by | Employer contact | `funding_reminder` | `reminders.py` | existing |
| Funding | Late | Employer contact | `funding_overdue` | `reminders.py` | existing |
| Funding | Statement requested | Employer contact | `funding_statement` | funding route | existing |
| Funding | Deposit arrives | Employer contact | `funds_received` | `autopilot.on_funds_arrived` | existing |
| Payroll | Run prepared and waiting (manual, scheduled, continuation) | Approvers | `approval_requested` | `runtime._announce_outcome` via `payroll_mail`; key `approval:{run}:{user}` shared with autopilot and the workflow step | yes |
| Payroll | Still waiting after N hours (3 max) | Approvers | `approval_reminder` | `payroll_mail.remind_waiting_approvals` in the sweep | yes |
| Payroll | Approved (optional, `NOTIFY_RUN_APPROVED`) | The preparer | `run_approved` | `payroll_mail.announce_run` | render only |
| Payroll | Held or rejected by an approver | Preparer and approvers | `run_rejected` | `payroll_mail.announce_run` | yes |
| Payroll | Settled | Approvers | `run_settled` | `payroll_mail.announce_run` | yes |
| Payroll | Partly paid (who is held, what to fund) | Approvers | `run_partial` | `payroll_mail.announce_run` | yes |
| Payroll | Held for lack of funds, incl. all or nothing | Approvers | `run_held` | `payroll_mail.announce_run` | render only |
| Payroll | Held lines waiting over 24h | Approvers | `held_lines_reminder` | `autopilot.remind_held_lines` | existing |
| Payroll | Risk hold (unverified bank, recent change) | Approvers | `risk_hold` | `payroll_mail.announce_run` | render only |
| Payroll | Run failed or returned (plain reason) | Approvers | `payment_failed` | `payroll_mail.announce_run` | render only |
| Payroll | Line paid | Contractor | `payslip_receipt` (SENSITIVE, with proof PDF link) | `payroll_mail.payslips`, key shared with the workflow step | yes |
| Spend | Benefit, kit or trip needs a decision | Approvers | `spend_approval_requested` | `benefits.py`, `onboarding.py`, `travel.py` | existing (kit, trip) |
| Spend | Live purchase waits for Airi | The approver who clicked Purchase | `purchase_auth_needed` (links to the Benefits page, never Airi) | `benefits._purchase_mail` | render only |
| Spend | Purchase succeeds | The requester | `purchase_receipt` | `benefits._purchase_mail` | render only |
| Spend | Kit completed, trip booked | Contractor | `kit_completed`, `trip_itinerary` | `onboarding.py`, `travel.py` | existing |
| Workflows | Send email step | Chosen recipient | any template in `SPEND_TEMPLATES`, or `custom_message` | `workflows._act_send_email` | escaping tested |
| Workflows | A workflow run fails | Owners, admins | `workflow_failed` | `workflows._fail_run` | render only |
| Workflows | Schedule invalid or occurrence missed | Owners, admins | `schedule_skipped` | `workflows.run_due_schedules` | render only |

"existing" means the event was wired and tested before this work. "render only" means wired and covered by the
style and render tests, but without a dedicated event test.

## Previews

Owners and admins: `GET /notifications/templates` (every template with sample data) and
`POST /notifications/templates/{key}/test` (sends to your own address only, 5 per hour, needs Brevo).
The dashboard shows them on Notifications, Templates tab.

## Signup and reset codes

`EMAIL_VERIFICATION=auto|on|off`. Codes are 6 digits, stored only as an HMAC, valid 10 minutes, 5 wrong tries,
60 seconds between sends, 5 per hour per email and per address, constant time compare, identical answers for
unknown emails. Sessions are stateless tokens, so a password reset cannot revoke them (the reset sends a
"password changed" notice instead).

## Adding a template

1. Write a function in `services/email_templates.py` that builds a `Mail` and returns `compose(mail)`.
2. Add a `Spec` (key, label, group, kind, sensitive, recipient, when, builder, sample) to `_specs()`.
3. Add its kind to `NOTIFICATION_KINDS` in `db/models.py` if new (the Postgres CHECK is widened at startup).
4. Queue it with a dedupe key through `notify.queue_email`. The style test and the preview pick it up
   automatically.
