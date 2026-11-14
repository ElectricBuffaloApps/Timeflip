"""Emailing invoices, one of two ways per business.

Apple Mail: a draft on the Mac that runs the tracker, with the invoice and its timesheet attached, left open to check
and send. Mail does the sending, so there's no password. Everything is passed to the script as arguments, never
pasted into its text.

Your own email provider (SMTP): sent straight away, with a copy to the sender. The password is kept in the macOS
Keychain, never in the settings file.
"""
from __future__ import annotations

import shutil
import smtplib
import ssl
import subprocess
import time
from email.message import EmailMessage
from pathlib import Path

from . import config as cfg

KEEP_DAYS = 30

SCRIPT = '''
on run argv
    set theSubject to item 1 of argv
    set theBody to item 2 of argv
    set theTo to item 3 of argv
    set theSender to item 4 of argv
    set theFiles to {}
    repeat with i from 5 to (count of argv)
        set end of theFiles to ((POSIX file (item i of argv)) as alias)
    end repeat
    tell application "Mail"
        set msg to make new outgoing message with properties {subject:theSubject, content:theBody & return & return, visible:true}
        tell msg
            if theTo is not "" then make new to recipient at end of to recipients with properties {address:theTo}
            if theSender is not "" then
                try
                    set sender to theSender
                end try
            end if
            repeat with f in theFiles
                tell content
                    make new attachment with properties {file name:f} at after the last paragraph
                end tell
                delay 0.5
            end repeat
        end tell
        activate
    end tell
end run
'''


def outbox(bill_id: int) -> Path:
    """A folder for this invoice's attachments. Mail reads them when the draft is sent, so they're kept a while."""
    root = cfg.APP_DIR / "outbox"
    root.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - KEEP_DAYS * 86400
    for old in root.iterdir():
        try:
            if old.is_dir() and old.stat().st_mtime < cutoff:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass
    folder = root / str(int(bill_id))
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    return folder


def draft(subject: str, body: str, to: str, sender: str, files: list[tuple[str, bytes]], bill_id: int) -> None:
    """Open a draft in Mail with the files attached. Raises ValueError with a message for the page if it can't."""
    if not shutil.which("osascript"):
        raise ValueError("Emailing needs Apple Mail on the Mac that runs the tracker.")
    folder = outbox(bill_id)
    paths = []
    for name, raw in files:
        path = folder / name.replace("/", "-")
        path.write_bytes(raw)
        paths.append(str(path))
    try:
        r = subprocess.run(["osascript", "-e", SCRIPT, subject, body, to, sender, *paths],
                           capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        raise ValueError("Mail didn't answer. Look on the Mac for a box asking to let FlipTracker control Mail, "
                         "click OK, then try again.")
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        if "-1743" in err or "Not authorized" in err or "not allowed" in err.lower():
            raise ValueError("macOS is blocking this. On the Mac: System Settings → Privacy & Security → Automation, "
                             "and switch on Mail for Python (or FlipTracker). Then try again.")
        raise ValueError(f"Mail couldn't make the draft: {err or 'unknown error'}")


# ---------- your own email provider (SMTP) ----------

KEYCHAIN_SERVICE = "tftrack-smtp"


def save_password(business_id: str, password: str) -> None:
    if not shutil.which("security"):
        raise ValueError("Saving an email password needs the macOS Keychain.")
    subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", business_id, "-w", password],
                   check=True, capture_output=True)


def get_password(business_id: str) -> str | None:
    if not shutil.which("security"):
        return None
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", business_id, "-w"],
                       capture_output=True, text=True)
    return r.stdout.rstrip("\n") if r.returncode == 0 else None


def delete_password(business_id: str) -> None:
    if shutil.which("security"):
        subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", business_id],
                       capture_output=True)


def send(business: dict, password: str | None, to: str, subject: str, body: str, files: list[tuple[str, bytes]]) -> None:
    """Send through the business's own mail server, with a copy to the sender. Raises ValueError for the page."""
    sender = business.get("email") or ""
    if not to:
        raise ValueError("There's no email address for this client. Add one in Settings → Who each invoice goes to.")
    if not sender or not business.get("smtp_host"):
        raise ValueError("Finish this business's email settings in Settings → Your businesses.")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{business['name']} <{sender}>" if business.get("name") else sender
    msg["To"] = to
    msg["Bcc"] = sender   # your provider won't file a copy in Sent, so you get one in your inbox
    msg.set_content(body)
    for name, raw in files:
        msg.add_attachment(raw, maintype="application", subtype="pdf", filename=name)
    host, port, security = business["smtp_host"], int(business.get("smtp_port") or 587), business.get("smtp_security")
    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=30, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=30)
        with server:
            if security == "starttls":
                server.starttls(context=ssl.create_default_context())
            if business.get("smtp_user"):
                if not password:
                    raise ValueError("No email password is saved for this business. Enter it in Settings → Your businesses.")
                server.login(business["smtp_user"], password)
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise ValueError("Your email provider refused the username or password. Many providers need an 'app password' "
                         "made in your email account's security settings, not your normal one.")
    except (smtplib.SMTPException, OSError) as e:
        raise ValueError(f"Couldn't send through {host}: {e}")
