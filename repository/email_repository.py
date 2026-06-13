"""
repository/email_repository.py
--------------------------------
ChromaDB abstraction layer.

All ChromaDB-specific code lives HERE and only here.
If you swap ChromaDB for Pinecone, Weaviate, or any other vector store,
this is the only file that changes -- services and API remain untouched.

Public interface:
    repo = EmailRepository()
    repo.upsert(ids, embeddings, documents, metadatas)
    repo.query(query_embedding, n_results, year=None, month=None) -> QueryResult
    repo.count() -> int
    repo.get_existing_ids() -> set[str]
    repo.get_years() -> list[int]
    repo.get_months_for_year(year) -> list[int]
    repo.clear() -> None
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

import chromadb
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv()

CHROMA_PATH = os.getenv("CHROMA_PATH", "./chroma_db")
COLLECTION_NAME = "emails"


# ---------------------------------------------------------------------------
# Data transfer objects
# ---------------------------------------------------------------------------

@dataclass
class QueryResult:
    """Raw result from a vector similarity query."""
    ids: list[str]
    metadatas: list[dict[str, Any]]
    distances: list[float]


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------

class EmailRepository:
    """
    Thin abstraction over ChromaDB.

    Instantiate once and reuse -- the underlying client is cached on the
    instance so repeated calls don't re-open the DB file.
    """

    def __init__(self, chroma_path: str = CHROMA_PATH) -> None:
        self._client = chromadb.PersistentClient(path=chroma_path)
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def upsert(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict[str, Any]],
    ) -> None:
        """Insert or update a batch of email vectors."""
        try:
            self._collection.upsert(
                ids=ids,
                embeddings=embeddings,
                documents=documents,
                metadatas=metadatas,
            )
            logger.info(f"[Repository] Upserted {len(ids)} emails into ChromaDB")
        except Exception as exc:
            logger.error(f"[Repository] upsert failed: {exc}", exc_info=True)
            raise

    def clear(self) -> None:
        """Remove all emails from the collection (destructive)."""
        try:
            self._client.delete_collection(COLLECTION_NAME)
            self._collection = self._client.get_or_create_collection(
                name=COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
            )
            logger.info("[Repository] Collection cleared and recreated")
        except Exception as exc:
            logger.error(f"[Repository] clear failed: {exc}", exc_info=True)
            raise

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def count(self) -> int:
        """Return total number of indexed emails."""
        try:
            return self._collection.count()
        except Exception as exc:
            logger.error(f"[Repository] count failed: {exc}", exc_info=True)
            raise

    def get_existing_ids(self) -> set[str]:
        """Return all IDs currently stored (used for deduplication on ingest)."""
        try:
            result = self._collection.get(include=[])
            return set(result["ids"])
        except Exception as exc:
            logger.error(f"[Repository] get_existing_ids failed: {exc}", exc_info=True)
            raise

    def query(
        self,
        query_embedding: list[float],
        n_results: int,
        year: Optional[int] = None,
        month: Optional[int] = None,
    ) -> QueryResult:
        """
        Run a cosine similarity search with optional year and/or month filters.

        Args:
            query_embedding: Embedding vector for the user query.
            n_results:        Number of results to return.
            year:             If provided, restrict to emails from that calendar year.
            month:            If provided alongside year, restrict to that month (1-12).

        Returns:
            QueryResult with parallel lists of ids, metadatas, distances.
        """
        n_results = min(n_results, self.count())
        if n_results == 0:
            return QueryResult(ids=[], metadatas=[], distances=[])

        where: Optional[dict] = None
        if year is not None and month is not None:
            where = {"$and": [{"year": {"$eq": year}}, {"month": {"$eq": month}}]}
        elif year is not None:
            where = {"year": {"$eq": year}}

        kwargs: dict[str, Any] = dict(
            query_embeddings=[query_embedding],
            n_results=n_results,
            include=["metadatas", "distances"],
        )
        if where is not None:
            kwargs["where"] = where

        try:
            raw = self._collection.query(**kwargs)
            return QueryResult(
                ids=raw["ids"][0],
                metadatas=raw["metadatas"][0],
                distances=raw["distances"][0],
            )
        except Exception as exc:
            logger.error(f"[Repository] query failed (year={year}, month={month}): {exc}", exc_info=True)
            raise

    def get_years(self) -> list[int]:
        """
        Return a sorted list of distinct years present in the collection.
        Year 0 is excluded -- it represents emails with missing/invalid dates.
        """
        try:
            if self.count() == 0:
                return []
            result = self._collection.get(include=["metadatas"])
            years: set[int] = set()
            for meta in result["metadatas"]:
                y = meta.get("year", 0)
                if isinstance(y, (int, float)) and int(y) > 0:
                    years.add(int(y))
            return sorted(years)
        except Exception as exc:
            logger.error(f"[Repository] get_years failed: {exc}", exc_info=True)
            raise

    def get_months_for_year(self, year: int) -> list[int]:
        """
        Return a sorted list of distinct months (1-12) present for a given year.
        Month 0 is excluded -- it represents emails with missing/invalid dates.
        """
        try:
            if self.count() == 0:
                return []
            result = self._collection.get(
                where={"year": {"$eq": year}},
                include=["metadatas"],
            )
            months: set[int] = set()
            for meta in result["metadatas"]:
                m = meta.get("month", 0)
                if isinstance(m, (int, float)) and 1 <= int(m) <= 12:
                    months.add(int(m))
            return sorted(months)
        except Exception as exc:
            logger.error(f"[Repository] get_months_for_year failed (year={year}): {exc}", exc_info=True)
            raise

    def count_for_year(self, year: int) -> int:
        """Return the total number of indexed emails for a given calendar year."""
        try:
            if self.count() == 0:
                return 0
            result = self._collection.get(
                where={"year": {"$eq": year}},
                include=[],
            )
            return len(result["ids"])
        except Exception as exc:
            logger.error(f"[Repository] count_for_year failed (year={year}): {exc}", exc_info=True)
            raise
