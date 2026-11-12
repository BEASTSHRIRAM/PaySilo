"""A FAKE ``airi-cli`` for tests — emits the JSON shapes of the real CLI.

Shapes follow the public README / skills of airwallex/airi-cli v0.1.0-beta.4
(and read-only checks against a real signed-in CLI). All values are fake.

Behaviour is driven by env vars so one script covers every scenario:
  FAKE_DIR        directory where every call is journaled (required)
  FAKE_AUTH       "out" -> not signed in
  FAKE_PM         "empty" -> no agent-enabled payment methods
  FAKE_CRED       "pending" -> get-credential times out (human has not authorized)
                  "error"   -> get-credential fails (mandate denied)
  FAKE_RESULT_FAIL "1" -> `mandate result` exits non-zero
"""

import json
import os
import sys

FAKE_PAN = "4111111111111111"  # the published test PAN; must NEVER reach DB / logs / API

args = sys.argv[1:]
d = os.environ["FAKE_DIR"]
with open(os.path.join(d, "calls.jsonl"), "a", encoding="utf-8") as fh:
    fh.write(json.dumps(args) + "\n")


def out(obj, code=0):
    print(json.dumps(obj))
    sys.exit(code)


def flag(name):
    return args[args.index(name) + 1] if name in args else None


if args[:2] == ["auth", "whoami"]:
    if os.environ.get("FAKE_AUTH") == "out":
        out({"authenticated": False})
    out({"authenticated": True, "session_expires": "2026-12-01T00:00:00Z",
         "email": "user@example.test", "client_name": "CLI"})

if args[:2] == ["payment-methods", "list"]:
    if os.environ.get("FAKE_PM") == "empty":
        out([])
    out([{"asDefault": True, "type": "CARD", "nickName": "Test Card",
          "paymentMethodId": "pm-fake-0001-aaaa", "createdAt": "2026-11-12T08:00:00Z",
          "card": {"brand": "visa", "last4": "1111", "expiryMonth": "02", "expiryYear": "2032",
                   "name": "Test Person",
                   "billingAddress": {"city": "Testville", "countryCode": "US", "postcode": "00000",
                                      "state": "TS", "street": "1 Fake St"}}}])

if args[:2] == ["mandate", "create"]:
    with open(os.path.join(d, "create.json"), "w", encoding="utf-8") as fh:
        json.dump(args, fh)
    n = len([ln for ln in open(os.path.join(d, "calls.jsonl"), encoding="utf-8")
             if '"create"' in ln])
    out({"mandate_id": f"mdt_fake_{n}", "status": "CREATED",
         "passkey_url": f"https://app.airi.example/passkey/mdt_fake_{n}"})

if args[:2] == ["mandate", "get-credential"]:
    mode = os.environ.get("FAKE_CRED")
    if mode == "pending":
        sys.stderr.write("Error: Timed out after 1s waiting for credential authorization.\n")
        sys.exit(1)
    if mode == "error":
        sys.stderr.write("Error: mandate was denied by the user\n")
        sys.exit(1)
    path = flag("--file")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"pan": FAKE_PAN, "expiry": "02/32", "cvv": "123"}, fh)
    with open(os.path.join(d, "credpath.txt"), "w", encoding="utf-8") as fh:
        fh.write(path)
    out({"status": "READY", "tokenType": "NETWORK_TOKEN",
         "tokenExpiration": "2026-11-12T20:00:00+05:30"})

if args[:2] == ["mandate", "result"]:
    with open(os.path.join(d, "result.json"), "w", encoding="utf-8") as fh:
        json.dump(args, fh)
    if os.environ.get("FAKE_RESULT_FAIL") == "1":
        sys.stderr.write("Error: boom\n")
        sys.exit(1)
    out({"status": "ok"})

if args[:2] == ["mandate", "cancel"]:
    with open(os.path.join(d, "cancel.json"), "w", encoding="utf-8") as fh:
        json.dump(args, fh)
    out({"status": "ok"})

sys.stderr.write("unknown command\n")
sys.exit(2)
