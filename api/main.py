"""
api/main.py
-----------
FastAPI application -- the single contract between any UI and the backend.

Endpoints:
  GET  /status              Health check + indexed email count
  GET  /years               Sorted list of years present in indexed emails
  GET  /months/{year}       Sorted list of months present for a given year
  GET  /count/year/{year}   Total email count for a specific calendar year
  POST /search              Semantic search with AI summary (optional year+month filter)
  POST /ingest              Start async ingestion -- returns 202 + job_id immediately
  GET  /jobs/{job_id}       Poll ingestion job status and progress
  GET  /debug/scan          File scan debug (remove before production)

Run:
  python -m uvicorn api.main:app --reload --port 8000

Both Streamlit (Phase 1) and React + Tailwind (Phase 2) call these same
endpoints. Swapping the UI requires zero changes here.
"""

import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv

# Load .env from project root regardless of where uvicorn is invoked from
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# ---------------------------------------------------------------------------
# Logging — central config (runs once at startup, inherited by all modules)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(),                          # uvicorn console
        logging.FileHandler("email_intelligence.log"),   # persistent log file
    ],
)
logger = logging.getLogger(__name__)

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from pydantic import BaseModel, Field

from repository.email_repository import EmailRepository
from services.search_service import SearchService, SearchResult
from services.ingest_service import IngestService, IngestResult

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Email Intelligence Search API",
    description="Semantic email search powered by OpenAI embeddings + ChromaDB",
    version="1.3.0",
)

# Allow Streamlit (8501) and React dev servers (3000 / 5173) to call the API.
# Override via CORS_ORIGINS env var before any shared deployment.
origins = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:8501,http://localhost:3000,http://localhost:5173",
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# In-memory job store for async ingestion tracking
# ---------------------------------------------------------------------------

_jobs: dict[str, dict] = {}


def _run_ingest_job(job_id: str, directory: str, year: Optional[int] = None) -> None:
    """
    Executed by FastAPI BackgroundTasks on a worker thread.
    Writes progress into _jobs[job_id] so GET /jobs/{job_id} can poll it.

    year=None  → ingest all years in the directory
    year=2023  → ingest only 2023 emails (PST filter runs at item level)
    """
    _jobs[job_id]["status"] = "running"
    _jobs[job_id]["started_at"] = datetime.now(timezone.utc).isoformat()

    def on_progress(current: int, total: int, message: str) -> None:
        # current is already a percentage (0-100) from the two-phase service
        _jobs[job_id]["progress"] = min(current, 100)
        _jobs[job_id]["message"] = message

    try:
        service = IngestService(repo=get_repo(), openai_client=get_openai())
        result: IngestResult = service.ingest(
            directory, year=year, progress_callback=on_progress
        )

        if result.error:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = result.error
        else:
            year_label = f" for {result.year}" if result.year else ""
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["progress"] = 100
            _jobs[job_id]["result"] = {
                "directory": result.directory,
                "year": result.year,
                "total_found": result.total_found,
                "new_ingested": result.new_ingested,
                "skipped_duplicates": result.skipped_duplicates,
                "message": (
                    f"Ingested {result.new_ingested:,} new emails{year_label}. "
                    f"Skipped {result.skipped_duplicates:,} duplicates."
                ),
            }
    except Exception as exc:
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["error"] = str(exc)
    finally:
        _jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Dependency factories (singletons -- created once, reused across all requests)
# ---------------------------------------------------------------------------

_repo: EmailRepository | None = None
_openai_client: OpenAI | None = None


def get_repo() -> EmailRepository:
    global _repo
    if _repo is None:
        _repo = EmailRepository()
    return _repo


def get_openai() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise HTTPException(status_code=500, detail="OPENAI_API_KEY not configured.")
        _openai_client = OpenAI(api_key=api_key)
    return _openai_client


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, description="Natural language search query")
    top_k: int = Field(default=10, ge=1, le=50, description="Number of results to return")
    year: Optional[int] = Field(
        default=None,
        description="Filter to a specific calendar year (e.g. 2023). Omit for all years.",
    )
    month: Optional[int] = Field(
        default=None,
        ge=1,
        le=12,
        description="Filter to a specific month (1-12). Only applied when year is also set.",
    )


class EmailMatchResponse(BaseModel):
    rank: int
    id: str
    subject: str
    from_: str
    to: str
    date: str
    body_preview: str
    relevance_pct: float
    source: str


class SearchResponse(BaseModel):
    query: str
    summary: str
    emails: list[EmailMatchResponse]
    total_indexed: int


class IngestRequest(BaseModel):
    directory: str = Field(..., description="Absolute path to folder containing .pst/.mbox/.eml files")
    year: Optional[int] = Field(
        default=None,
        description=(
            "Calendar year to ingest (e.g. 2023). Only emails from that year are "
            "read and indexed. Leave null to ingest all years in the directory."
        ),
    )


class IngestAcceptedResponse(BaseModel):
    job_id: str
    status: Literal["queued"]
    message: str
    poll_url: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: Literal["queued", "running", "done", "error"]
    progress: int = Field(ge=0, le=100, description="Completion percentage 0-100")
    message: str
    result: dict | None = None
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class StatusResponse(BaseModel):
    status: str
    total_indexed: int
    chroma_path: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/debug/scan", tags=["Debug"])
def debug_scan(directory: str):
    """List all files in a directory -- verify path visibility. Remove before production."""
    directory = os.path.expanduser(directory)
    if not os.path.isdir(directory):
        return {"error": f"Not a directory: {directory}", "cwd": os.getcwd()}
    files = []
    for root, _, fnames in os.walk(directory):
        for fname in fnames:
            fpath = os.path.join(root, fname)
            files.append({
                "path": fpath,
                "ext": Path(fname).suffix.lower(),
                "size_bytes": os.path.getsize(fpath),
            })
    return {"directory": directory, "cwd": os.getcwd(), "file_count": len(files), "files": files}


@app.get("/status", response_model=StatusResponse, tags=["Health"])
def status():
    """Health check -- returns API status and number of indexed emails."""
    try:
        repo = get_repo()
        return StatusResponse(
            status="ok",
            total_indexed=repo.count(),
            chroma_path=os.getenv("CHROMA_PATH", "./chroma_db"),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/years", response_model=list[int], tags=["Search"])
def get_years():
    """
    Return a sorted list of distinct years present in the indexed emails.
    Use this to populate the year filter dropdown in the UI.
    """
    try:
        return get_repo().get_years()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/months/{year}", response_model=list[int], tags=["Search"])
def get_months(year: int):
    """
    Return a sorted list of distinct months (1-12) present for a given year.
    Use this to populate the month filter dropdown after a year is selected.
    """
    try:
        return get_repo().get_months_for_year(year)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/count/year/{year}", response_model=int, tags=["Search"])
def get_count_for_year(year: int):
    """
    Return the total number of indexed emails for a given calendar year.
    Used by the UI to display an email count badge next to the year filter.
    """
    try:
        return get_repo().count_for_year(year)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/search", response_model=SearchResponse, tags=["Search"])
def search(request: SearchRequest):
    """
    Semantic search across indexed emails.
    Returns an AI-generated summary and a ranked list of matching emails.
    Optional year and month filters restrict results to a specific period.
    """
    try:
        service = SearchService(repo=get_repo(), openai_client=get_openai())
        result: SearchResult = service.search(
            request.query,
            top_k=request.top_k,
            year=request.year,
            month=request.month,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if result.error:
        raise HTTPException(status_code=400, detail=result.error)

    return SearchResponse(
        query=result.query,
        summary=result.summary,
        total_indexed=result.total_indexed,
        emails=[
            EmailMatchResponse(
                rank=e.rank,
                id=e.id,
                subject=e.subject,
                from_=e.from_,
                to=e.to,
                date=e.date,
                body_preview=e.body_preview,
                relevance_pct=e.relevance_pct,
                source=e.source,
            )
            for e in result.emails
        ],
    )


@app.post("/ingest", response_model=IngestAcceptedResponse, status_code=202, tags=["Ingestion"])
def ingest(request: IngestRequest, background_tasks: BackgroundTasks):
    """
    Start an async ingestion job. Returns 202 Accepted immediately with a job_id.

    The actual parsing + embedding runs in the background.
    Poll GET /jobs/{job_id} to track progress and retrieve results.

    Idempotent -- already-indexed emails are skipped automatically.

    year=null  → ingest all years found in the directory
    year=2023  → ingest only emails from 2023 (PST filter at item level)
    """
    if not os.path.isdir(request.directory):
        raise HTTPException(status_code=400, detail=f"Directory not found: {request.directory}")

    year_label = f" for year {request.year}" if request.year else " (all years)"
    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "status": "queued",
        "progress": 0,
        "message": f"Queued — will ingest emails{year_label}",
        "result": None,
        "error": None,
        "started_at": None,
        "finished_at": None,
    }

    background_tasks.add_task(_run_ingest_job, job_id, request.directory, request.year)

    return IngestAcceptedResponse(
        job_id=job_id,
        status="queued",
        message=f"Ingestion started{year_label}. Poll /jobs/{{job_id}} for progress.",
        poll_url=f"/jobs/{job_id}",
    )


@app.get("/jobs/{job_id}", response_model=JobStatusResponse, tags=["Ingestion"])
def get_job(job_id: str):
    """
    Poll the status of an ingestion job.

    status values:
      queued  -- accepted, not yet started
      running -- parsing and embedding in progress (check progress 0-100)
      done    -- completed successfully (check result field)
      error   -- failed (check error field)
    """
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    job = _jobs[job_id]
    return JobStatusResponse(
        job_id=job_id,
        status=job["status"],
        progress=job["progress"],
        message=job["message"],
        result=job.get("result"),
        error=job.get("error"),
        started_at=job.get("started_at"),
        finished_at=job.get("finished_at"),
    )
