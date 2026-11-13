"""The workflow 'custom message' email: a user written subject and short body, made safe.

Plain text only, length capped, variables from a fixed whitelist, links and addresses removed (the only link
in such a mail is the in app deep link that code adds as the button), control characters and angle brackets
dropped. HTML escaping is done again by the layout.
"""

from __future__ import annotations

import re

MAX_SUBJECT = 60
MAX_BODY = 600
MAX_VALUE = 80
VARIABLES = ("employer_name", "run_label", "total", "shortfall", "currency", "contractor_name", "amount",
             "organization_name", "status")
_VAR = re.compile(r"\{([a-z_]+)\}")
_LINK = re.compile(r"(?:https?://|www\.)\S+|\S+@\S+\.\S+", re.I)
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean(text: object, limit: int) -> str:
    text = "" if text is None else str(text)
    text = _CTRL.sub("", _LINK.sub("[link removed]", text)).replace("<", "").replace(">", "")
    text = text.replace("—", ", ").replace("–", ", ")
    return text.strip()[:limit]


def render_custom(subject: str, body: str, ctx: dict) -> tuple[str, str]:
    def fill(template: str, limit: int) -> str:
        def sub(match: re.Match) -> str:
            name = match.group(1)
            return clean(ctx.get(name), MAX_VALUE) if name in VARIABLES else ""
        return clean(_VAR.sub(sub, clean(template, limit * 2)), limit)

    return fill(subject, MAX_SUBJECT) or "A message from PaySilo", fill(body, MAX_BODY)
