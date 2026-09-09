#!/usr/bin/env python3
"""
Telegram notifications for long-running experiment campaigns.

Usable three ways:

    python3 scripts/notify.py --discover          # learn the chat id, write it to config
    python3 scripts/notify.py --test              # send a hello message
    python3 scripts/notify.py "message text"      # send an arbitrary message

and from Python:

    from scripts.notify import Notifier
    n = Notifier.from_env()
    n.send("Phase P finished")
    with n.stage("Phase P"):                      # reports start, duration, failures
        run_campaign(...)

Notifications never raise into the caller: a campaign that runs for two days must not
die because the network hiccuped while sending a status update.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / "config" / "notify.env"
API = "https://api.telegram.org"

_MAX_LEN = 4000  # Telegram hard limit is 4096; leave room for the prefix.


def load_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    """Parse the ``export KEY=value`` lines of the gitignored config file."""
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line[len("export ") :] if line.startswith("export ") else line
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _escape_markdown(text: str) -> str:
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!])", r"\\\1", text)


class Notifier:
    def __init__(
        self,
        token: str | None,
        chat_id: str | None,
        *,
        prefix: str = "adaptix",
        enabled: bool = True,
        timeout_s: float = 15.0,
    ):
        self.token = token
        self.chat_id = chat_id
        self.prefix = prefix
        self.timeout_s = timeout_s
        self.enabled = bool(enabled and token and chat_id)
        self.failures = 0

    @classmethod
    def from_env(cls) -> Notifier:
        cfg = load_env_file()
        token = os.environ.get("TELEGRAM_BOT_TOKEN") or cfg.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID") or cfg.get("TELEGRAM_CHAT_ID")
        prefix = os.environ.get("NOTIFY_PREFIX") or cfg.get("NOTIFY_PREFIX") or "adaptix"
        raw_enabled = os.environ.get("NOTIFY_ENABLED") or cfg.get("NOTIFY_ENABLED") or "1"
        return cls(token, chat_id, prefix=prefix, enabled=raw_enabled not in ("0", "false", "no"))

    # -- low level ---------------------------------------------------------

    def _call(self, method: str, params: dict) -> dict | None:
        if not self.token:
            return None
        url = f"{API}/bot{self.token}/{method}"
        body = urllib.parse.urlencode(params).encode()
        try:
            req = urllib.request.Request(url, data=body, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, json.JSONDecodeError):
            self.failures += 1
            return None

    def send(self, text: str, *, silent: bool = False) -> bool:
        """Best-effort send. Returns False when disabled or the call failed."""
        if not self.enabled:
            return False
        message = f"[{self.prefix}] {text}" if self.prefix else text
        if len(message) > _MAX_LEN:
            message = message[: _MAX_LEN - 20] + "\n...(truncated)"
        out = self._call(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": message,
                "disable_notification": "true" if silent else "false",
            },
        )
        return bool(out and out.get("ok"))

    def send_document(self, path: Path, caption: str = "") -> bool:
        """Upload a small artefact (a summary JSON, a figure) as a document."""
        if not self.enabled or not path.is_file():
            return False
        boundary = "----adaptixnotify"
        parts: list[bytes] = []

        def field(name: str, value: str) -> None:
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                f"{value}\r\n".encode()
            )

        field("chat_id", str(self.chat_id))
        if caption:
            field("caption", caption[:1000])
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; "
            f"filename=\"{path.name}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode()
        )
        try:
            parts.append(path.read_bytes())
        except OSError:
            return False
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(parts)

        try:
            req = urllib.request.Request(
                f"{API}/bot{self.token}/sendDocument",
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=max(self.timeout_s, 60)) as resp:
                return bool(json.loads(resp.read().decode()).get("ok"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            self.failures += 1
            return False

    # -- convenience -------------------------------------------------------

    def discover_chat_id(self, *, wait_s: float = 0.0, poll_s: float = 3.0) -> str | None:
        """
        Find the chat id from pending updates, optionally waiting for a first message.

        Telegram only reveals a chat id once someone has messaged the bot, so a fresh
        bot has nothing to discover until the user sends /start.
        """
        deadline = time.time() + wait_s
        while True:
            data = self._call("getUpdates", {"timeout": 0})
            for update in (data or {}).get("result", []):
                for key in ("message", "edited_message", "channel_post", "my_chat_member"):
                    chat = (update.get(key) or {}).get("chat") or {}
                    if chat.get("id"):
                        return str(chat["id"])
            if time.time() >= deadline:
                return None
            time.sleep(poll_s)

    def notify_stage(self, name: str, status: str, detail: str = "") -> bool:
        marks = {"start": "->", "ok": "OK", "fail": "FAILED", "warn": "!"}
        head = f"{marks.get(status, status)} {name}"
        return self.send(f"{head}\n{detail}".strip(), silent=(status == "start"))

    @contextmanager
    def stage(self, name: str, *, detail: str = ""):
        """Report a stage boundary, including the traceback if it raises."""
        self.notify_stage(name, "start", detail)
        started = time.time()
        try:
            yield self
        except BaseException as exc:  # noqa: BLE001 - report anything, then re-raise
            elapsed = time.time() - started
            tb = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            self.notify_stage(name, "fail", f"after {elapsed / 60:.1f} min\n{tb}")
            raise
        else:
            elapsed = time.time() - started
            self.notify_stage(name, "ok", f"in {elapsed / 60:.1f} min")


def _write_chat_id(chat_id: str) -> bool:
    if not ENV_FILE.is_file():
        return False
    text = ENV_FILE.read_text()
    if re.search(r"^export TELEGRAM_CHAT_ID=.*$", text, flags=re.M):
        text = re.sub(
            r"^export TELEGRAM_CHAT_ID=.*$", f"export TELEGRAM_CHAT_ID={chat_id}", text, flags=re.M
        )
    else:
        text = text.rstrip("\n") + f"\nexport TELEGRAM_CHAT_ID={chat_id}\n"
    ENV_FILE.write_text(text)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a Telegram notification")
    parser.add_argument("message", nargs="*", help="message text")
    parser.add_argument("--discover", action="store_true", help="find and store the chat id")
    parser.add_argument("--wait", type=float, default=0.0,
                        help="seconds to wait for a first message during --discover")
    parser.add_argument("--test", action="store_true", help="send a hello message")
    parser.add_argument("--document", type=Path, default=None, help="upload a file")
    args = parser.parse_args()

    n = Notifier.from_env()
    if not n.token:
        print("No TELEGRAM_BOT_TOKEN. Copy config/notify.env.example to config/notify.env.",
              file=sys.stderr)
        return 1

    if args.discover:
        chat_id = n.discover_chat_id(wait_s=args.wait)
        if not chat_id:
            print(
                "No chat id yet. Send any message to your bot, then re-run "
                "(optionally with --wait 120).",
                file=sys.stderr,
            )
            return 1
        n.chat_id = chat_id
        n.enabled = True
        stored = _write_chat_id(chat_id)
        print(json.dumps({"chat_id": chat_id, "written_to_config": stored}, indent=2))
        n.send("Notifications wired up.")
        return 0

    if not n.enabled:
        print("Notifier disabled: missing chat id. Run --discover first.", file=sys.stderr)
        return 1

    if args.document:
        ok = n.send_document(args.document, caption=" ".join(args.message))
        return 0 if ok else 1

    text = " ".join(args.message) or ("Test from " + socket.gethostname() if args.test else "")
    if not text:
        parser.error("nothing to send")
    return 0 if n.send(text) else 1


if __name__ == "__main__":
    raise SystemExit(main())
