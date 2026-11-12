"""``throughline`` command line: delegated setup without pasting a key into chat.

POSIX, Python 3.10+, standard library only. Every command prints exactly one
JSON object on stdout and never prints a credential. Secrets are generated
here, kept in private project files and sent only as SHA-256 hashes until a
signed-in human approves the request in the account portal.

Private files, all relative to the project directory:

* ``.env``                         ``THROUGHLINE_API_KEY=...`` (feed key)
* ``.throughline/pending.json``    locally generated secrets awaiting approval
* ``.throughline/credentials.json`` management credential, if scopes were granted
* ``.throughline/cursor.json``     the start-from-now cursor, captured once

These files stop a key from landing in chat, logs or source control. They do
not hide it from a shell agent doing the setup, which can read local files.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import ipaddress
import json
import os
import random
import re
import secrets
import signal
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPException
from typing import Any, NoReturn

from .client import (
    DEFAULT_BASE_URL,
    _NoRedirect,
    _retry_after,
)

__all__ = ["main"]

VERSION = "0.3.0"
_UA = f"throughline-cli/{VERSION} (+https://throughline.markets/api)"
BASE_URL_ENV = "THROUGHLINE_API_BASE_URL"
KEY_ENV = "THROUGHLINE_API_KEY"

SCOPES = ("news:read", "account:read", "keys:read", "keys:write", "billing:read")
FEED_KEY = re.compile(r"tl_live_[A-Za-z0-9_-]{43}\Z")
AGENT_TOKEN = re.compile(r"tl_agent_[A-Za-z0-9_-]{43}\Z")
_SECRET = re.compile(r"tl_(?:live|agent)_[A-Za-z0-9_-]{8,}")
_IDENT = re.compile(r"[0-9a-f]{32}\Z")
# The server alphabet: 32 symbols, no I, O, 0 or 1.
_USER_CODE = re.compile(r"[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}\Z")
_DEVICE_CODE = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
_CODE = re.compile(r"[a-z0-9_]{1,64}\Z")
_ENV_LINE = re.compile(rb"[ \t]*(?:export[ \t]+)?THROUGHLINE_API_KEY[ \t]*=(.*)\Z", re.S)

MAX_RESPONSE = 1 << 20
MAX_FILE = 1 << 20
MIN_INTERVAL = 5.0
STATE_DIR = ".throughline"
PRIVATE_FILES = ("credentials.json", "pending.json", "cursor.json", "key-create.json", "disconnect.json")
ENV_FILE = ".env"
FALLBACK = ("Sign in at https://api.throughline.markets/account, create a key and save "
            "its .env line to a private, git-ignored .env file. Do not paste it into chat.")

EXIT = {"ok": 0, "delivered": 0, "pending": 2, "slow_down": 2, "denied": 3,
        "cancelled": 3, "error": 4, "delivery_unknown": 4}

MESSAGES = {
    "usage": "Invalid command line. Run 'throughline --help'.",
    "secret_in_arguments": "A credential was passed on the command line. Nothing was sent. "
                           "Credentials are only read from private files.",
    "invalid_base_url": f"{BASE_URL_ENV} may only point at a loopback address for tests.",
    "invalid_project": "The project directory does not exist or is not a directory.",
    "git_required": "This directory is not inside a git work tree, so ignore rules cannot be "
                    "checked. Pass --no-git only if that is intentional.",
    "tracked_file": "A private file is tracked by git. Untrack it before continuing.",
    "not_ignored": "A private file is not git-ignored. Add the listed lines to .gitignore yourself.",
    "unsafe_file": "A private file is a symlink, is not a regular file, has another owner or "
                   "is readable by group/others.",
    "unsafe_state_directory": ".throughline must be a real directory owned by you with mode 0700.",
    "state_locked": "Another throughline command is using this project. Wait for it to finish.",
    "duplicate_variable": ".env has more than one THROUGHLINE_API_KEY line. Leave exactly one.",
    "credential_exists": ".env already has a THROUGHLINE_API_KEY. Pass --replace to store a new "
                         "one; the old key stays active until it is revoked.",
    "invalid_credential": "The stored credential is not a valid Throughline credential.",
    "no_credential": "No feed key found. Run 'throughline login'.",
    "no_management_credential": "No management credential is stored. Run 'throughline login "
                                "--scopes ...' and approve those scopes.",
    "management_expired": "The management credential has expired. Renewal needs a new "
                          "'throughline login --replace --scopes ...' and human approval.",
    "missing_scope": "The stored connection was not granted the scope this command needs.",
    "invalid_name": "Names are 1-64 printable ASCII characters.",
    "invalid_scopes": "Scopes must come from: " + ", ".join(SCOPES) + ".",
    "invalid_id": "IDs are 32 lowercase hexadecimal characters.",
    "invalid_output": "--output must be a new file inside the project directory.",
    "output_exists": "The --output file already exists. Nothing was overwritten.",
    "network_error": "The request could not be completed. Nothing was printed or retried.",
    "redirect_refused": "The server answered with a redirect. Credentials are never forwarded.",
    "response_too_large": "The response exceeded the client's size limit.",
    "invalid_response": "The server response did not match the published contract.",
    "base_url_changed": "The pending request was started against a different API origin.",
    "no_pending_request": "There is no pending login request in this project.",
    "storage_error": "A private file could not be written.",
    "delivery_unknown": "Local pending state is missing or unreadable, so the secrets for this "
                        "request are gone. If the request was approved, revoke that connection "
                        "in the account portal, run 'throughline cancel', then log in again.",
    "interrupted": "Interrupted. Local state was kept; run the command again to resume.",
    "pending_key_creation": "A key creation is unfinished. Repeat its exact name and output path to recover it.",
    "key_output_changed": "The pending key's output changed. Restore that private file before retrying; no new key was issued.",
    "disconnect_unknown": "The disconnect acknowledgement was lost. Local credentials were kept. Inspect and revoke this connection in the account portal.",
}


class CliError(Exception):
    def __init__(self, code: str, status: str = "error", **fields: Any) -> None:
        super().__init__(code)
        self.code, self.status, self.fields = code, status, fields


def _fail(code: str, **fields: Any) -> NoReturn:
    raise CliError(code, **fields)


# -- output -------------------------------------------------------------------


def _scrub(value: Any) -> Any:
    """Defence in depth: nothing shaped like a credential leaves this process."""
    if isinstance(value, str):
        return _SECRET.sub("[redacted]", value)
    if isinstance(value, dict):
        return {str(_scrub(k)): _scrub(v) for k, v in value.items()
                if k not in ("key", "management_token", "device_code", "token")
                or isinstance(v, dict)}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value if isinstance(value, (int, float, bool)) or value is None else None


def _emit(payload: dict[str, Any]) -> int:
    sys.stdout.write(json.dumps(_scrub(payload), sort_keys=True, allow_nan=False) + "\n")
    sys.stdout.flush()
    return EXIT[payload["status"]]


# -- private files ------------------------------------------------------------


def _base_url() -> str:
    override = os.environ.get(BASE_URL_ENV)
    if override is None:
        return DEFAULT_BASE_URL
    try:
        target = urllib.parse.urlsplit(override)
        host = target.hostname or ""
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
        _ = target.port
    except ValueError:
        loopback = False
    if (not loopback or target.scheme not in ("http", "https") or target.username is not None
            or target.password is not None or target.query or target.fragment
            or target.path not in ("", "/")):
        _fail("invalid_base_url")
    return override.rstrip("/")


def _owned_private(st: os.stat_result) -> str | None:
    if st.st_uid != os.geteuid():
        return "owner"
    if st.st_mode & 0o077:
        return "mode"
    return None


class Project:
    """The project directory, its private state directory and the state lock."""

    def __init__(self, path: str, *, no_git: bool) -> None:
        self.root = os.path.realpath(path)
        if not os.path.isdir(self.root):
            _fail("invalid_project")
        self.no_git = no_git
        self.state = os.path.join(self.root, STATE_DIR)
        self.env = os.path.join(self.root, ENV_FILE)
        self._lock: int | None = None

    def rel(self, path: str) -> str:
        return os.path.relpath(path, self.root)

    # git ----------------------------------------------------------------

    def _git(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
        return subprocess.run(("git", "-C", self.root) + args, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=30, env=env, check=False)

    def guard(self, *paths: str) -> None:
        """Require every private path to be untracked and ignored.

        ``--no-git`` only acknowledges that there is no repository. Inside a
        work tree the checks always run, and .gitignore is never edited here.
        """
        try:
            inside = self._git("rev-parse", "--is-inside-work-tree")
            found = inside.returncode == 0 and inside.stdout.strip() == b"true"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            found = False
        if not found:
            if self.no_git:
                return
            _fail("git_required")
        relative = [self.rel(p) for p in paths]
        tracked = self._git("ls-files", "-z", "--", *relative)
        if tracked.returncode != 0 or tracked.stdout:
            _fail("tracked_file", paths=sorted(
                p for p in tracked.stdout.decode("utf-8", "replace").split("\0") if p))
        missing = [p for p in relative if self._git("check-ignore", "-q", "--", p).returncode != 0]
        if missing:
            _fail("not_ignored", paths=missing,
                  gitignore_lines=sorted({ENV_FILE if p == ENV_FILE else
                                          STATE_DIR + "/" if p.startswith(STATE_DIR + os.sep) else p
                                          for p in missing}))

    def guard_state(self, *, writing: bool = True) -> None:
        """Every private artifact must be untracked and ignored before it is
        read or written. Readers check the ones that exist; writers check all."""
        self.open_state(create=False, lock=False)  # name an unsafe directory before git does
        paths = [self.env] + [os.path.join(self.state, name) for name in PRIVATE_FILES]
        if not writing:
            paths = [path for path in paths if os.path.lexists(path)]
        if paths:
            self.guard(*paths)

    # state directory and lock ----------------------------------------------

    def open_state(self, *, create: bool, lock: bool = True) -> bool:
        """Validate (optionally create) the state directory; writers take the lock."""
        try:
            st = os.lstat(self.state)
        except FileNotFoundError:
            if not create:
                return False
            try:
                os.mkdir(self.state, 0o700)
            except OSError:
                _fail("storage_error")
            st = os.lstat(self.state)
        if not stat.S_ISDIR(st.st_mode) or _owned_private(st):
            _fail("unsafe_state_directory")
        if lock and self._lock is None:
            try:
                fd = os.open(os.path.join(self.state, "lock"),
                             os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            except OSError:
                _fail("unsafe_file", path=STATE_DIR + "/lock", reason="symlink")
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                os.close(fd)
                _fail("unsafe_file", path=STATE_DIR + "/lock", reason="not_regular")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                _fail("state_locked")
            self._lock = fd
            for name in os.listdir(self.state):  # leftovers of an interrupted write
                if name.startswith(".tmp-"):
                    try:
                        os.unlink(os.path.join(self.state, name))
                    except OSError:
                        pass
        return True

    # files ------------------------------------------------------------------

    def read(self, path: str) -> bytes | None:
        """Read a private file, refusing anything that is not plainly ours."""
        name = self.rel(path)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except FileNotFoundError:
            return None
        except OSError as exc:
            _fail("unsafe_file", path=name,
                  reason="symlink" if exc.errno in (errno.ELOOP, errno.EMLINK) else "unreadable")
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                _fail("unsafe_file", path=name, reason="not_regular")
            problem = _owned_private(st) or ("hardlink" if st.st_nlink != 1 else None) or (
                "too_large" if st.st_size > MAX_FILE else None)
            if problem:
                _fail("unsafe_file", path=name, reason=problem)
            data = b""
            while len(data) <= MAX_FILE:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                data += chunk
            return data
        finally:
            os.close(fd)

    def write(self, path: str, data: bytes) -> None:
        """0600, fsync, then atomic rename from inside the 0700 state directory."""
        tmp = os.path.join(self.state, ".tmp-" + secrets.token_hex(8))
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600)
            try:
                os.fchmod(fd, 0o600)
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path)
            for directory in {self.state, os.path.dirname(path)}:
                dfd = os.open(directory, os.O_RDONLY | os.O_CLOEXEC)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            _fail("storage_error", path=self.rel(path))

    def remove(self, path: str) -> None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError:
            _fail("storage_error", path=self.rel(path))
        try:
            dfd = os.open(os.path.dirname(path), os.O_RDONLY | os.O_CLOEXEC)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            _fail("storage_error", path=self.rel(path))

    def read_json(self, name: str) -> dict[str, Any] | None:
        raw = self.read(os.path.join(self.state, name))
        if raw is None:
            return None
        try:
            value = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            value = None
        return value if isinstance(value, dict) else {}

    def write_json(self, name: str, value: dict[str, Any]) -> None:
        self.write(os.path.join(self.state, name),
                   (json.dumps(value, sort_keys=True, indent=1) + "\n").encode("utf-8"))

    # .env -------------------------------------------------------------------

    def env_key(self) -> tuple[bytes, int | None, str | None]:
        """Return (file bytes, index of the key line, its value). Lines are data,
        never sourced or evaluated."""
        data = self.read(self.env) or b""
        lines = data.split(b"\n")
        hits = [(i, m) for i, line in enumerate(lines)
                if (m := _ENV_LINE.match(line.rstrip(b"\r")))]
        if len(hits) > 1:
            _fail("duplicate_variable")
        if not hits:
            return data, None, None
        raw = hits[0][1].group(1).strip()
        if len(raw) >= 2 and raw[:1] == raw[-1:] and raw[:1] in (b'"', b"'"):
            raw = raw[1:-1]
        return data, hits[0][0], raw.decode("utf-8", "replace")

    def store_env_key(self, key: str, *, replace: bool) -> bool:
        """Write the feed key, preserving every other byte. True if one was replaced."""
        data, index, current = self.env_key()
        line = (KEY_ENV + "=" + key).encode("ascii")
        if current == key:
            return False
        if index is None:
            if data and not data.endswith(b"\n"):
                data += b"\n"
            self.write(self.env, data + line + b"\n")
            return False
        if current and not replace:
            _fail("credential_exists")
        lines = data.split(b"\n")
        lines[index] = line + (b"\r" if lines[index].endswith(b"\r") else b"")
        self.write(self.env, b"\n".join(lines))
        return bool(current)

    def feed_key(self) -> str:
        key = os.environ.get(KEY_ENV)
        if key is None:
            key = self.env_key()[2]
        if not key:
            _fail("no_credential")
        if not FEED_KEY.fullmatch(key):
            _fail("invalid_credential")
        return key


# -- transport ----------------------------------------------------------------

_OPENER = urllib.request.build_opener(_NoRedirect())


class _DeadlineExceeded(OSError):
    pass


class _wall_clock:
    """A hard whole-request deadline from the POSIX interval timer.

    The CLI is a single-threaded process, so the alarm interrupts whatever
    system call is blocking. Outside the main thread it is not available and
    the per-socket timeout and the read-loop check remain the only bounds."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.armed = False

    def __enter__(self) -> None:
        def expire(signum: int, frame: Any) -> NoReturn:
            raise _DeadlineExceeded()
        try:
            self.previous = signal.signal(signal.SIGALRM, expire)
        except ValueError:  # not the main thread
            return
        self.armed = True
        signal.setitimer(signal.ITIMER_REAL, self.seconds)

    def __exit__(self, *exc: Any) -> None:
        if self.armed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, self.previous)


def _http(base: str, method: str, path: str, *, token: str | None = None,
          body: dict[str, Any] | None = None, timeout: float) -> tuple[int, dict[str, Any], Any]:
    """One request, no retry, no redirect, bounded time and memory."""
    headers = {"Accept": "application/json", "User-Agent": _UA}
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    deadline = time.monotonic() + timeout

    def drain(response: Any) -> bytes:
        raw = b""
        while True:
            chunk = response.read1(65536)  # returns what has arrived; never waits to fill
            if not chunk:
                return raw
            raw += chunk
            if len(raw) > MAX_RESPONSE:
                _fail("response_too_large")
            if time.monotonic() > deadline:
                _fail("network_error")

    try:
        # The socket timeout bounds each wait; the timer bounds the whole request,
        # including name resolution and a reply that trickles one byte at a time.
        with _wall_clock(timeout):
            try:
                with _OPENER.open(request, timeout=timeout) as response:
                    status, headers_in, raw = response.status, response.headers, drain(response)
            except urllib.error.HTTPError as exc:
                try:
                    status, headers_in, raw = exc.code, exc.headers, drain(exc)
                finally:
                    exc.close()
    except (urllib.error.URLError, HTTPException, OSError):
        # No exception text: it can contain the URL or proxy details.
        _fail("network_error")
    if 300 <= status < 400:
        _fail("redirect_refused")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        payload = None
    if not isinstance(payload, dict):
        if status < 400:
            _fail("invalid_response")
        payload = {}
    return status, payload, headers_in


def _server_error(base: str, status: int, payload: dict[str, Any], headers: Any) -> CliError:
    """A sanitized error: fixed fields only, never the response body."""
    code = payload.get("error")
    fields: dict[str, Any] = {"http_status": status}
    request_id = payload.get("request_id")
    if isinstance(request_id, str) and _IDENT.fullmatch(request_id):
        fields["request_id"] = request_id
    if status in (429, 503):
        fields["retry_after"] = round(_retry_after(headers.get("Retry-After")), 3)
    upgrade = payload.get("upgrade_url")
    if isinstance(upgrade, str) and upgrade.startswith(DEFAULT_BASE_URL + "/account"):
        fields["upgrade_url"] = upgrade
    reset = payload.get("reset_at")
    if type(reset) in (int, float):
        fields["reset_at"] = reset
    return CliError(code if isinstance(code, str) and _CODE.fullmatch(code) else f"http_{status}",
                    **fields)


def _call(base: str, method: str, path: str, *, token: str, body: dict[str, Any] | None = None,
          timeout: float) -> dict[str, Any]:
    status, payload, headers = _http(base, method, path, token=token, body=body, timeout=timeout)
    if status != 200:
        raise _server_error(base, status, payload, headers)
    return payload


# -- login --------------------------------------------------------------------


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _parse_scopes(text: str) -> list[str]:
    wanted = {part.strip() for part in text.split(",") if part.strip()} | {"news:read"}
    if wanted - set(SCOPES):
        _fail("invalid_scopes")
    return [scope for scope in SCOPES if scope in wanted]


def _valid_name(name: str) -> str:
    if not 1 <= len(name) <= 64 or any(not 32 <= ord(ch) <= 126 for ch in name):
        _fail("invalid_name")
    return name


def _poll_floor(base: str) -> float:
    """Production polls are never closer than the contract minimum, whatever a
    server reply or an edited state file says. Only the loopback test override
    may compress the clock."""
    return MIN_INTERVAL if base == DEFAULT_BASE_URL else 0.0


def _load_pending(project: Project, base: str) -> dict[str, Any] | None:
    pending = project.read_json("pending.json")
    if pending is None:
        return None
    management = pending.get("management_token")
    scopes = pending.get("scopes")
    valid = (
        pending.get("version") == 1
        and all(isinstance(pending.get(k), str) for k in
                ("pairing_id", "name", "feed_key", "device_code", "user_code",
                 "verification_uri", "verification_uri_complete", "base_url"))
        and _IDENT.fullmatch(pending["pairing_id"]) and FEED_KEY.fullmatch(pending["feed_key"])
        and _DEVICE_CODE.fullmatch(pending["device_code"]) and _USER_CODE.fullmatch(pending["user_code"])
        # A consent link is shown again only if it is still the pinned one.
        and pending["verification_uri"] == pending["base_url"] + "/account"
        and pending["verification_uri_complete"]
        == pending["verification_uri"] + "#pair=" + pending["user_code"]
        and 1 <= len(pending["name"]) <= 64 and all(32 <= ord(ch) < 127 for ch in pending["name"])
        and (management is None or isinstance(management, str) and AGENT_TOKEN.fullmatch(management))
        and isinstance(scopes, list) and scopes and all(isinstance(x, str) for x in scopes)
        and set(scopes) <= set(SCOPES) and len(set(scopes)) == len(scopes) and "news:read" in scopes
        and all(type(pending.get(k)) in (int, float) for k in ("interval", "expires_at", "next_poll_at"))
        and type(pending.get("replace")) is bool
    )
    if not valid:
        raise CliError("delivery_unknown", status="delivery_unknown")
    if pending["base_url"] != base:
        _fail("base_url_changed")
    pending["interval"] = max(float(pending["interval"]), _poll_floor(base))
    return pending


def _pending_view(pending: dict[str, Any], status: str = "pending", **extra: Any) -> dict[str, Any]:
    return dict(status=status, pairing_id=pending["pairing_id"], user_code=pending["user_code"],
                verification_uri=pending["verification_uri"],
                verification_uri_complete=pending["verification_uri_complete"],
                name=pending["name"], scopes=pending["scopes"],
                expires_in=max(0, int(pending["expires_at"] - time.time())),
                interval=pending["interval"], pending_file=STATE_DIR + "/pending.json",
                next="Relay the code and link to the person, then immediately run "
                     "'throughline login --wait'.", **extra)


def _start(project: Project, base: str, args: argparse.Namespace) -> dict[str, Any]:
    scopes = _parse_scopes(args.scopes)
    name = _valid_name(args.name)
    feed_key = "tl_live_" + secrets.token_urlsafe(32)
    management = "tl_agent_" + secrets.token_urlsafe(32) if len(scopes) > 1 else None
    status, payload, headers = _http(base, "POST", "/auth/v1/pairings", timeout=args.timeout, body=dict(
        name=name, scopes=scopes, feed_key_hash=_sha256(feed_key),
        management_token_hash=_sha256(management) if management else None))
    if status != 200:
        error = _server_error(base, status, payload, headers)
        error.fields["fallback"] = FALLBACK  # capacity, or a server without this release
        raise error
    device, code = payload.get("device_code"), payload.get("user_code")
    uri, complete = payload.get("verification_uri"), payload.get("verification_uri_complete")
    interval, expires = payload.get("interval"), payload.get("expires_in")
    account = base + "/account"
    if not (isinstance(payload.get("pairing_id"), str) and _IDENT.fullmatch(payload["pairing_id"])
            and isinstance(device, str) and _DEVICE_CODE.fullmatch(device)
            and isinstance(code, str) and _USER_CODE.fullmatch(code)
            and uri == account and complete == account + "#pair=" + code
            and type(interval) in (int, float) and type(expires) in (int, float)):
        # Never print a link that is not on the pinned origin.
        _fail("invalid_response")
    interval, expires = float(interval), float(expires)  # type: ignore[arg-type]
    if not (0 < interval <= 3600 and 0 < expires <= 86400):
        _fail("invalid_response")
    interval = max(interval, _poll_floor(base))
    now = time.time()
    pending = dict(version=1, base_url=base, pairing_id=payload["pairing_id"], name=name,
                   scopes=scopes, feed_key=feed_key, management_token=management,
                   device_code=device, user_code=code, verification_uri=uri,
                   verification_uri_complete=complete, interval=interval, created_at=now,
                   expires_at=now + expires, next_poll_at=now + interval, replace=bool(args.replace))
    # Durable before anything is shown: an approval must always find its secrets.
    project.write_json("pending.json", pending)
    return pending


def _finalize(project: Project, pending: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Copy approved secrets into place. Safe to repeat after an interruption."""
    key, connection = payload.get("key"), payload.get("connection")
    ident = pending["pairing_id"]
    if not (isinstance(key, dict) and isinstance(connection, dict)
            and key.get("id") == ident and connection.get("id") == ident
            and isinstance(key.get("name"), str) and isinstance(connection.get("name"), str)
            and type(key.get("created_at")) in (int, float)
            and type(connection.get("created_at")) in (int, float)
            and type(connection.get("expires_at")) in (int, float)
            and isinstance(connection.get("scopes"), list)
            and "news:read" in connection["scopes"]
            and set(connection["scopes"]) <= set(pending["scopes"])
            and len(set(connection["scopes"])) == len(connection["scopes"])):
        _fail("invalid_response")
    scopes = [scope for scope in SCOPES if scope in connection["scopes"]]
    managed = len(scopes) > 1 and pending["management_token"] is not None
    # Refuse before touching anything: a login that may not replace the key must
    # not disturb the management credential of the connection still in use.
    _, _, current = project.env_key()
    if current and current != pending["feed_key"] and not pending["replace"]:
        _fail("credential_exists")
    # The project holds one connection. Its management credential is replaced by
    # the new grant, or removed when the human granted none, so an older and
    # wider token can never outlive the login that superseded it.
    credentials = os.path.join(project.state, "credentials.json")
    if not managed and os.path.lexists(credentials) and not pending.get("superseded"):
        pending["superseded"] = True  # survives an interruption after the removal
        project.write_json("pending.json", pending)
    superseded = bool(pending.get("superseded"))
    if managed:
        project.write_json("credentials.json", dict(
            version=1, base_url=pending["base_url"], connection_id=ident,
            name=connection["name"], scopes=scopes, created_at=connection["created_at"],
            expires_at=connection["expires_at"], management_token=pending["management_token"],
            feed_key_hash=_sha256(pending["feed_key"])))
    else:
        project.remove(credentials)
    replaced = project.store_env_key(pending["feed_key"], replace=pending["replace"])
    project.remove(os.path.join(project.state, "pending.json"))
    result = dict(
        status="delivered",
        key=dict(id=ident, name=key["name"], created_at=key["created_at"], lifetime="until_revoked"),
        connection=dict(id=ident, name=connection["name"], scopes=scopes,
                        created_at=connection["created_at"],
                        management_expires_at=connection["expires_at"] if managed else None),
        requested_scopes=pending["scopes"], env_file=ENV_FILE, env_variable=KEY_ENV,
        credentials_file=STATE_DIR + "/credentials.json" if managed else None,
        replaced_existing_key=replaced, removed_previous_management_credential=superseded)
    if replaced or superseded:
        result["note"] = ("The previous connection is still active on the server. Revoke it in the "
                          "account portal once nothing uses it.")
    return result


def _receipt_expired(pending: dict[str, Any]) -> dict[str, Any]:
    return dict(status="delivery_unknown", reason="expired_token",
                pairing_id=pending["pairing_id"], name=pending["name"],
                pending_file=STATE_DIR + "/pending.json", local_state_kept=True,
                next=f"The server no longer answers for this request. If it was approved, a key "
                     f"may be active. Sign in at {pending['verification_uri']} and look for the "
                     f"connection named '{pending['name']}' (id {pending['pairing_id']}); revoke "
                     f"it if it is there. Then run 'throughline cancel' to discard the local "
                     f"request and 'throughline login' to start again.")


def _poll(project: Project, base: str, pending: dict[str, Any], timeout: float) -> dict[str, Any]:
    """One metadata pickup. The server reply carries no secret, so it is idempotent."""
    status, payload, headers = _http(base, "POST", "/auth/v1/token", timeout=timeout,
                                     body=dict(device_code=pending["device_code"]))
    code = payload.get("error")
    if status == 200 and payload.get("status") == "delivered":
        return _finalize(project, pending, payload)
    if status == 400 and code == "authorization_pending":
        pending["next_poll_at"] = time.time() + max(pending["interval"], _poll_floor(base))
        project.write_json("pending.json", pending)
        return _pending_view(pending)
    if status == 400 and code == "access_denied":
        # A denial is definitive: this pairing never produced a key.
        project.remove(os.path.join(project.state, "pending.json"))
        return dict(status="denied", pairing_id=pending["pairing_id"],
                    next="No keys from this request are active. Run 'throughline login' to start again.")
    if status == 400 and code == "expired_token":
        # Expiry does not prove the request was never approved: an approval whose
        # pickup window lapsed answers the same way. The secrets stay on disk.
        return _receipt_expired(pending)
    if status == 429:
        wait = max(_retry_after(headers.get("Retry-After"), default=pending["interval"]),
                   _poll_floor(base))
        pending["next_poll_at"] = time.time() + wait
        project.write_json("pending.json", pending)
        return _pending_view(pending, "slow_down", retry_after=round(wait, 3))
    raise _server_error(base, status, payload, headers)


def cmd_login(project: Project, base: str, args: argparse.Namespace) -> dict[str, Any]:
    project.guard_state()
    project.open_state(create=True)
    pending = _load_pending(project, base)
    if pending is None:
        _, _, current = project.env_key()
        if current and not args.replace:
            _fail("credential_exists")
        pending = _start(project, base, args)
        if not args.wait:
            return _pending_view(pending)
        # Show the code before blocking; the final object still goes to stdout.
        sys.stderr.write(f"Approve code {pending['user_code']} at "
                         f"{pending['verification_uri_complete']}\n")
        sys.stderr.flush()
    elif args.replace and not pending["replace"]:
        pending["replace"] = True
        project.write_json("pending.json", pending)
    # The server decides expiry; the local clock only bounds how long we wait.
    deadline = time.monotonic() + max(0.0, pending["expires_at"] - time.time()) + 2 * pending["interval"]
    if args.max_wait is not None:
        deadline = min(deadline, time.monotonic() + args.max_wait)
    while True:
        wait = pending["next_poll_at"] - time.time()
        if wait > 0:
            if not args.wait:
                return _pending_view(pending, polled=False, retry_after=round(wait, 3))
            wait += random.uniform(0, min(1.0, pending["interval"] / 5))
            if time.monotonic() + wait > deadline:
                return _pending_view(pending, retry_after=round(wait, 3))
            time.sleep(wait)
        result = _poll(project, base, pending, args.timeout)
        if result["status"] not in ("pending", "slow_down") or not args.wait:
            return result


def cmd_cancel(project: Project, base: str, args: argparse.Namespace) -> dict[str, Any]:
    project.guard_state(writing=False)
    if not project.open_state(create=False):
        _fail("no_pending_request")
    try:
        pending = _load_pending(project, base)
    except CliError as exc:
        if exc.code != "delivery_unknown":
            raise
        project.remove(os.path.join(project.state, "pending.json"))
        return dict(status="cancelled", local_state_removed=True,
                    next="If this request was approved, revoke its connection in the account portal.")
    if pending is None:
        _fail("no_pending_request")
    # Cancelling after approval would strand a live key, so look first.
    try:
        result = _poll(project, base, pending, args.timeout)
    except CliError as exc:
        if exc.code in ("credential_exists", "storage_error", "unsafe_file", "duplicate_variable"):
            raise
        result = dict(status="pending")
    if result["status"] in ("delivered", "denied"):
        if result["status"] == "delivered":
            result["cancel"] = "refused_already_approved"
        return result
    if result["status"] == "delivery_unknown":
        # An explicit cancel is the owner's decision to discard the unused secrets.
        project.remove(os.path.join(project.state, "pending.json"))
        return dict(status="cancelled", pairing_id=pending["pairing_id"], name=pending["name"],
                    local_state_removed=True, delivery="unknown",
                    next=f"Local secrets were discarded. If a connection named '{pending['name']}' "
                         f"(id {pending['pairing_id']}) is listed at {pending['verification_uri']}, "
                         f"revoke it: its key can no longer be recovered here.")
    project.remove(os.path.join(project.state, "pending.json"))
    return dict(status="cancelled", pairing_id=pending["pairing_id"], user_code=pending["user_code"],
                next="Cancellation is local. Do not approve this code; the request expires on its "
                     "own. If it is approved anyway, revoke that connection in the account portal.")


# -- other commands -----------------------------------------------------------


def cmd_status(project: Project, base: str, args: argparse.Namespace) -> dict[str, Any]:
    project.guard_state(writing=False)
    payload = _call(base, "GET", "/v1/usage", token=project.feed_key(), timeout=args.timeout)
    if not isinstance(payload.get("plan"), str) or not isinstance(payload.get("usage"), dict):
        _fail("invalid_response")
    return dict(status="ok", plan=payload["plan"], usage=payload["usage"])


def _management(project: Project, base: str, scope: str | None) -> str:
    project.guard_state(writing=False)
    # Readers take no lock: files are replaced atomically, never edited in place.
    credentials = (project.read_json("credentials.json")
                   if project.open_state(create=False, lock=False) else None)
    if credentials is None:
        _fail("no_management_credential")
    token, scopes = credentials.get("management_token"), credentials.get("scopes")
    if (not isinstance(token, str) or not AGENT_TOKEN.fullmatch(token)
            or not isinstance(scopes, list) or type(credentials.get("expires_at")) not in (int, float)):
        _fail("invalid_credential")
    if credentials.get("base_url") != base:
        _fail("base_url_changed")
    if scope is not None and scope not in scopes:
        _fail("missing_scope", scope=scope, granted_scopes=scopes)
    if credentials["expires_at"] <= time.time():
        _fail("management_expired", expired_at=credentials["expires_at"])
    return token



def cmd_whoami(project: Project, base: str, args: argparse.Namespace) -> dict[str, Any]:
    token = _management(project, base, None)
    payload = _call(base, "GET", "/account/v1/connection", token=token, timeout=args.timeout)
    if not isinstance(payload.get("connection"), dict) or not isinstance(payload.get("account_id"), str):
        _fail("invalid_response")
    return dict(status="ok", **payload)


def cmd_doctor(project: Project, base: str, args: argparse.Namespace) -> dict[str, Any]:
    usage = cmd_status(project, base, args)
    try:
        connection = cmd_whoami(project, base, args)
    except CliError as exc:
        if exc.code != "no_management_credential":
            raise
        return dict(usage, connection=None, note="Feed access works. This project has no management credential.")
    return dict(status="ok", plan=usage["plan"], usage=usage["usage"],
                account_id=connection["account_id"], connection=connection["connection"],
                note="Checks use noncharged usage and connection status; no news poll was sent.")


def cmd_audit(project: Project, base: str, args: argparse.Namespace) -> dict[str, Any]:
    token = _management(project, base, None)
    path = "/account/v1/audit"
    if args.cursor is not None:
        if not re.fullmatch(r"[1-9][0-9]{0,18}", args.cursor):
            _fail("invalid_id")
        path += "?cursor=" + args.cursor
    return dict(status="ok", **_call(base, "GET", path, token=token, timeout=args.timeout))


def cmd_disconnect(project: Project, base: str, args: argparse.Namespace) -> dict[str, Any]:
    project.guard_state()
    project.open_state(create=True)
    receipt = project.read_json("disconnect.json")
    credentials = project.read_json("credentials.json")
    if receipt is None:
        token = _management(project, base, None)
        current = cmd_whoami(project, base, args)["connection"]
        if current.get("id") != credentials.get("connection_id"):
            _fail("invalid_response")
        try:
            result = _call(base, "DELETE", "/account/v1/connection", token=token, timeout=args.timeout)
        except CliError as exc:
            if exc.code in ("network_error", "invalid_response", "response_too_large"):
                raise CliError("disconnect_unknown", connection_id=credentials["connection_id"],
                               recovery_url=DEFAULT_BASE_URL + "/account") from None
            raise
        if result.get("id") != current["id"] or result.get("revoked") is not True:
            _fail("disconnect_unknown", connection_id=current["id"])
        receipt = dict(base_url=base, connection_id=current["id"],
                       credential_hash=_sha256(token), feed_key_hash=credentials.get("feed_key_hash"),
                       acknowledged=True)
        project.write_json("disconnect.json", receipt)
    if receipt.get("base_url") != base or receipt.get("acknowledged") is not True:
        _fail("invalid_response")
    if credentials is not None and (credentials.get("connection_id") != receipt.get("connection_id")
            or _sha256(credentials.get("management_token", "")) != receipt.get("credential_hash")):
        _fail("credential_exists")
    data, index, current_key = project.env_key()
    removed = False
    if current_key and _sha256(current_key) == receipt.get("feed_key_hash"):
        lines = data.split(b"\n")
        del lines[index]
        project.write(project.env, b"\n".join(lines))
        removed = True
    project.remove(os.path.join(project.state, "credentials.json"))
    project.remove(os.path.join(project.state, "disconnect.json"))
    return dict(status="ok", id=receipt["connection_id"], revoked=True,
                removed_feed_variable=removed,
                note="The connection and all linked keys are revoked. Other local files and integrations were preserved.")


def cmd_account(project: Project, base: str, args: argparse.Namespace) -> dict[str, Any]:
    token = _management(project, base, "account:read")
    return dict(status="ok", account=_call(base, "GET", "/account/v1/me", token=token,
                                           timeout=args.timeout))


def cmd_billing_status(project: Project, base: str, args: argparse.Namespace) -> dict[str, Any]:
    token = _management(project, base, "billing:read")
    return dict(status="ok", billing=_call(base, "GET", "/account/v1/billing/read", token=token,
                                           timeout=args.timeout))


def cmd_keys_list(project: Project, base: str, args: argparse.Namespace) -> dict[str, Any]:
    token = _management(project, base, "keys:read")
    query = {}
    if args.cursor is not None:
        if not _IDENT.fullmatch(args.cursor):
            _fail("invalid_id")
        query["cursor"] = args.cursor
    if args.newest:
        query["order"] = "newest"
    path = "/account/v1/keys" + ("?" + urllib.parse.urlencode(query) if query else "")
    payload = _call(base, "GET", path, token=token, timeout=args.timeout)
    if not isinstance(payload.get("items"), list) or "next_cursor" not in payload:
        _fail("invalid_response")
    return dict(status="ok", items=payload["items"], next_cursor=payload["next_cursor"])


def cmd_keys_revoke(project: Project, base: str, args: argparse.Namespace) -> dict[str, Any]:
    if not _IDENT.fullmatch(args.id):
        _fail("invalid_id")
    token = _management(project, base, "keys:write")
    payload = _call(base, "DELETE", "/account/v1/keys/" + args.id, token=token, timeout=args.timeout)
    if payload.get("id") != args.id or payload.get("revoked") is not True:
        _fail("invalid_response")
    return dict(status="ok", id=args.id, revoked=True)


def cmd_keys_create(project: Project, base: str, args: argparse.Namespace) -> dict[str, Any]:
    name = _valid_name(args.name)
    output = os.path.abspath(os.path.join(project.root, args.output))
    parent = os.path.realpath(os.path.dirname(output))
    output = os.path.join(parent, os.path.basename(output))
    if (os.path.commonpath([parent, project.root]) != project.root or not os.path.isdir(parent)
            or os.path.basename(output) in ("", ".", "..")):
        _fail("invalid_output")
    project.guard(output)
    project.guard_state()
    project.open_state(create=True)
    token = _management(project, base, "keys:write")
    credentials = project.read_json("credentials.json")
    pending = project.read_json("key-create.json")
    relative = project.rel(output)
    if pending is not None:
        if (pending.get("base_url") != base or pending.get("connection_id") != credentials.get("connection_id")
                or pending.get("name") != name or pending.get("output") != relative):
            _fail("pending_key_creation")
        key, ident = pending.get("feed_key"), pending.get("id")
        if (not isinstance(key, str) or not FEED_KEY.fullmatch(key)
                or not isinstance(ident, str) or not _IDENT.fullmatch(ident)):
            _fail("invalid_credential")
    else:
        if os.path.lexists(output):
            _fail("output_exists")
        key, ident = "tl_live_" + secrets.token_urlsafe(32), secrets.token_hex(16)
        pending = dict(base_url=base, connection_id=credentials["connection_id"],
                       name=name, output=relative, feed_key=key, id=ident)
        # The only secret exists durably before any remotely visible mutation.
        project.write_json("key-create.json", pending)
    line = (KEY_ENV + "=" + key + "\n").encode("ascii")
    saved = project.read(output)
    if saved is None:
        try:
            fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            os.close(fd)
        except OSError:
            _fail("storage_error", path=relative)
        project.write(output, line)
    elif saved != line:
        # Recover our interrupted exclusive write; arbitrary output is never replaced.
        if line.startswith(saved):
            project.write(output, line)
        else:
            _fail("key_output_changed")
    payload = _call(base, "POST", "/account/v1/keys", token=token,
                    body=dict(id=ident, name=name, key_hash=_sha256(key)), timeout=args.timeout)
    if (payload.get("id") != ident or payload.get("name") != name
            or type(payload.get("created_at")) not in (int, float) or "key" in payload):
        _fail("invalid_response")
    # Verify the replacement without spending news allowance. No old key is revoked.
    usage = _call(base, "GET", "/v1/usage", token=key, timeout=args.timeout)
    if not isinstance(usage.get("usage"), dict) or not isinstance(usage.get("plan"), str):
        _fail("invalid_response")
    project.remove(os.path.join(project.state, "key-create.json"))
    return dict(status="ok", key={k: payload[k] for k in ("id", "name", "created_at")},
                output=relative, env_variable=KEY_ENV, verified=True,
                next="Switch the consumer to this file, verify its call, then explicitly revoke the old key ID.")


def cmd_start_from_now(project: Project, base: str, args: argparse.Namespace) -> dict[str, Any]:
    project.guard_state()
    project.open_state(create=True)
    saved = project.read_json("cursor.json")
    if saved is not None:
        cursor = saved.get("cursor")
        if type(cursor) is not int or not 0 <= cursor <= 2**63 - 1:
            _fail("invalid_response", path=STATE_DIR + "/cursor.json")
        # Capturing the head again would skip everything published since.
        return dict(status="ok", cursor=cursor, captured=False, cursor_file=STATE_DIR + "/cursor.json")
    # The same bounded, single-attempt request path as every other command: one
    # whole-request deadline, no redirect, no retry.
    payload = _call(base, "GET", "/v1/news?cursor=latest&limit=1", token=project.feed_key(),
                    timeout=args.timeout)
    cursor = payload.get("next_cursor")
    if (type(cursor) is not int or not 0 <= cursor <= 2**63 - 1
            or payload.get("items") != [] or payload.get("has_more") is not False):
        _fail("invalid_response")
    project.write_json("cursor.json", dict(version=1, cursor=cursor, captured_at=time.time()))
    return dict(status="ok", cursor=cursor, captured=True, cursor_file=STATE_DIR + "/cursor.json",
                next="Poll /v1/news with this numeric cursor and save each next_cursor with the "
                     "records it brought.")


# -- entry point --------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:  # never echo argv: it may hold a secret
        raise CliError("usage")


def _parser() -> argparse.ArgumentParser:
    common = _Parser(add_help=False)
    common.add_argument("--project", default=".", help="project directory (default: current)")
    common.add_argument("--no-git", action="store_true",
                        help="acknowledge that the project is not a git work tree")
    common.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")
    parser = _Parser(prog="throughline", description="Throughline delegated setup. Prints one "
                     "JSON object; never prints a credential.")
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    login = sub.add_parser("login", parents=[common], help="request access for a human to approve")
    login.add_argument("--name", default="Throughline CLI")
    login.add_argument("--scopes", default="news:read", help="comma-separated; news:read is always included")
    login.add_argument("--replace", action="store_true", help="allow replacing an existing key in .env")
    wait = login.add_mutually_exclusive_group()
    wait.add_argument("--wait", dest="wait", action="store_true", help="poll until decided")
    wait.add_argument("--no-wait", dest="wait", action="store_false", help="check once and exit (default)")
    login.add_argument("--max-wait", type=float, default=None, help="stop waiting after this many seconds")
    login.set_defaults(run=cmd_login, wait=False)

    sub.add_parser("cancel", parents=[common], help="drop the local pending request").set_defaults(run=cmd_cancel)
    sub.add_parser("status", parents=[common], help="plan and allowance (GET /v1/usage)").set_defaults(run=cmd_status)
    sub.add_parser("account", parents=[common], help="account summary (account:read)").set_defaults(run=cmd_account)
    sub.add_parser("whoami", parents=[common], help="verified identity and permissions").set_defaults(run=cmd_whoami)
    sub.add_parser("doctor", parents=[common], help="verify access without a news poll").set_defaults(run=cmd_doctor)
    sub.add_parser("disconnect", parents=[common], help="revoke this connection and all its keys").set_defaults(run=cmd_disconnect)
    audit = sub.add_parser("audit", parents=[common], help="this connection's retained access history")
    audit.add_argument("--cursor", default=None)
    audit.set_defaults(run=cmd_audit)
    sub.add_parser("start-from-now", parents=[common],
                   help="capture the feed head once and save it").set_defaults(run=cmd_start_from_now)

    keys = sub.add_parser("keys", help="list, create or revoke keys").add_subparsers(
        dest="keys_command", required=True, parser_class=_Parser)
    listing = keys.add_parser("list", parents=[common])
    listing.add_argument("--newest", action="store_true", help="newest first")
    listing.add_argument("--cursor", default=None)
    listing.set_defaults(run=cmd_keys_list)
    create = keys.add_parser("create", parents=[common])
    create.add_argument("--name", required=True)
    create.add_argument("--output", required=True, help="new private ignored file for the key")
    create.set_defaults(run=cmd_keys_create)
    revoke = keys.add_parser("revoke", parents=[common])
    revoke.add_argument("id")
    revoke.set_defaults(run=cmd_keys_revoke)

    billing = sub.add_parser("billing", help="billing status").add_subparsers(
        dest="billing_command", required=True, parser_class=_Parser)
    billing.add_parser("status", parents=[common]).set_defaults(run=cmd_billing_status)
    return parser


def _terminate(signum: int, frame: Any) -> NoReturn:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    signal.signal(signal.SIGTERM, _terminate)
    try:
        if any(_SECRET.search(arg) for arg in argv):
            _fail("secret_in_arguments")
        args = _parser().parse_args(argv)
        if not 0 < args.timeout <= 300 or getattr(args, "max_wait", None) is not None and args.max_wait < 0:
            _fail("usage")
        base = _base_url()
        project = Project(args.project, no_git=args.no_git)
        return _emit(args.run(project, base, args))
    except CliError as exc:
        payload = dict(status=exc.status, **exc.fields)
        if exc.status == "error":
            payload["error"] = exc.code
        payload["message"] = MESSAGES.get(exc.code, "The server refused the request.")
        return _emit(payload)
    except KeyboardInterrupt:
        return _emit(dict(status="cancelled", interrupted=True, local_state_kept=True,
                          message=MESSAGES["interrupted"]))
    except Exception:
        # No traceback or exception text: either can carry a path, URL or value.
        return _emit(dict(status="error", error="internal_error",
                          message="Unexpected failure. Local state was not printed."))


if __name__ == "__main__":
    sys.exit(main())
