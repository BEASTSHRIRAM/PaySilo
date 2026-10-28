"""A transparent wrapper that records every idempotent Airwallex call.

The graph talks to ``RecordingAirwallex`` exactly as it would to the
simulator or the REST client. Each call that carries a ``request_id`` is
appended to ``log`` (operation, request id, on-behalf-of account, result)
so the platform can persist an exact, replayable record per run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


def json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


@dataclass
class RecordedCall:
    operation: str
    request_id: str
    on_behalf_of: str | None
    response: dict


@dataclass
class RecordingAirwallex:
    inner: Any
    log: list[RecordedCall] = field(default_factory=list)

    def __getattr__(self, name: str) -> Any:
        target = getattr(self.inner, name)
        if not callable(target):
            return target

        def call(*args: Any, **kwargs: Any) -> Any:
            result = target(*args, **kwargs)
            request_id = kwargs.get("request_id")
            if request_id:
                self.log.append(RecordedCall(
                    operation=name, request_id=str(request_id),
                    on_behalf_of=kwargs.get("on_behalf_of") or kwargs.get("account_id")
                    or kwargs.get("source_account_id"),
                    response=json_safe(result) if isinstance(result, dict) else {"result": json_safe(result)},
                ))
            return result

        return call
