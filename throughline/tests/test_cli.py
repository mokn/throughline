"""End-to-end tests for the ``throughline`` command.

Every test runs the real CLI as a subprocess inside a real temporary git
repository, against a real local HTTP server that implements the frozen
DelegatedAccessV1 routes. Only the network boundary is substituted: the server
is in-process and nothing here touches the hosted service.
"""

import hashlib
import json
import os
import pathlib
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[2]
SECRET = re.compile(r"tl_(?:live|agent)_[A-Za-z0-9_-]{8,}")
ALL = "account:read,keys:read,keys:write,billing:read"


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Api:
    """In-memory stand-in for the hosted service, at the HTTP boundary only."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pairings = {}      # user_code -> pairing
        self.keys = {}          # sha256(feed key) -> key row
        self.agents = {}        # sha256(agent token) -> grant
        self.requests = []      # (time, method, path, headers, body)
        self.interval = 0.2
        self.expires_in = 600
        self.drop_token_replies = 0
        self.token_429 = []     # Retry-After values to serve before answering
        self.start_reply = None  # (status, headers, body) override
        self.usage_reply = None
        self.redirect_to = None
        self.slow = 0.0
        self.huge = False
        self.bad_link = False
        self.latest_calls = 0
        self.force_code = None  # a fixed user code for the next pairing
        self.drip = False       # answer with one byte at a time, forever
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    # the human side: these stand in for the signed-in browser ---------------

    def approve(self, user_code, scopes=None):
        with self.lock:
            p = self.pairings[user_code]
            granted = [s for s in p["scopes"] if scopes is None or s in scopes]
            now = time.time()
            p.update(state="approved", granted=granted, approved_at=now)
            self.keys[p["feed_key_hash"]] = dict(id=p["id"], name=p["name"], created_at=now,
                                                 revoked_at=None)
            if len(granted) > 1:
                self.agents[p["management_token_hash"]] = dict(scopes=granted,
                                                               expires_at=now + 30 * 86400)

    def deny(self, user_code):
        self.pairings[user_code]["state"] = "denied"

    def expire(self, user_code):
        self.pairings[user_code]["state"] = "expired"

    def seen(self, path):
        return [r for r in self.requests if r[2].split("?")[0] == path]

    # the wire ----------------------------------------------------------------

    def _handler(api):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body, headers=()):
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                for name, value in headers:
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(raw)

            def error(self, http, code, headers=(), **extra):
                self.reply(http, dict(error=code, message=code, request_id="ab" * 16, **extra), headers)

            def handle_any(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                headers = {k.lower(): v for k, v in self.headers.items()}
                path, _, query = self.path.partition("?")
                with api.lock:
                    api.requests.append((time.monotonic(), self.command, self.path, headers, raw))
                if api.slow:
                    time.sleep(api.slow)
                if api.redirect_to:
                    self.send_response(302)
                    self.send_header("Location", api.redirect_to + self.path)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if api.drip:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "100000")
                    self.end_headers()
                    try:
                        while api.drip:
                            self.wfile.write(b" ")
                            self.wfile.flush()
                            time.sleep(0.05)
                    except OSError:
                        pass
                    self.close_connection = True
                    return
                if api.huge:
                    return self.reply(200, b'{"plan":"' + b"x" * (2 << 20) + b'"}')
                bearer = headers.get("authorization", "")[7:]
                body = json.loads(raw) if raw else None
                route = (self.command, path)

                if route == ("POST", "/auth/v1/pairings"):
                    if api.start_reply:
                        status, extra, payload = api.start_reply
                        return self.reply(status, payload, extra)
                    assert set(body) == {"name", "scopes", "feed_key_hash", "management_token_hash"}
                    assert re.fullmatch(r"[0-9a-f]{64}", body["feed_key_hash"])
                    assert (body["management_token_hash"] is None) == (body["scopes"] == ["news:read"])
                    assert body["management_token_hash"] != body["feed_key_hash"]
                    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
                    code = api.force_code or code[:4] + "-" + code[4:]
                    device = secrets.token_urlsafe(32)
                    pairing = dict(body, id=secrets.token_hex(16), device_hash=sha(device),
                                   state="pending", granted=None)
                    with api.lock:
                        api.pairings[code] = pairing
                    account = ("https://elsewhere.example" if api.bad_link else api.base) + "/account"
                    return self.reply(200, dict(
                        pairing_id=pairing["id"], device_code=device, user_code=code,
                        verification_uri=account, verification_uri_complete=account + "#pair=" + code,
                        expires_in=api.expires_in, interval=api.interval))

                if route == ("POST", "/auth/v1/token"):
                    assert set(body) == {"device_code"}
                    with api.lock:
                        if api.token_429:
                            return self.error(429, "burst_limited",
                                              [("Retry-After", str(api.token_429.pop(0)))])
                        found = [p for p in api.pairings.values()
                                 if p["device_hash"] == sha(body["device_code"])]
                        drop = found and found[0]["state"] == "approved" and api.drop_token_replies
                        if drop:
                            api.drop_token_replies -= 1
                    if drop:  # processed, but the reply never reaches the client
                        self.close_connection = True
                        self.connection.shutdown(2)
                        return
                    if not found or found[0]["state"] == "expired":
                        return self.error(400, "expired_token", status="expired")
                    p = found[0]
                    if p["state"] == "denied":
                        return self.error(400, "access_denied", status="denied")
                    if p["state"] == "pending":
                        return self.error(400, "authorization_pending", status="pending")
                    return self.reply(200, dict(
                        status="delivered",
                        key=dict(id=p["id"], name=p["name"], created_at=p["approved_at"]),
                        connection=dict(id=p["id"], name=p["name"], scopes=p["granted"],
                                        created_at=p["approved_at"],
                                        expires_at=p["approved_at"] + 30 * 86400)))

                if path.startswith("/account/v1/"):
                    grant = api.agents.get(sha(bearer))
                    if "origin" in headers:
                        return self.error(403, "origin_forbidden")
                    if not bearer.startswith("tl_agent_") or grant is None:
                        return self.error(401, "unauthorized")

                    def need(scope):
                        if scope not in grant["scopes"]:
                            self.error(403, "insufficient_scope")
                            return False
                        return True
                    if route == ("GET", "/account/v1/me"):
                        return need("account:read") and self.reply(200, dict(
                            id="cd" * 16, plan="free", usage=dict(requests=3, request_limit=100)))
                    if route == ("GET", "/account/v1/billing/read"):
                        return need("billing:read") and self.reply(200, dict(live=False, plan="free"))
                    if route == ("GET", "/account/v1/keys"):
                        items = [dict(v) for v in api.keys.values()]
                        return need("keys:read") and self.reply(200, dict(items=items, next_cursor=None))
                    if route == ("POST", "/account/v1/keys"):
                        if not need("keys:write"):
                            return
                        key = "tl_live_" + secrets.token_urlsafe(32)
                        row = dict(id=secrets.token_hex(16), name=body["name"],
                                   created_at=time.time(), revoked_at=None)
                        api.keys[sha(key)] = row
                        return self.reply(200, dict(id=row["id"], name=row["name"],
                                                    created_at=row["created_at"], key=key))
                    if self.command == "DELETE" and path.startswith("/account/v1/keys/"):
                        if not need("keys:write"):
                            return
                        ident = path.rsplit("/", 1)[1]
                        rows = [v for v in api.keys.values() if v["id"] == ident]
                        if not rows:
                            return self.error(404, "not_found")
                        rows[0]["revoked_at"] = time.time()
                        return self.reply(200, dict(id=ident, revoked=True))
                    return self.error(404, "not_found")

                row = api.keys.get(sha(bearer))
                if not bearer.startswith("tl_live_") or row is None or row["revoked_at"]:
                    return self.error(401, "invalid_key")
                if route == ("GET", "/v1/usage"):
                    if api.usage_reply:
                        status, extra, payload = api.usage_reply
                        return self.reply(status, payload, extra)
                    return self.reply(200, dict(plan="free", usage=dict(
                        requests=1, request_limit=100, requests_remaining=99, reset_at=1)))
                if route == ("GET", "/v1/news"):
                    assert query == "cursor=latest&limit=1"
                    api.latest_calls += 1
                    return self.reply(200, dict(items=[], next_cursor=4242, has_more=False))
                return self.error(404, "not_found")

            do_GET = do_POST = do_DELETE = handle_any
        return Handler


class Case(unittest.TestCase):
    ignore = ".env\n.throughline/\nprivate/\n"

    def setUp(self):
        self.api = Api()
        self.addCleanup(self.api.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(os.path.realpath(tmp.name)) / "project"
        self.dir.mkdir()
        self.output = ""
        if self.ignore is not None:
            self.git("init", "-q", ".")
            (self.dir / ".gitignore").write_text(self.ignore)
        self.addCleanup(self.no_secret_was_printed)

    def git(self, *args):
        subprocess.run(("git", "-c", "user.name=t", "-c", "user.email=t@example.invalid") + args,
                       cwd=self.dir, check=True, capture_output=True)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith("THROUGHLINE_")}
        env.update(PYTHONPATH=str(ROOT), THROUGHLINE_API_BASE_URL=self.api.base)
        env.update(extra)
        return env

    def cli(self, *args, **extra):
        """Run the production CLI. Returns (exit code, the single JSON object)."""
        done = subprocess.run((sys.executable, "-m", "throughline.cli") + args, cwd=self.dir,
                              env=self.env(**extra), capture_output=True, text=True, timeout=60)
        return self.parse(done.returncode, done.stdout, done.stderr)

    def parse(self, code, out, err):
        self.output += out + err
        lines = out.splitlines()
        self.assertEqual(len(lines), 1, (out, err))  # exactly one JSON object
        value = json.loads(lines[0])
        self.assertIsInstance(value, dict)
        self.assertNotIn("Traceback", err)
        return code, value

    def no_secret_was_printed(self):
        self.assertIsNone(SECRET.search(self.output))
        for name in ("pending.json", "credentials.json"):
            path = self.dir / ".throughline" / name
            if path.is_file() and not path.is_symlink():
                for value in json.loads(path.read_text() or "{}").values():
                    if isinstance(value, str) and len(value) >= 32 and name == "pending.json":
                        if value.startswith("http") or re.fullmatch(r"[0-9a-f]{32}", value):
                            continue
                        self.assertNotIn(value, self.output)

    def state(self, name):
        return json.loads((self.dir / ".throughline" / name).read_text())

    def mode(self, relative):
        return stat.S_IMODE(os.lstat(self.dir / relative).st_mode)

    def env_key(self):
        lines = [x for x in (self.dir / ".env").read_text().splitlines()
                 if x.startswith("THROUGHLINE_API_KEY=")]
        self.assertEqual(len(lines), 1)
        return lines[0].split("=", 1)[1]

    def login(self, *args, scopes=None, approve=True):
        code, out = self.cli("login", *args)
        self.assertEqual((code, out["status"]), (2, "pending"), out)
        if approve:
            self.api.approve(out["user_code"], scopes)
            code, done = self.cli("login", "--wait")
            self.assertEqual((code, done["status"]), (0, "delivered"), done)
            return done
        return out


class Login(Case):
    def test_full_flow_sends_only_hashes_until_a_human_approves(self):
        code, out = self.cli("login", "--name", "bot one")
        self.assertEqual((code, out["status"]), (2, "pending"))
        self.assertRegex(out["user_code"], r"^[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$")
        self.assertEqual(out["verification_uri"], self.api.base + "/account")
        self.assertEqual(out["verification_uri_complete"],
                         self.api.base + "/account#pair=" + out["user_code"])
        pending = self.state("pending.json")
        self.assertEqual(self.mode(".throughline"), 0o700)
        self.assertEqual(self.mode(".throughline/pending.json"), 0o600)
        self.assertFalse((self.dir / ".env").exists())
        self.assertNotIn(pending["device_code"], self.output)
        (start,) = self.api.seen("/auth/v1/pairings")
        self.assertEqual(json.loads(start[4]), dict(
            name="bot one", scopes=["news:read"], feed_key_hash=sha(pending["feed_key"]),
            management_token_hash=None))
        self.assertNotIn("authorization", start[3])
        # The key does not work before approval.
        self.assertEqual(self.cli("status", THROUGHLINE_API_KEY=pending["feed_key"])[1]["error"],
                         "invalid_key")

        self.api.approve(out["user_code"])
        code, done = self.cli("login", "--wait")
        self.assertEqual((code, done["status"]), (0, "delivered"))
        self.assertEqual(done["key"]["id"], out["pairing_id"])
        self.assertEqual(done["key"]["lifetime"], "until_revoked")
        self.assertEqual(done["connection"]["scopes"], ["news:read"])
        self.assertIsNone(done["credentials_file"])
        self.assertIsNone(done["connection"]["management_expires_at"])
        self.assertEqual(self.env_key(), pending["feed_key"])
        self.assertEqual(self.mode(".env"), 0o600)
        self.assertFalse((self.dir / ".throughline/pending.json").exists())
        self.assertFalse((self.dir / ".throughline/credentials.json").exists())
        for request in self.api.requests:  # no plaintext credential before or during pickup
            self.assertNotIn(pending["feed_key"].encode(), request[4])

        code, status = self.cli("status")
        self.assertEqual((code, status["status"], status["plan"]), (0, "ok", "free"))
        self.assertEqual(self.api.seen("/v1/usage")[-1][3]["authorization"],
                         "Bearer " + pending["feed_key"])
        self.assertFalse(any("origin" in r[3] for r in self.api.requests))

    def test_documented_pending_example_has_the_real_output_shape(self):
        readme = (ROOT / "throughline/README.md").read_text()
        example = json.loads(re.search(r'```json\n(\{.*?\})\n```', readme, re.S).group(1))
        code, out = self.cli("login")
        self.assertEqual(set(example), set(out))
        self.assertEqual({k: type(v) for k, v in example.items()}, {k: type(v) for k, v in out.items()})
        for name in ("status", "name", "scopes", "pending_file", "next"):
            self.assertEqual(example[name], out[name])

    def test_wait_starts_and_finishes_in_one_command(self):
        proc = subprocess.Popen((sys.executable, "-m", "throughline.cli", "login", "--wait"),
                                cwd=self.dir, env=self.env(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        code = proc.stderr.readline().split()[2]  # the prompt goes to stderr, not the JSON
        self.api.approve(code)
        out, err = proc.communicate(timeout=30)
        result, done = self.parse(proc.returncode, out, "Approve code " + code + err)
        self.assertEqual((result, done["status"]), (0, "delivered"))
        self.assertGreaterEqual(len(self.api.seen("/auth/v1/token")), 1)

    def test_management_scopes_are_stored_apart_and_used_without_origin(self):
        done = self.login("--scopes", ALL)
        self.assertEqual(done["credentials_file"], ".throughline/credentials.json")
        self.assertEqual(self.mode(".throughline/credentials.json"), 0o600)
        saved = self.state("credentials.json")
        self.assertRegex(saved["management_token"], r"^tl_agent_[A-Za-z0-9_-]{43}$")
        self.assertNotIn("tl_agent_", (self.dir / ".env").read_text())
        self.assertEqual(done["connection"]["management_expires_at"], saved["expires_at"])
        start = json.loads(self.api.seen("/auth/v1/pairings")[0][4])
        self.assertEqual(start["management_token_hash"], sha(saved["management_token"]))

        self.assertEqual(self.cli("account")[1]["account"]["plan"], "free")
        self.assertEqual(self.cli("billing", "status")[1]["billing"], dict(live=False, plan="free"))
        code, listing = self.cli("keys", "list", "--newest")
        self.assertEqual((code, len(listing["items"])), (0, 1))
        self.assertEqual(self.api.seen("/account/v1/keys")[-1][2], "/account/v1/keys?order=newest")

        (self.dir / "private").mkdir()
        code, made = self.cli("keys", "create", "--name", "worker", "--output", "private/worker.env")
        self.assertEqual((code, made["status"], made["output"]), (0, "ok", "private/worker.env"))
        self.assertEqual(set(made["key"]), {"id", "name", "created_at"})
        text = (self.dir / "private/worker.env").read_text()
        self.assertRegex(text, r"^THROUGHLINE_API_KEY=tl_live_[A-Za-z0-9_-]{43}\n$")
        self.assertEqual(self.mode("private/worker.env"), 0o600)
        self.assertIn(sha(text.strip().split("=", 1)[1]), self.api.keys)

        before = len(self.api.requests)
        code, again = self.cli("keys", "create", "--name", "worker", "--output", "private/worker.env")
        self.assertEqual((code, again["error"]), (4, "output_exists"))
        code, loose = self.cli("keys", "create", "--name", "worker", "--output", "tracked.env")
        self.assertEqual((code, loose["error"], loose["paths"]), (4, "not_ignored", ["tracked.env"]))
        code, outside = self.cli("keys", "create", "--name", "worker", "--output", "../escape.env")
        self.assertEqual((code, outside["error"]), (4, "invalid_output"))
        self.assertEqual(len(self.api.requests), before)  # refused before any request
        self.assertEqual((self.dir / "private/worker.env").read_text(), text)
        self.assertFalse((self.dir / "tracked.env").exists())

        code, revoked = self.cli("keys", "revoke", made["key"]["id"])
        self.assertEqual((code, revoked["revoked"]), (0, True))
        self.assertEqual(self.cli("keys", "revoke", "nope")[1]["error"], "invalid_id")
        for request in self.api.requests:
            auth = request[3].get("authorization", "")
            self.assertEqual("tl_agent_" in auth, request[2].startswith("/account/v1/"))
            self.assertNotIn("origin", request[3])

    def test_narrowed_scopes_grant_only_what_the_human_approved(self):
        done = self.login("--scopes", "account:read,keys:write", scopes=["news:read"])
        self.assertEqual(done["connection"]["scopes"], ["news:read"])
        self.assertEqual(done["requested_scopes"], ["news:read", "account:read", "keys:write"])
        self.assertIsNone(done["credentials_file"])
        self.assertFalse((self.dir / ".throughline/credentials.json").exists())
        code, out = self.cli("account")
        self.assertEqual((code, out["error"]), (4, "no_management_credential"))

    def test_news_only_replacement_removes_the_older_management_credential(self):
        self.login("--scopes", ALL)
        old = self.state("credentials.json")
        old_key = self.env_key()
        self.assertEqual(self.cli("keys", "list")[0], 0)

        # Pending and denied: the connection in use keeps both of its credentials.
        pending = self.login("--replace", approve=False)
        self.assertEqual(self.state("credentials.json"), old)
        self.assertEqual(self.cli("keys", "list")[0], 0)
        self.api.deny(pending["user_code"])
        time.sleep(self.api.interval)
        code, out = self.cli("login")
        self.assertEqual((code, out["status"]), (3, "denied"))
        self.assertEqual((self.state("credentials.json"), self.env_key()), (old, old_key))

        # Delivered news-only: the older, wider token must not stay usable here.
        done = self.login("--replace")
        self.assertEqual(done["connection"]["scopes"], ["news:read"])
        self.assertTrue(done["replaced_existing_key"])
        self.assertTrue(done["removed_previous_management_credential"])
        self.assertIsNone(done["credentials_file"])
        self.assertIsNone(done["connection"]["management_expires_at"])
        self.assertFalse((self.dir / ".throughline/credentials.json").exists())
        self.assertNotEqual(self.env_key(), old_key)
        before = len(self.api.requests)
        for command in (("account",), ("keys", "list"), ("billing", "status")):
            code, out = self.cli(*command)
            self.assertEqual((code, out["error"]), (4, "no_management_credential"))
        self.assertEqual(len(self.api.requests), before)
        self.assertEqual(self.cli("status")[0], 0)

    def test_narrowed_replacement_swaps_in_the_new_connection_only(self):
        self.login("--scopes", ALL)
        old = self.state("credentials.json")
        done = self.login("--replace", "--scopes", ALL, scopes=["news:read", "account:read"])
        new = self.state("credentials.json")
        self.assertEqual(new["scopes"], ["news:read", "account:read"])
        self.assertEqual(new["connection_id"], done["connection"]["id"])
        self.assertNotEqual(new["connection_id"], old["connection_id"])
        self.assertNotEqual(new["management_token"], old["management_token"])
        self.assertFalse(done["removed_previous_management_credential"])
        self.assertEqual(self.cli("keys", "list")[1]["error"], "missing_scope")
        # Granted nothing extra although more was requested: no token is kept.
        done = self.login("--replace", "--scopes", ALL, scopes=["news:read"])
        self.assertTrue(done["removed_previous_management_credential"])
        self.assertFalse((self.dir / ".throughline/credentials.json").exists())

    def test_refused_replacement_keeps_the_management_credential(self):
        self.login("--scopes", ALL)
        old, old_key = self.state("credentials.json"), self.env_key()
        (self.dir / ".env").write_text("")
        os.chmod(self.dir / ".env", 0o600)
        pending = self.login(approve=False)          # no key present, so no --replace
        (self.dir / ".env").write_text("THROUGHLINE_API_KEY=" + old_key + "\n")
        os.chmod(self.dir / ".env", 0o600)
        self.api.approve(pending["user_code"])
        time.sleep(self.api.interval)
        code, out = self.cli("login")
        self.assertEqual((code, out["error"]), (4, "credential_exists"))
        self.assertEqual((self.state("credentials.json"), self.env_key()), (old, old_key))

    def test_production_poll_floor_cannot_be_lowered(self):
        sys.path.insert(0, str(ROOT))
        self.addCleanup(sys.path.remove, str(ROOT))
        from throughline import cli
        from throughline.client import DEFAULT_BASE_URL
        self.assertEqual(cli.MIN_INTERVAL, 5.0)
        self.assertEqual(cli._poll_floor(DEFAULT_BASE_URL), 5.0)
        self.assertEqual(cli._poll_floor(self.api.base), 0.0)
        # The override that compresses the clock is loopback only.
        code, out = self.cli("login", THROUGHLINE_API_BASE_URL="https://example.com")
        self.assertEqual(code, 4)
        self.assertEqual(self.api.requests, [])

    def test_partly_narrowed_scopes_refuse_locally_without_a_request(self):
        self.login("--scopes", ALL, scopes=["news:read", "account:read"])
        self.assertEqual(self.state("credentials.json")["scopes"], ["news:read", "account:read"])
        self.assertEqual(self.cli("account")[0], 0)
        before = len(self.api.requests)
        code, out = self.cli("keys", "list")
        self.assertEqual((code, out["error"], out["scope"]), (4, "missing_scope", "keys:read"))
        self.assertEqual(len(self.api.requests), before)

    def test_expired_management_credential_never_reaches_the_network(self):
        self.login("--scopes", ALL)
        path = self.dir / ".throughline/credentials.json"
        saved = json.loads(path.read_text())
        saved["expires_at"] = time.time() - 1
        path.write_text(json.dumps(saved))
        before = len(self.api.requests)
        code, out = self.cli("account")
        self.assertEqual((code, out["error"]), (4, "management_expired"))
        self.assertEqual(len(self.api.requests), before)
        self.assertEqual(self.cli("status")[0], 0)  # the feed key is not tied to that expiry

    def test_denial_is_definitive_and_activates_nothing(self):
        pending = self.login(approve=False)
        secret = self.state("pending.json")["feed_key"]
        self.api.deny(pending["user_code"])
        time.sleep(self.api.interval)
        code, out = self.cli("login", "--wait")
        self.assertEqual((code, out["status"]), (3, "denied"))
        self.assertIn("Nothing was activated", out["next"])
        self.assertFalse((self.dir / ".throughline/pending.json").exists())
        self.assertFalse((self.dir / ".env").exists())
        self.assertNotIn(sha(secret), self.api.keys)
        self.assertEqual(self.cli("status")[1]["error"], "no_credential")

    def test_expiry_never_claims_that_nothing_was_activated(self):
        # The server answers expired_token both for a request nobody approved and
        # for an approved one whose pickup window lapsed. The client cannot tell.
        pending = self.login(approve=False)
        before = (self.dir / ".throughline/pending.json").read_bytes()
        self.api.expire(pending["user_code"])
        time.sleep(self.api.interval)
        for _ in range(2):  # separate processes, same answer, nothing discarded
            code, out = self.cli("login", "--wait")
            self.assertEqual((code, out["status"], out["reason"]), (4, "delivery_unknown", "expired_token"))
            self.assertNotIn("Nothing was activated", json.dumps(out))
            self.assertIn("may be active", out["next"])
            self.assertEqual((out["pairing_id"], out["name"]), (pending["pairing_id"], pending["name"]))
            self.assertEqual((self.dir / ".throughline/pending.json").read_bytes(), before)
        self.assertEqual(len(self.api.pairings), 1)  # no second request minted over it
        self.assertFalse((self.dir / ".env").exists())

    def test_user_code_uses_the_server_alphabet(self):
        self.api.force_code = "8899-ZZ22"
        done = self.login()
        self.assertEqual(done["status"], "delivered")
        for bad in ("ABCI-2222", "ABCO-2222", "ABC0-2222", "ABC1-2222", "abcd-2222", "ABCD2222"):
            self.api.force_code = bad
            code, out = self.cli("login", "--replace")
            self.assertEqual((code, out["error"]), (4, "invalid_response"), bad)
            self.assertFalse((self.dir / ".throughline/pending.json").exists())

    def test_edited_pending_state_never_shows_a_different_consent_link(self):
        pending = self.login(approve=False)
        path = self.dir / ".throughline/pending.json"
        good = json.loads(path.read_text())
        for field, value in (("verification_uri_complete", "https://evil.example/account#pair=" + good["user_code"]),
                             ("verification_uri", "https://evil.example/account"),
                             ("verification_uri_complete", good["verification_uri"] + "#pair=AAAA-BBBB"),
                             ("user_code", "AAAA-BBBB"), ("device_code", "short"),
                             ("scopes", ["news:read", "news:read"]), ("name", "bad\u001bname")):
            path.write_text(json.dumps(dict(good, **{field: value})))
            before = len(self.api.requests)
            for command in (("login",), ("login", "--wait", "--max-wait", "0")):
                code, out = self.cli(*command)
                self.assertEqual((code, out["status"]), (4, "delivery_unknown"), field)
                self.assertNotIn("evil.example", json.dumps(out))
                self.assertNotIn("verification_uri", out)
            self.assertEqual(len(self.api.requests), before)
        path.write_text(json.dumps(good))
        self.api.approve(pending["user_code"])
        time.sleep(self.api.interval)
        self.assertEqual(self.cli("login")[1]["status"], "delivered")

    def test_server_link_off_the_pinned_origin_is_never_shown_or_saved(self):
        self.api.bad_link = True
        code, out = self.cli("login")
        self.assertEqual((code, out["error"]), (4, "invalid_response"))
        self.assertNotIn("elsewhere.example", self.output)
        self.assertFalse((self.dir / ".throughline/pending.json").exists())


class Recovery(Case):
    def test_lost_metadata_reply_is_recovered_from_local_pending_secrets(self):
        pending = self.login(approve=False)
        secret = self.state("pending.json")["feed_key"]
        self.api.approve(pending["user_code"])
        self.api.drop_token_replies = 1
        time.sleep(self.api.interval)
        code, out = self.cli("login")
        self.assertEqual((code, out["error"]), (4, "network_error"))
        self.assertEqual(self.state("pending.json")["feed_key"], secret)  # still held locally
        self.assertFalse((self.dir / ".env").exists())
        code, done = self.cli("login")  # a new process: same request, same key
        self.assertEqual((code, done["status"], done["key"]["id"]), (0, "delivered", pending["pairing_id"]))
        self.assertEqual(self.env_key(), secret)
        self.assertEqual(len(self.api.pairings), 1)
        self.assertEqual(len(self.api.keys), 1)
        self.assertEqual(self.cli("status")[0], 0)

    def test_approved_request_picked_up_after_the_receipt_window_keeps_the_only_key(self):
        pending = self.login(approve=False)
        path = self.dir / ".throughline/pending.json"
        secret = self.state("pending.json")["feed_key"]
        self.api.approve(pending["user_code"])
        self.api.drop_token_replies = 1
        time.sleep(self.api.interval)
        self.assertEqual(self.cli("login")[1]["error"], "network_error")  # the reply was lost
        self.api.expire(pending["user_code"])  # more than 24 hours pass on the server
        self.assertIn(sha(secret), self.api.keys)  # the approved key is live
        code, out = self.cli("login", "--wait")
        self.assertEqual((code, out["status"], out["reason"]), (4, "delivery_unknown", "expired_token"))
        self.assertTrue(out["local_state_kept"])
        self.assertIn("may be active", out["next"])
        self.assertIn(pending["pairing_id"], out["next"])
        self.assertIn("revoke", out["next"])
        self.assertNotIn("Nothing was activated", json.dumps(out))
        # The only copy of the live key is still on disk, private, and not in .env.
        self.assertEqual(self.state("pending.json")["feed_key"], secret)
        self.assertEqual(self.mode(".throughline/pending.json"), 0o600)
        self.assertFalse((self.dir / ".env").exists())
        self.assertEqual(self.cli("status")[1]["error"], "no_credential")
        # Discarding it is an explicit owner decision that names what to revoke.
        code, out = self.cli("cancel")
        self.assertEqual((code, out["status"], out["delivery"]), (3, "cancelled", "unknown"))
        self.assertIn(pending["pairing_id"], out["next"])
        self.assertIn("revoke", out["next"])
        self.assertFalse(path.exists())
        self.assertEqual(self.cli("login")[1]["status"], "pending")

    def test_interrupted_finalization_finishes_the_same_files(self):
        pending = self.login("--scopes", ALL, approve=False)
        before = self.state("pending.json")
        self.api.approve(pending["user_code"])
        (self.dir / "elsewhere").write_text("untouched\n")
        os.symlink("elsewhere", self.dir / ".env")  # finalization cannot complete
        time.sleep(self.api.interval)
        code, out = self.cli("login")
        self.assertEqual((code, out["error"], out["reason"]), (4, "unsafe_file", "symlink"))
        self.assertEqual((self.dir / "elsewhere").read_text(), "untouched\n")
        self.assertEqual(self.state("pending.json")["feed_key"], before["feed_key"])
        # Refused before anything was written: no half-installed credential set.
        self.assertFalse((self.dir / ".throughline/credentials.json").exists())
        os.unlink(self.dir / ".env")
        code, done = self.cli("login")
        self.assertEqual((code, done["status"]), (0, "delivered"))
        self.assertEqual(self.env_key(), before["feed_key"])
        self.assertEqual(self.state("credentials.json")["management_token"], before["management_token"])
        self.assertFalse((self.dir / ".throughline/pending.json").exists())
        self.assertEqual(sorted(p.name for p in (self.dir / ".throughline").iterdir()),
                         ["credentials.json", "lock"])
        self.assertEqual(self.cli("account")[0], 0)

    def test_finishing_twice_after_the_env_write_is_a_no_op(self):
        pending = self.login(approve=False)
        saved = (self.dir / ".throughline/pending.json").read_bytes()
        self.api.approve(pending["user_code"])
        time.sleep(self.api.interval)
        self.assertEqual(self.cli("login")[0], 0)
        # Simulate a crash after .env was written but before pending state was cleared.
        path = self.dir / ".throughline/pending.json"
        path.write_bytes(saved)
        os.chmod(path, 0o600)
        env = (self.dir / ".env").read_bytes()
        code, done = self.cli("login")
        self.assertEqual((code, done["status"], done["replaced_existing_key"]), (0, "delivered", False))
        self.assertEqual((self.dir / ".env").read_bytes(), env)

    def test_competing_login_is_refused_and_a_killed_wait_resumes(self):
        proc = subprocess.Popen((sys.executable, "-m", "throughline.cli", "login", "--wait"),
                                cwd=self.dir, env=self.env(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        code = proc.stderr.readline().split()[2]
        saved = (self.dir / ".throughline/pending.json").read_text()
        rc, out = self.cli("login", "--replace", "--name", "second")
        self.assertEqual((rc, out["error"]), (4, "state_locked"))
        self.assertEqual(self.cli("cancel")[1]["error"], "state_locked")
        self.assertEqual(json.loads(saved)["feed_key"], self.state("pending.json")["feed_key"])
        self.assertEqual(len(self.api.seen("/auth/v1/pairings")), 1)

        proc.send_signal(signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=30)
        rc, out = self.parse(proc.returncode, stdout, stderr)
        self.assertEqual((rc, out["status"], out["local_state_kept"]), (3, "cancelled", True))
        self.assertEqual(json.loads(saved)["feed_key"], self.state("pending.json")["feed_key"])

        self.api.approve(code)
        time.sleep(self.api.interval)
        rc, done = self.cli("login")
        self.assertEqual((rc, done["status"]), (0, "delivered"))
        self.assertEqual(self.env_key(), json.loads(saved)["feed_key"])

        hard = subprocess.Popen((sys.executable, "-m", "throughline.cli", "login", "--wait", "--replace"),
                                cwd=self.dir, env=self.env(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        second = hard.stderr.readline().split()[2]
        hard.kill()
        hard.communicate(timeout=30)
        self.api.approve(second)
        time.sleep(self.api.interval)
        rc, done = self.cli("login")  # the lock died with the process; the secrets did not
        self.assertEqual((rc, done["status"], done["replaced_existing_key"]), (0, "delivered", True))

    def test_unreadable_pending_state_is_delivery_unknown_until_cancelled(self):
        self.login(approve=False)
        path = self.dir / ".throughline/pending.json"
        path.write_text('{"version": 1}')
        before = len(self.api.requests)
        code, out = self.cli("login", "--wait")
        self.assertEqual((code, out["status"]), (4, "delivery_unknown"))
        self.assertNotIn("error", out)
        self.assertEqual(len(self.api.requests), before)  # no new request was minted over it
        code, out = self.cli("cancel")
        self.assertEqual((code, out["status"]), (3, "cancelled"))
        self.assertFalse(path.exists())
        self.assertEqual(self.cli("login")[1]["status"], "pending")

    def test_cancel_is_local_and_refuses_after_approval(self):
        self.assertEqual(self.cli("cancel")[1]["error"], "no_pending_request")
        pending = self.login(approve=False)
        code, out = self.cli("cancel")
        self.assertEqual((code, out["status"], out["user_code"]), (3, "cancelled", pending["user_code"]))
        self.assertFalse((self.dir / ".throughline/pending.json").exists())
        self.assertFalse((self.dir / ".env").exists())

        second = self.login(approve=False)
        secret = self.state("pending.json")["feed_key"]
        self.api.approve(second["user_code"])
        code, out = self.cli("cancel")  # cancelling now would strand a live key
        self.assertEqual((code, out["status"], out["cancel"]), (0, "delivered", "refused_already_approved"))
        self.assertEqual(self.env_key(), secret)


class Limits(Case):
    def test_retry_after_is_honoured_across_commands_and_while_waiting(self):
        pending = self.login(approve=False)
        self.api.token_429 = [1]
        time.sleep(self.api.interval)
        code, out = self.cli("login")
        self.assertEqual((code, out["status"], out["retry_after"]), (2, "slow_down", 1.0))
        polls = len(self.api.seen("/auth/v1/token"))
        code, out = self.cli("login")  # too soon: answered locally
        self.assertEqual((code, out["status"], out["polled"]), (2, "pending", False))
        self.assertEqual(len(self.api.seen("/auth/v1/token")), polls)

        self.api.token_429 = [1]
        self.api.approve(pending["user_code"])
        code, done = self.cli("login", "--wait")
        self.assertEqual((code, done["status"]), (0, "delivered"))
        times = [r[0] for r in self.api.seen("/auth/v1/token")]
        self.assertGreaterEqual(times[-1] - times[-2], 1.0)
        self.assertGreaterEqual(times[-2] - times[-3], 0.9)

    def test_pending_polls_keep_the_published_interval(self):
        self.api.interval = 0.5
        self.login(approve=False)
        code, out = self.cli("login", "--wait", "--max-wait", "1.8")
        self.assertEqual((code, out["status"]), (2, "pending"))
        times = [r[0] for r in self.api.seen("/auth/v1/token")]
        self.assertTrue(2 <= len(times) <= 4, times)
        self.assertTrue(all(b - a >= 0.5 for a, b in zip(times, times[1:])), times)

    def test_capacity_denial_at_start_prints_the_private_file_fallback(self):
        self.api.start_reply = (429, [("Retry-After", "120")], dict(error="pairing_capacity"))
        code, out = self.cli("login")
        self.assertEqual((code, out["error"], out["retry_after"]), (4, "pairing_capacity", 120.0))
        self.assertIn("https://api.throughline.markets/account", out["fallback"])
        self.assertFalse((self.dir / ".throughline/pending.json").exists())
        self.assertEqual(len(self.api.seen("/auth/v1/pairings")), 1)  # no automatic retry

    def test_quota_error_is_sanitized_and_actionable(self):
        self.login()
        self.api.usage_reply = (429, [("Retry-After", "30")], dict(
            error="account_quota_exceeded", message="tl_live_" + "A" * 43,
            upgrade_url="https://api.throughline.markets/account#billing", reset_at=1700000000,
            request_id="ab" * 16, usage=dict(secret="x")))
        code, out = self.cli("status")
        self.assertEqual(code, 4)
        self.assertEqual({k: out[k] for k in out if k != "message"}, dict(
            status="error", error="account_quota_exceeded", http_status=429, retry_after=30.0,
            upgrade_url="https://api.throughline.markets/account#billing", reset_at=1700000000,
            request_id="ab" * 16))
        self.api.usage_reply = (500, [], b"<html>stack trace with /srv/path</html>")
        code, out = self.cli("status")
        self.assertEqual((code, out["error"]), (4, "http_500"))
        self.assertNotIn("srv", json.dumps(out))
        self.api.usage_reply = (429, [], dict(error="x", upgrade_url="https://evil.example/pay"))
        self.assertNotIn("upgrade_url", self.cli("status")[1])

    def test_response_size_and_time_are_bounded(self):
        self.login()
        self.api.huge = True
        code, out = self.cli("status")
        self.assertEqual((code, out["error"]), (4, "response_too_large"))
        self.api.huge, self.api.slow = False, 3
        started = time.monotonic()
        code, out = self.cli("status", "--timeout", "0.5")
        self.assertEqual((code, out["error"]), (4, "network_error"))
        self.assertLess(time.monotonic() - started, 2.5)

    def test_trickling_reply_cannot_outlast_the_whole_request_deadline(self):
        self.login()
        self.api.drip = True  # every socket read succeeds; only a wall clock stops this
        self.addCleanup(setattr, self.api, "drip", False)
        started = time.monotonic()
        code, out = self.cli("status", "--timeout", "1")
        self.assertEqual((code, out["error"]), (4, "network_error"))
        self.assertLess(time.monotonic() - started, 3)
        pending_started = time.monotonic()
        code, out = self.cli("login", "--replace", "--timeout", "1")
        self.assertEqual((code, out["error"]), (4, "network_error"))
        self.assertLess(time.monotonic() - pending_started, 3)
        self.assertFalse((self.dir / ".throughline/pending.json").exists())

    def test_redirects_are_refused_and_no_credential_is_forwarded(self):
        self.login("--scopes", ALL)
        other = Api()
        self.addCleanup(other.close)
        self.api.redirect_to = other.base
        for command in (("status",), ("account",), ("keys", "list"), ("start-from-now",)):
            code, out = self.cli(*command)
            self.assertEqual((code, out["status"]), (4, "error"), command)
        self.assertEqual(self.cli("status")[1]["error"], "redirect_refused")
        code, out = self.cli("login", "--replace")
        self.assertEqual((code, out["error"]), (4, "redirect_refused"))
        self.assertEqual(other.requests, [])

    def test_only_the_pinned_origin_or_loopback_is_accepted(self):
        for target in ("https://evil.example", "http://192.0.2.1:8080", "https://api.throughline.markets.evil.example",
                       "http://127.0.0.1@evil.example", "http://127.0.0.1:1/path", "ftp://127.0.0.1"):
            with self.subTest(target=target):
                code, out = self.cli("status", THROUGHLINE_API_BASE_URL=target, THROUGHLINE_API_KEY="tl_live_" + "A" * 43)
                self.output = ""  # the fake key above is test input, not CLI output
                self.assertEqual((code, out["error"]), (4, "invalid_base_url"))
        self.assertEqual(self.api.requests, [])
        self.assertNotIn("--base-url", subprocess.run(
            (sys.executable, "-m", "throughline.cli", "login", "--help"), cwd=self.dir,
            env=self.env(), capture_output=True, text=True).stdout)

    def test_start_from_now_captures_the_head_once(self):
        self.login()
        code, out = self.cli("start-from-now")
        self.assertEqual((code, out["cursor"], out["captured"]), (0, 4242, True))
        self.assertEqual(self.state("cursor.json")["cursor"], 4242)
        self.assertEqual(self.mode(".throughline/cursor.json"), 0o600)
        for _ in range(2):
            code, out = self.cli("start-from-now")
            self.assertEqual((code, out["cursor"], out["captured"]), (0, 4242, False))
        self.assertEqual(self.api.latest_calls, 1)  # never repeated on resume


class Files(Case):
    def refused(self, error, *args, **fields):
        before = len(self.api.requests)
        code, out = self.cli(*(args or ("login",)))
        self.assertEqual((code, out.get("error")), (4, error), out)
        for name, value in fields.items():
            self.assertEqual(out[name], value)
        self.assertEqual(len(self.api.requests), before)  # nothing was sent
        return out

    def test_symlinked_env_is_refused_and_its_target_untouched(self):
        (self.dir / "real").write_text("KEEP=1\n")
        os.chmod(self.dir / "real", 0o600)
        os.symlink("real", self.dir / ".env")
        self.refused("unsafe_file", path=".env", reason="symlink")
        self.assertEqual((self.dir / "real").read_text(), "KEEP=1\n")

    def test_unsafe_mode_hardlink_and_non_regular_env_are_refused(self):
        env = self.dir / ".env"
        env.write_text("OTHER=1\n")
        os.chmod(env, 0o644)
        self.refused("unsafe_file", path=".env", reason="mode")
        self.assertEqual(self.mode(".env"), 0o644)  # reported, never silently changed
        os.chmod(env, 0o600)
        os.link(env, self.dir / "alias")
        self.refused("unsafe_file", reason="hardlink")
        os.unlink(self.dir / "alias")
        os.unlink(env)
        os.mkfifo(env, 0o600)
        self.refused("unsafe_file", reason="not_regular")

    def test_unsafe_state_directory_is_refused(self):
        state = self.dir / ".throughline"
        state.mkdir(mode=0o755)
        os.chmod(state, 0o755)
        self.refused("unsafe_state_directory")
        state.rmdir()
        (self.dir / "private").mkdir(mode=0o700)
        os.symlink("private", state)
        self.refused("unsafe_state_directory")
        self.assertEqual(list((self.dir / "private").iterdir()), [])

    def test_symlinked_pending_and_credentials_are_refused(self):
        self.login("--scopes", ALL)
        saved = self.dir / ".throughline/credentials.json"
        os.rename(saved, self.dir / "private-copy")
        os.symlink("../private-copy", saved)
        self.refused("unsafe_file", "account", reason="symlink")
        os.symlink("../private-copy", self.dir / ".throughline/pending.json")
        self.refused("unsafe_file", "login", "--replace", reason="symlink")

    def test_tracked_env_is_refused(self):
        (self.dir / ".gitignore").write_text(".throughline/\n")
        (self.dir / ".env").write_text("OTHER=1\n")
        os.chmod(self.dir / ".env", 0o600)
        self.git("add", ".env")
        self.git("commit", "-q", "-m", "oops")
        (self.dir / ".gitignore").write_text(self.ignore)  # ignoring it later does not untrack it
        self.refused("tracked_file", paths=[".env"])

    def test_unignored_files_are_refused_and_gitignore_is_never_edited(self):
        (self.dir / ".gitignore").write_text("*.log\n")
        out = self.refused("not_ignored", paths=[".env", ".throughline/credentials.json",
                                                 ".throughline/pending.json", ".throughline/cursor.json"])
        self.assertEqual(out["gitignore_lines"], [".env", ".throughline/"])
        self.assertEqual((self.dir / ".gitignore").read_text(), "*.log\n")
        self.assertFalse((self.dir / ".throughline").exists())
        self.refused("not_ignored", "login", "--no-git")  # the flag is not a bypass inside a repo
        self.refused("not_ignored", "start-from-now")

    def test_tracked_state_files_are_refused_by_every_command(self):
        self.login("--scopes", ALL)
        self.assertEqual(self.cli("start-from-now")[0], 0)
        for name, commands in (("credentials.json", (("account",), ("keys", "list"), ("billing", "status"),
                                                      ("status",), ("login", "--replace"))),
                               ("cursor.json", (("start-from-now",), ("status",)))):
            self.git("add", "-f", ".throughline/" + name)
            for command in commands:
                self.refused("tracked_file", *command, paths=[".throughline/" + name])
            self.git("rm", "-q", "--cached", ".throughline/" + name)
        pending = self.login("--replace", approve=False)
        self.git("add", "-f", ".throughline/pending.json")
        self.api.approve(pending["user_code"])
        for command in (("login",), ("login", "--wait"), ("cancel",)):
            self.refused("tracked_file", *command, paths=[".throughline/pending.json"])
        self.git("rm", "-q", "--cached", ".throughline/pending.json")
        time.sleep(self.api.interval)
        self.assertEqual(self.cli("login")[1]["status"], "delivered")

    def test_readers_refuse_an_unignored_env_file(self):
        self.login()
        (self.dir / ".gitignore").write_text(".throughline/\n")
        self.refused("not_ignored", "status", paths=[".env"])
        self.refused("not_ignored", "cancel", paths=[".env"])

    def test_duplicate_variable_is_refused(self):
        env = self.dir / ".env"
        env.write_text("THROUGHLINE_API_KEY=\nexport THROUGHLINE_API_KEY = x\n")
        os.chmod(env, 0o600)
        self.refused("duplicate_variable")
        self.refused("duplicate_variable", "status")

    def test_existing_key_needs_replace_and_other_lines_survive_byte_for_byte(self):
        old = "tl_live_" + "B" * 43
        env = self.dir / ".env"
        head, tail = b"# comment\r\nA=1\n\n  spaced = two words \n", b"\nexport Z='q'\n# no trailing newline"
        env.write_bytes(head + b"export THROUGHLINE_API_KEY = '" + old.encode() + b"'" + tail)
        os.chmod(env, 0o600)
        self.output = ""
        self.refused("credential_exists")
        self.assertFalse((self.dir / ".throughline/pending.json").exists())
        done = self.login("--replace")
        self.assertTrue(done["replaced_existing_key"])
        self.assertIn("still active", done["note"])
        new = self.env_key()
        self.assertNotEqual(new, old)
        self.assertEqual(env.read_bytes(), head + b"THROUGHLINE_API_KEY=" + new.encode() + tail)
        self.assertEqual(self.mode(".env"), 0o600)

    def test_key_is_appended_to_an_existing_env_without_a_trailing_newline(self):
        env = self.dir / ".env"
        env.write_bytes(b"A=1\nTHROUGHLINE_API_KEYS=not-ours")
        os.chmod(env, 0o600)
        self.login()
        self.assertEqual(env.read_bytes(), b"A=1\nTHROUGHLINE_API_KEYS=not-ours\nTHROUGHLINE_API_KEY="
                         + self.env_key().encode() + b"\n")

    def test_replace_decided_after_start_still_needs_the_flag(self):
        pending = self.login(approve=False)
        env = self.dir / ".env"
        old = "tl_live_" + "C" * 43
        env.write_text("THROUGHLINE_API_KEY=" + old + "\n")
        os.chmod(env, 0o600)
        self.api.approve(pending["user_code"])
        time.sleep(self.api.interval)
        code, out = self.cli("login")
        self.assertEqual((code, out["error"]), (4, "credential_exists"))
        self.assertEqual(env.read_text(), "THROUGHLINE_API_KEY=" + old + "\n")  # no silent overwrite
        time.sleep(self.api.interval)
        code, done = self.cli("login", "--replace")
        self.assertEqual((code, done["replaced_existing_key"]), (0, True))
        self.output = self.output.replace(old, "")

    def test_credentials_in_arguments_are_refused_without_echo(self):
        token = "tl_live_" + "D" * 43
        for args in (("login", "--name", token), ("keys", "revoke", "tl_agent_" + "E" * 43),
                     ("status", "--key=" + token)):
            out = self.refused("secret_in_arguments", *args)
            self.assertNotIn("D" * 10, json.dumps(out))
        code, out = self.cli("login", "--unknown-flag", "value-that-must-not-echo")
        self.assertEqual((code, out["error"]), (4, "usage"))
        self.assertNotIn("must-not-echo", self.output)
        self.assertEqual(self.cli("login", "--scopes", "root")[1]["error"], "invalid_scopes")
        self.assertEqual(self.cli("login", "--name", "bad\nname")[1]["error"], "invalid_name")
        self.assertEqual(self.api.requests, [])


class NoGit(Case):
    ignore = None

    def test_no_repository_needs_an_explicit_acknowledgement(self):
        env = dict(GIT_CEILING_DIRECTORIES=str(self.dir.parent))
        code, out = self.cli("login", **env)
        self.assertEqual((code, out["error"]), (4, "git_required"))
        self.assertEqual(self.api.requests, [])
        self.assertFalse((self.dir / ".throughline").exists())
        code, out = self.cli("login", "--no-git", **env)
        self.assertEqual((code, out["status"]), (2, "pending"))
        self.api.approve(out["user_code"])
        code, out = self.cli("login", "--wait", "--no-git", **env)
        self.assertEqual((code, out["status"]), (0, "delivered"))
        self.assertEqual(self.mode(".env"), 0o600)
        # Reading the private key file needs the same acknowledgement as writing it.
        code, out = self.cli("status", **env)
        self.assertEqual((code, out["error"]), (4, "git_required"))
        self.assertEqual(self.cli("status", "--no-git", **env)[0], 0)


if __name__ == "__main__":
    unittest.main()
