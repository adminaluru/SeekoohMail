"""
services/search_service.py
---------------------------
Pure search logic -- no ChromaDB imports, no HTTP, no UI.

Depends on:
  - repository.email_repository.EmailRepository  (data access)
  - OpenAI client                                (AI calls)

Flow:
  1. Embed the user query via OpenAI text-embedding-3-small
  2. Query the repository for top-N similar emails
  3. Generate a 3-5 line summary via GPT-4o-mini
  4. Return a SearchResult dataclass

Public API:
  service = SearchService(repo, openai_client)
  result  = service.search(query, top_k=10)
  result  = service.search(query, top_k=10, year=2023)
  result  = service.search(query, top_k=10, year=2023, month=6)
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

from openai import OpenAI

from repository.email_repository import EmailRepository, QueryResult

EMBED_MODEL = "text-embedding-3-small"
SUMMARY_MODEL = "gpt-4o-mini"


# ---------------------------------------------------------------------------
# Data transfer objects (consumed by API layer)
# ---------------------------------------------------------------------------

@dataclass
class EmailMatch:
    rank: int
    id: str
    subject: str
    from_: str
    to: str
    date: str
    body_preview: str
    relevance_pct: float   # 0-100, higher = more relevant
    source: str


@dataclass
class SearchResult:
    query: str
    summary: str
    emails: list[EmailMatch] = field(default_factory=list)
    total_indexed: int = 0
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class SearchService:

    def __init__(self, repo: EmailRepository, openai_client: OpenAI) -> None:
        self._repo = repo
        self._client = openai_client

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        top_k: int = 10,
        year: Optional[int] = None,
        month: Optional[int] = None,
    ) -> SearchResult:
        """
        Run a semantic search and return results with an AI summary.

        Args:
            query:  Natural-language search string from the user.
            top_k:  Number of emails to retrieve.
            year:   Optional calendar year filter (e.g. 2023). None = all years.
            month:  Optional month filter (1-12). Only applied when year is set.

        Returns:
            SearchResult -- check .error first before reading .emails.
        """
        total = self._repo.count()
        if total == 0:
            return SearchResult(
                query=query,
                summary="",
                total_indexed=0,
                error="No emails indexed yet. Run ingestion first.",
            )

        logger.info(f"[SearchService] Query='{query}' top_k={top_k} year={year} month={month}")

        try:
            query_embedding = self._embed(query)
            query_result: QueryResult = self._repo.query(
                query_embedding, n_results=top_k, year=year, month=month
            )

            emails = self._build_email_matches(query_result)
            summary = self._summarize(query, emails)

            logger.info(f"[SearchService] Returned {len(emails)} results for query='{query}'")
            return SearchResult(
                query=query,
                summary=summary,
                emails=emails,
                total_indexed=total,
            )
        except Exception as exc:
            logger.error(f"[SearchService] Unexpected error for query='{query}': {exc}", exc_info=True)
            return SearchResult(query=query, summary="", total_indexed=total, error=str(exc))

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _embed(self, text: str) -> list[float]:
        """Embed search query with 3-attempt retry — mirrors IngestService._embed_batch_with_retry."""
        import time
        last_exc = None
        for attempt in range(3):
            try:
                response = self._client.embeddings.create(model=EMBED_MODEL, input=[text])
                return response.data[0].embedding
            except Exception as exc:
                last_exc = exc
                if any(k in str(exc).lower() for k in ("api key", "invalid", "400", "401")):
                    raise
                delay = 2 ** attempt  # 1s, 2s, 4s
                print(f"[WARN] Search embed attempt {attempt + 1} failed: {exc}. Retrying in {delay}s…")
                time.sleep(delay)
        raise RuntimeError(f"OpenAI embed failed after 3 attempts: {last_exc}")

    def _build_email_matches(self, result: QueryResult) -> list[EmailMatch]:
        matches = []
        for i, (id_, meta, dist) in enumerate(
            zip(result.ids, result.metadatas, result.distances)
        ):
            relevance_pct = round(max(0.0, (1 - dist) * 100), 1)
            matches.append(
                EmailMatch(
                    rank=i + 1,
                    id=id_,
                    subject=meta.get("subject", "(no subject)"),
                    from_=meta.get("from", ""),
                    to=meta.get("to", ""),
                    date=meta.get("date", ""),
                    body_preview=meta.get("body_preview", ""),
                    relevance_pct=relevance_pct,
                    source=meta.get("source", ""),
                )
            )
        return matches

    def _summarize(self, query: str, emails: list[EmailMatch]) -> str:
        if not emails:
            return "No relevant emails found for your query."

        snippets = "\n\n".join(
            f"[{e.rank}] From: {e.from_} | Subject: {e.subject} | Date: {e.date[:10]}\n"
            f"  {e.body_preview[:200]}"
            for e in emails
        )
        prompt = (
            f'You are an email assistant. The user searched for: "{query}"\n\n'
            f"Here are the top {len(emails)} matching emails:\n\n{snippets}\n\n"
            f"Write a concise 3-5 line summary of what these emails collectively tell you "
            f"about the user's query. Be specific -- mention names, dates, or key facts "
            f"where relevant. Do not just list the emails."
        )
        response = self._client.chat.completions.create(
            model=SUMMARY_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.3,
        )
        return response.choices[0].message.content.strip()
