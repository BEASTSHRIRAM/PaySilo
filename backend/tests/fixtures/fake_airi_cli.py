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
  FAKE_LOGIN      scenario for `auth login` / `auth poll` (stateful via files in FAKE_DIR):
                  unset/"approve" -> login prints a code; poll approves (after FAKE_POLL_PENDING
                                     "authorization_pending" answers, default 0)
                  "pending" -> poll always answers authorization_pending
                  "slow"    -> poll sleeps 5s (to exercise the caller's short timeout)
                  "denied" / "expired" -> poll fails with access_denied / expired_token
                  "garbage" -> login prints non-JSON;  "evil" -> login returns an off-domain URL
                  "noconn"  -> login exits non-zero
"""

import json
import os
import sys
import time

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


def _exists(name):
    return os.path.exists(os.path.join(d, name))


def _touch(name, text=""):
    with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
        fh.write(text)


def _signed_in():
    if _exists("signed_in"):
        return True
    if _exists("signed_out"):
        return False
    return os.environ.get("FAKE_AUTH") != "out"


if args[:2] == ["auth", "whoami"]:
    if not _signed_in():
        out({"authenticated": False})
    out({"authenticated": True, "session_expires": "2026-12-01T00:00:00Z",
         "email": "user@example.test", "client_name": "CLI", "phone_number": "5550100",
         "country_calling_code": "1"})

if args[:2] == ["auth", "login"]:
    mode = os.environ.get("FAKE_LOGIN", "approve")
    if mode == "noconn":
        sys.stderr.write("Error: cannot reach the authorization server\n")
        sys.exit(1)
    if mode == "garbage":
        print("Welcome to airi! Open your browser to continue.")
        sys.exit(0)
    n = len([ln for ln in open(os.path.join(d, "calls.jsonl"), encoding="utf-8") if '"login"' in ln])
    host = "https://evil.example" if mode == "evil" else "https://app.airi.com"
    _touch("pending.json", json.dumps({"n": n, "client_name": flag("--client-name")}))
    out({"verification_uri": f"{host}/device", "user_code": f"WXYZ-{n:04d}", "expires_in": 600,
         "interval": 5})

if args[:2] == ["auth", "poll"]:
    mode = os.environ.get("FAKE_LOGIN", "approve")
    if not _exists("pending.json"):
        sys.stderr.write("Error: no login in progress\n")
        sys.exit(1)
    if mode == "slow":
        time.sleep(5)
    if mode == "pending":
        out({"status": "authorization_pending"}, 1)
    if mode == "denied":
        os.remove(os.path.join(d, "pending.json"))
        sys.stderr.write("Error: access_denied\n")
        sys.exit(1)
    if mode == "expired":
        os.remove(os.path.join(d, "pending.json"))
        sys.stderr.write("Error: expired_token\n")
        sys.exit(1)
    left = int(os.environ.get("FAKE_POLL_PENDING", "0"))
    done = len([ln for ln in open(os.path.join(d, "calls.jsonl"), encoding="utf-8")
                if '"poll"' in ln])
    if done <= left:
        out({"status": "authorization_pending"}, 1)
    os.remove(os.path.join(d, "pending.json"))
    _touch("signed_in")
    if _exists("signed_out"):
        os.remove(os.path.join(d, "signed_out"))
    out({"authenticated": True, "session_expires": "2026-12-01T00:00:00Z"})

if args[:2] == ["auth", "logout"]:
    for f in ("signed_in", "pending.json"):
        if _exists(f):
            os.remove(os.path.join(d, f))
    _touch("signed_out")
    out({"status": "ok"})

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
