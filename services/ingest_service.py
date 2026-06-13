"""
services/ingest_service.py
---------------------------
Pure ingestion logic — parse emails, embed, store via repository.

Depends on:
  - infrastructure.parse_emails.parse_directory  (file parsing)
  - repository.email_repository.EmailRepository  (data access)
  - OpenAI client                                (embeddings)

Year-based ingestion:
  Pass year= to ingest() to load only that calendar year from the PST.
  Leave year=None to load everything in the directory (e.g. 2 years at once).
  Either way, the year/month metadata is always stored so the UI filters work.

Flow:
  Phase 1 — Parse  (progress 0-40%):  Read emails from PST/mbox/eml
  Phase 2 — Embed  (progress 40-100%): Batch-embed via OpenAI, upsert to ChromaDB

Public API:
  service = IngestService(repo, openai_client)
  result  = service.ingest(directory)                    # all years
  result  = service.ingest(directory, year=2023)         # 2023 only
  result  = service.ingest(directory, year=2023,
                           progress_callback=fn)         # with live progress
"""

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

from openai import OpenAI

from infrastructure.parse_emails import parse_directory
from repository.email_repository import EmailRepository

EMBED_MODEL = "text-embedding-3-small"
BATCH_SIZE = 100      # emails per OpenAI embedding call (API max: 2048)
MAX_RETRIES = 3       # retry attempts on transient OpenAI errors
RETRY_BASE_DELAY = 2  # seconds; doubles each retry (2s, 4s, 8s)


# ---------------------------------------------------------------------------
# Data transfer objects
# ---------------------------------------------------------------------------

@dataclass
class IngestResult:
    directory: str
    year: Optional[int] = None
    total_found: int = 0
    new_ingested: int = 0
    skipped_duplicates: int = 0
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class IngestService:

    def __init__(self, repo: EmailRepository, openai_client: OpenAI) -> None:
        self._repo = repo
        self._client = openai_client

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def ingest(
        self,
        directory: str,
        year: Optional[int] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> IngestResult:
        """
        Parse and embed emails in *directory*, skipping already-indexed ones.

        Args:
            directory:          Path to folder containing .pst/.mbox/.eml files.
            year:               If provided, only emails from this calendar year
                                are parsed and indexed. Leave None to ingest all
                                years present in the directory (e.g. 2 years at once).
                                After ingestion the UI will show year+month filters
                                for every year loaded.
            progress_callback:  Optional fn(current, total, message) for live updates.
                                Phase 1 (parse)  reports progress 0-40%.
                                Phase 2 (embed)  reports progress 40-100%.

        Returns:
            IngestResult summary — check .error first.
        """
        directory = os.path.expanduser(directory)
        if not os.path.isdir(directory):
            return IngestResult(
                directory=directory,
                year=year,
                error=f"Directory not found: {directory}",
            )

        year_label = f" for year {year}" if year else " (all years)"
        logger.info(f"[IngestService] Starting ingest: directory={directory}{year_label}")

        try:
            # ------------------------------------------------------------------
            # Phase 1: Parse  (progress 0 -> 40%)
            # ------------------------------------------------------------------
            self._notify(progress_callback, 0, 100, f"Reading emails{year_label}…")

            def _parse_progress(count: int, message: str) -> None:
                """Map parse-time count progress into the 0-38% window.
                Scales against 20K emails so progress moves smoothly for typical PST sizes.
                Clamps at 38 so the embed phase always starts visibly at 40%.
                """
                pct = min(38, int((count / 20_000) * 38))
                self._notify(progress_callback, pct, 100, message)

            emails = parse_directory(
                directory,
                year=year,
                progress_callback=_parse_progress,
            )
            total_found = len(emails)
            logger.info(f"[IngestService] Parsed {total_found:,} emails{year_label}")

            self._notify(
                progress_callback, 40, 100,
                f"Parsed {total_found:,} emails{year_label}. Checking for duplicates…",
            )

            existing_ids = self._repo.get_existing_ids()
            new_emails = [e for e in emails if e["id"] not in existing_ids]
            skipped = total_found - len(new_emails)

            if not new_emails:
                self._notify(
                    progress_callback, 100, 100,
                    f"All {skipped:,} emails already indexed — nothing new to add.",
                )
                logger.info(f"[IngestService] All {skipped:,} emails already indexed — skipping.")
                return IngestResult(
                    directory=directory,
                    year=year,
                    total_found=total_found,
                    new_ingested=0,
                    skipped_duplicates=skipped,
                )

            # ------------------------------------------------------------------
            # Phase 2: Embed + store  (progress 40 -> 100%)
            # ------------------------------------------------------------------
            total_new = len(new_emails)
            ingested = 0

            for start in range(0, total_new, BATCH_SIZE):
                batch = new_emails[start: start + BATCH_SIZE]
                end = min(start + BATCH_SIZE, total_new)

                # Map embed progress into the 40-100% window
                embed_pct = int((start / total_new) * 60)
                overall_pct = 40 + embed_pct
                self._notify(
                    progress_callback,
                    overall_pct,
                    100,
                    f"Embedding emails {start + 1:,}–{end:,} of {total_new:,}{year_label}…",
                )

                documents = [self._build_document(e) for e in batch]
                embeddings = self._embed_batch_with_retry(documents)

                self._repo.upsert(
                    ids=[e["id"] for e in batch],
                    embeddings=embeddings,
                    documents=documents,
                    metadatas=[self._build_metadata(e) for e in batch],
                )
                ingested += len(batch)

            self._notify(
                progress_callback, 100, 100,
                f"Done — ingested {ingested:,} new emails{year_label}.",
            )
            logger.info(f"[IngestService] Completed — ingested {ingested:,} new, skipped {skipped:,}{year_label}")

        except Exception as exc:
            logger.error(f"[IngestService] Unexpected error during ingest: {exc}", exc_info=True)
            return IngestResult(directory=directory, year=year, error=str(exc))
        return IngestResult(
            directory=directory,
            year=year,
            total_found=total_found,
            new_ingested=ingested,
            skipped_duplicates=skipped,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _embed_batch_with_retry(self, texts: list[str]) -> list[list[float]]:
        """
        Embed a batch of texts via OpenAI with exponential-backoff retry.

        Retries up to MAX_RETRIES times on transient errors (rate limits,
        timeouts, 5xx). Raises immediately on permanent errors (auth, 400).
        """
        last_exc: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            try:
                response = self._client.embeddings.create(
                    model=EMBED_MODEL, input=texts
                )
                return [item.embedding for item in response.data]
            except Exception as exc:
                last_exc = exc
                err_str = str(exc).lower()
                # Don't retry auth or input-validation errors
                if any(k in err_str for k in ("api key", "invalid", "400", "401")):
                    raise
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                print(
                    f"[WARN] OpenAI embed attempt {attempt + 1} failed: {exc}. "
                    f"Retrying in {delay}s…"
                )
                time.sleep(delay)
        raise RuntimeError(
            f"OpenAI embedding failed after {MAX_RETRIES} attempts: {last_exc}"
        )

    @staticmethod
    def _build_document(e: dict) -> str:
        return (
            f"Subject: {e['subject']}\n"
            f"From: {e['from']}\n"
            f"To: {e['to']}\n"
            f"Date: {e['date']}\n\n"
            f"{e['body']}"
        )

    @staticmethod
    def _build_metadata(e: dict) -> dict[str, Any]:
        return {
            "source": e["source"],
            "from": e["from"],
            "to": e["to"],
            "date": e["date"],
            "year": e.get("year", 0),
            "month": e.get("month", 0),
            "subject": e["subject"],
            "body_preview": e["body"][:500],
        }

    @staticmethod
    def _notify(
        callback: Optional[Callable[[int, int, str], None]],
        current: int,
        total: int,
        message: str,
    ) -> None:
        if callback:
            callback(current, total, message)
        else:
            print(f"[INFO] {message}")
