"""
parse_emails.py
---------------
Converts PST, mbox, and .eml files into a unified list of email dicts.

Unified schema per email:
  {
    "id":       str,   # unique deterministic ID (sha256 of from+date+subject)
    "source":   str,   # file path the email came from
    "from":     str,
    "to":       str,
    "date":     str,   # ISO-8601 string
    "year":     int,   # calendar year (0 if unknown)
    "month":    int,   # 1-12 (0 if unknown)
    "subject":  str,
    "body":     str,   # clean plain-text body (HTML stripped, noise removed)
  }

Year-based ingestion:
  Pass year= to parse_directory() or parse_pst() to read only that year's
  emails. For PST files via COM the filter runs at item level — Outlook only
  reads the body of matching items, so 75K-email PSTs ingest one year (~15K)
  without reading the other 60K.

Usage:
  from parse_emails import parse_directory
  emails = parse_directory("./my_emails")            # all years
  emails = parse_directory("./my_emails", year=2023) # 2023 only
"""

import hashlib
import logging
import mailbox
import email
import email.utils
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTML stripping & body cleaning helpers
# ---------------------------------------------------------------------------

def _strip_html(html: str) -> str:
    """
    Convert HTML email body to clean plain text.

    Handles: tag removal, entity decoding, quoted-printable artifacts,
    and whitespace normalization. No external dependencies -- stdlib re only.
    """
    # Remove <style> and <script> blocks entirely -- not readable text
    text = re.sub(r"<(style|script)[^>]*>.*?</(style|script)>", " ", html,
                  flags=re.DOTALL | re.IGNORECASE)

    # Replace block-level tags with newlines to preserve sentence boundaries
    text = re.sub(r"<(br|p|div|tr|li|h[1-6])[^>]*>", "\n", text, flags=re.IGNORECASE)

    # Strip remaining HTML tags
    text = re.sub(r"<[^>]+>", " ", text)

    # Decode common HTML entities
    entity_map = [
        (r"&nbsp;", " "), (r"&amp;", "&"), (r"&lt;", "<"), (r"&gt;", ">"),
        (r"&quot;", '"'), (r"&#39;", "'"), (r"&#\d+;", " "), (r"&[a-z]+;", " "),
    ]
    for pattern, replacement in entity_map:
        text = re.sub(pattern, replacement, text)

    # Remove quoted-printable encoding artifacts (=20, =3D, soft line breaks)
    text = re.sub(r"=[0-9A-Fa-f]{2}", " ", text)
    text = re.sub(r"=\r?\n", "", text)

    # Normalize whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


# Signature marker patterns -- lines matching these mark the start of the signature block
_SIGNATURE_MARKER = re.compile(
    r"^(-{2,}|_{2,}|={2,})\s*$"
    r"|^sent from (my |the )?(iphone|android|samsung|outlook|gmail|yahoo|mail)",
    re.IGNORECASE,
)

# Quoted reply header patterns -- signals start of quoted thread; stop processing here
_REPLY_HEADER = re.compile(
    r"^on\s+.{5,80}wrote\s*:?\s*$"
    r"|^-{3,}\s*original message\s*-{3,}$"
    r"|^-{3,}\s*forwarded message\s*-{3,}$",
    re.IGNORECASE,
)

# Long tracking/unsubscribe URLs -- add vector noise without semantic value
_TRACKING_URL = re.compile(
    r"https?://\S{80,}"
    r"|https?://[^\s]*(?:track|click|open|pixel|beacon|unsubscribe|optout)[^\s]*",
    re.IGNORECASE,
)


def _clean_body(text: str) -> str:
    """
    Remove noise from a plain-text email body before embedding.

    Applied after _strip_html() so input is already plain text.

    Removes:
      1. Tracking / unsubscribe URLs (add vector noise, zero semantic value)
      2. Quoted reply lines starting with ">"
      3. Reply / forward header lines ("On Mon Jan wrote:", "--- Original Message ---")
      4. Email signatures (everything from "--", "__", "Sent from my iPhone" onwards)
      5. Pure decorative separator lines ("***", "---", "===")
      6. Excess blank lines
    """
    # 1. Strip tracking URLs inline
    text = _TRACKING_URL.sub(" ", text)

    lines = text.splitlines()
    clean: list[str] = []

    for line in lines:
        stripped = line.strip()

        # 2. Skip quoted reply lines
        if stripped.startswith(">"):
            continue

        # 3. Reply header -- everything below is the quoted thread; stop
        if _REPLY_HEADER.match(stripped):
            break

        # 4. Signature marker -- everything below is boilerplate; stop
        if _SIGNATURE_MARKER.match(stripped):
            break

        # 5. Pure separator lines
        if re.match(r"^[\*\-=_#~]{3,}\s*$", stripped):
            continue

        clean.append(line)

    # 6. Collapse 3+ consecutive blank lines
    result = re.sub(r"\n{3,}", "\n\n", "\n".join(clean))
    return result.strip()


# ---------------------------------------------------------------------------
# Shared parsing helpers
# ---------------------------------------------------------------------------

def _email_id(from_: str, date: str, subject: str) -> str:
    raw = f"{from_}|{date}|{subject}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _parse_date(date_str: str) -> str:
    """Return ISO-8601 string; fall back to epoch on parse failure."""
    if not date_str:
        return datetime(1970, 1, 1, tzinfo=timezone.utc).isoformat()
    try:
        parsed = email.utils.parsedate_to_datetime(date_str)
        return parsed.isoformat()
    except Exception:
        return date_str


def _body_from_message(msg: email.message.Message) -> str:
    """Extract best plain-text body from an email.message.Message."""
    plain_parts: list[str] = []
    html_parts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if "attachment" in disp:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                text = payload.decode(charset, errors="replace")
            except Exception:
                continue
            if ct == "text/plain":
                plain_parts.append(text)
            elif ct == "text/html":
                html_parts.append(_strip_html(text))
    else:
        charset = msg.get_content_charset() or "utf-8"
        try:
            payload = msg.get_payload(decode=True)
            text = payload.decode(charset, errors="replace") if payload else ""
        except Exception:
            text = str(msg.get_payload())
        if msg.get_content_type() == "text/html":
            html_parts.append(_strip_html(text))
        else:
            plain_parts.append(text)

    body = "\n".join(plain_parts).strip() or "\n".join(html_parts).strip()
    body = _clean_body(body)
    # Limit body length stored in vector DB to 2000 chars for cost control
    return body[:2000]


def _year_from_iso(date_iso: str) -> int:
    """
    Extract the year as an integer from an ISO-8601 date string.
    Returns 0 for epoch-fallback dates (1970) or unparseable strings,
    so they can be identified as 'unknown year' without crashing.
    """
    try:
        year = int(date_iso[:4])
        return year if year > 1970 else 0
    except (ValueError, TypeError):
        return 0


def _month_from_iso(date_iso: str) -> int:
    """
    Extract the month as an integer (1-12) from an ISO-8601 date string.
    Returns 0 for missing/unparseable dates.
    """
    try:
        return int(date_iso[5:7])
    except (ValueError, TypeError):
        return 0


def _to_unified(msg: email.message.Message, source: str) -> dict:
    from_ = msg.get("From", "")
    to = msg.get("To", "")
    date_raw = msg.get("Date", "")
    subject = msg.get("Subject", "")
    body = _body_from_message(msg)
    date_iso = _parse_date(date_raw)
    return {
        "id": _email_id(from_, date_iso, subject),
        "source": source,
        "from": from_,
        "to": to,
        "date": date_iso,
        "year": _year_from_iso(date_iso),
        "month": _month_from_iso(date_iso),
        "subject": subject,
        "body": body,
    }


# ---------------------------------------------------------------------------
# Format-specific parsers
# ---------------------------------------------------------------------------

def parse_eml(path: str) -> Generator[dict, None, None]:
    """Parse a single .eml file."""
    try:
        with open(path, "rb") as f:
            msg = email.message_from_binary_file(f)
        yield _to_unified(msg, source=path)
    except Exception as exc:
        logger.warning(f"Could not parse EML {path}: {exc}")


def parse_mbox(path: str) -> Generator[dict, None, None]:
    """Parse an mbox file (may contain thousands of emails)."""
    try:
        mbox = mailbox.mbox(path)
        for msg in mbox:
            try:
                yield _to_unified(msg, source=path)
            except Exception as exc:
                logger.warning(f"Skipping message in {path}: {exc}")
    except Exception as exc:
        logger.warning(f"Could not open mbox {path}: {exc}")


def parse_pst(
    path: str,
    year: Optional[int] = None,
    progress_callback: Optional[Any] = None,
) -> Generator[dict, None, None]:
    """
    Parse a PST file on Windows using Outlook COM interface via pywin32.
    Falls back to pypff on non-Windows platforms (Linux/Mac in CI/deployment).

    Args:
        path:              Path to the .pst file.
        year:              If provided, only emails from this calendar year are
                           returned. COM filtering happens at item level so we
                           never read the full 75K dataset to extract one year.
        progress_callback: Optional fn(count, message) called as items are read.
                           Used by the ingest service for real-time parse progress.
    """
    import platform
    if platform.system() == "Windows":
        yield from _parse_pst_via_com(path, year=year, progress_callback=progress_callback)
    else:
        yield from _parse_pst_via_pypff(path, year=year)


def _parse_pst_via_com(
    path: str,
    year: Optional[int] = None,
    progress_callback: Optional[Any] = None,
) -> Generator[dict, None, None]:
    """
    Read PST via Outlook COM automation (Windows only, requires pywin32).

    COM requires Single-Threaded Apartment (STA) initialization on the calling
    thread. FastAPI worker threads are NOT COM-initialized by default.

    Fix: run all COM work inside a dedicated ThreadPoolExecutor thread that
    calls pythoncom.CoInitialize() as its first action. Results collected to
    list (generators cannot cross threads), then yielded back on caller thread.

    Year filtering happens inside the COM thread before any dict is appended,
    so only the target year's emails cross the thread boundary.
    Timeout raised to 900s (15 min) to handle 15K+ items safely.
    """
    try:
        import win32com.client  # type: ignore
        import pythoncom        # type: ignore
    except ImportError:
        logger.error("pywin32 not installed. Run: pip install pywin32")
        return

    abs_path = str(Path(path).resolve())
    year_label = f" (year={year})" if year else ""

    def _com_worker() -> list[dict]:
        """Runs entirely inside a COM-initialized thread. Returns a filtered list."""
        pythoncom.CoInitialize()
        results: list[dict] = []
        namespace = None
        pst_store = None
        try:
            outlook = win32com.client.Dispatch("Outlook.Application")
            namespace = outlook.GetNamespace("MAPI")
            namespace.AddStoreEx(abs_path, 3)  # 3 = olStoreUnicode

            for store in namespace.Stores:
                try:
                    if store.FilePath and Path(store.FilePath).resolve() == Path(abs_path).resolve():
                        pst_store = store
                        break
                except Exception:
                    continue

            if pst_store is None:
                logger.warning(f"Could not locate PST store for {abs_path}")
                return results

            root_folder = pst_store.GetRootFolder()
            for email_dict in _walk_com_folder(root_folder, source=abs_path, year=year):
                results.append(email_dict)
                count = len(results)
                # Emit progress every 100 items so the UI stays responsive
                if progress_callback and count % 100 == 0:
                    progress_callback(
                        count,
                        f"Reading PST{year_label} — {count:,} emails read so far…",
                    )

        except Exception as exc:
            logger.warning(f"COM PST parsing failed for {abs_path}: {exc}", exc_info=True)
        finally:
            if namespace is not None and pst_store is not None:
                try:
                    namespace.RemoveStore(pst_store.GetRootFolder())
                except Exception:
                    pass
            pythoncom.CoUninitialize()

        return results

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_com_worker)
        try:
            # 1800s (30 min) — headroom for 15K+ items; raise COM_TIMEOUT env var to override
            com_timeout = int(os.environ.get("COM_TIMEOUT", "1800"))
            emails = future.result(timeout=com_timeout)
        except concurrent.futures.TimeoutError:
            com_timeout = int(os.environ.get("COM_TIMEOUT", "1800"))
            logger.warning(f"PST parsing timed out after {com_timeout}s for {path}{year_label}")
            return
        except Exception as exc:
            logger.warning(f"PST thread failed: {exc}", exc_info=True)
            return

    yield from emails


def _walk_com_folder(
    folder,
    source: str,
    year: Optional[int] = None,
) -> Generator[dict, None, None]:
    """
    Recursively yield emails from an Outlook COM folder.

    When *year* is provided, Items.Restrict() pre-filters at the Outlook level
    so only matching items are returned — Python never calls .Item(i) on
    non-matching rows.  This is the key performance fix for multi-year PSTs:
    scanning 15K items instead of 75K.  Falls back to manual SentOn check if
    Restrict() raises (e.g. unsupported store).

    Blocker 3 is resolved automatically: with Restrict() active, every scanned
    item is also a collected item, so the caller's 100-item progress tick fires
    at the expected rate even during parse.
    """
    try:
        items = folder.Items

        # --- Blocker 2: Outlook-level year pre-filter via Restrict() ---
        _restrict_active = False
        if year is not None:
            # JET filter: Outlook returns only items whose SentOn falls in [year]
            filter_str = (
                f"[SentOn] >= '01/01/{year} 12:00 AM' "
                f"AND [SentOn] < '01/01/{year + 1} 12:00 AM'"
            )
            try:
                items = items.Restrict(filter_str)
                _restrict_active = True
            except Exception as exc:
                # Restrict unsupported on this store — fall through to manual filter
                print(f"[WARN] Items.Restrict() failed, using manual year filter: {exc}")

        for i in range(1, items.Count + 1):
            try:
                item = items.Item(i)
                # Only process mail items (Outlook item class 43)
                if item.Class != 43:
                    continue

                # --- Year fallback filter (only needed when Restrict() was unavailable) ---
                date_iso = ""
                try:
                    sent = item.SentOn
                    if sent:
                        date_iso = sent.isoformat()
                        if year is not None and not _restrict_active and sent.year != year:
                            continue   # skip — wrong year, don't read body
                except Exception:
                    if year is not None and not _restrict_active:
                        continue

                from_ = item.SenderEmailAddress or item.SenderName or ""
                to = item.To or ""
                subject = item.Subject or ""
                body = _clean_body(item.Body or "")[:2000]

                yield {
                    "id": _email_id(from_, date_iso, subject),
                    "source": source,
                    "from": from_,
                    "to": to,
                    "date": date_iso,
                    "year": _year_from_iso(date_iso),
                    "month": _month_from_iso(date_iso),
                    "subject": subject,
                    "body": body,
                }
            except Exception as exc:
                logger.warning(f"Skipping COM mail item: {exc}")
    except Exception as exc:
        logger.warning(f"Could not iterate folder {getattr(folder, 'Name', '?')}: {exc}")

    # Recurse into subfolders
    try:
        for i in range(1, folder.Folders.Count + 1):
            try:
                yield from _walk_com_folder(folder.Folders.Item(i), source=source, year=year)
            except Exception as exc:
                logger.warning(f"Skipping subfolder: {exc}")
    except Exception as exc:
        logger.warning(f"Could not access subfolders: {exc}")


def _parse_pst_via_pypff(
    path: str,
    year: Optional[int] = None,
) -> Generator[dict, None, None]:
    """Read PST via pypff — Linux/Mac fallback (requires libpff-python)."""
    try:
        import pypff  # type: ignore
    except ImportError:
        logger.error("pypff not installed. On Linux: pip install libpff-python")
        return

    def _walk_folder(folder):
        for i in range(folder.get_number_of_sub_messages()):
            try:
                msg = folder.get_sub_message(i)
                date_iso = ""
                delivery = msg.get_delivery_time()
                if delivery:
                    date_iso = delivery.isoformat()
                    # Year pre-filter — same logic as COM path
                    if year is not None and delivery.year != year:
                        continue
                elif year is not None:
                    continue  # no date + year filter active -> skip

                from_ = msg.get_sender_name() or ""
                subject = msg.get_subject() or ""
                body_plain = msg.get_plain_text_body()
                body_html = msg.get_html_body()
                if body_plain:
                    body = _clean_body(body_plain.decode("utf-8", errors="replace"))[:2000]
                elif body_html:
                    body = _clean_body(_strip_html(body_html.decode("utf-8", errors="replace")))[:2000]
                else:
                    body = ""

                yield {
                    "id": _email_id(from_, date_iso, subject),
                    "source": path,
                    "from": from_,
                    "to": "",
                    "date": date_iso,
                    "year": _year_from_iso(date_iso),
                    "month": _month_from_iso(date_iso),
                    "subject": subject,
                    "body": body,
                }
            except Exception as exc:
                logger.warning(f"Skipping PST message: {exc}")
        for j in range(folder.get_number_of_sub_folders()):
            yield from _walk_folder(folder.get_sub_folder(j))

    try:
        pst_file = pypff.file()
        pst_file.open(path)
        root = pst_file.get_root_folder()
        yield from _walk_folder(root)
        pst_file.close()
    except Exception as exc:
        logger.warning(f"pypff could not parse PST {path}: {exc}", exc_info=True)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_directory(
    directory: str,
    year=None,
    progress_callback=None,
) -> list[dict]:
    """
    Recursively scan *directory* for .pst, .mbox, and .eml files.
    Returns a deduplicated list of unified email dicts.

    Args:
        directory:         Root folder to scan.
        year:              If provided, only emails from this calendar year are
                           returned. For PST files the filter runs at COM/pypff
                           item level -- unmatched emails are never fully parsed.
                           For mbox/eml files the filter is applied post-parse
                           (these files are typically much smaller than PSTs).
        progress_callback: Optional fn(count, message) forwarded to PST parsers
                           so callers can show live parse progress in the UI.
    """
    directory = os.path.expanduser(directory)
    seen_ids: set[str] = set()
    results: list[dict] = []

    for root, _, files in os.walk(directory):
        for fname in files:
            ext = Path(fname).suffix.lower()
            if ext not in (".eml", ".mbox", ".pst"):
                continue
            fpath = os.path.join(root, fname)
            year_label = f" [year={year}]" if year else ""
            logger.info(f"Parsing {fpath}{year_label} ...")

            if ext == ".pst":
                generator = parse_pst(fpath, year=year, progress_callback=progress_callback)
            elif ext == ".mbox":
                generator = parse_mbox(fpath)
            else:
                generator = parse_eml(fpath)

            for email_dict in generator:
                # Post-parse year filter for mbox/eml (no native filter available)
                if year is not None and ext != ".pst":
                    if email_dict.get("year", 0) != year:
                        continue
                if email_dict["id"] not in seen_ids:
                    seen_ids.add(email_dict["id"])
                    results.append(email_dict)

    year_label = f" for year {year}" if year else ""
    logger.info(f"Parsed {len(results):,} unique emails from {directory}{year_label}")
    return results


if __name__ == "__main__":
    import sys
    import json

    target = sys.argv[1] if len(sys.argv) > 1 else "./sample_emails"
    filter_year = int(sys.argv[2]) if len(sys.argv) > 2 else None
    emails = parse_directory(target, year=filter_year)
    print(json.dumps(emails[:3], indent=2))
