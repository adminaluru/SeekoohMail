"""
presentation/app.py -- Streamlit UI (Phase 1)
---------------------------------------------
Calls FastAPI via HTTP -- no direct imports from services or repository.

This is the ONLY file that gets replaced when you swap to React + Tailwind.
The FastAPI endpoints (/status, /years, /months, /search, /ingest, /jobs/{id})
remain unchanged.

Run order:
  1. python -m uvicorn api.main:app --reload --port 8000   (terminal 1)
  2. python -m streamlit run presentation/app.py            (terminal 2)
"""

import os
import time
import calendar

import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

API_BASE = os.getenv("API_BASE_URL", "http://localhost:8000")

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="SeekooHMail",
    page_icon="📧",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# API helpers -- thin wrappers over FastAPI endpoints
# ---------------------------------------------------------------------------

def api_status() -> dict | None:
    try:
        r = requests.get(f"{API_BASE}/status", timeout=(5, 10))
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def api_years() -> list[int]:
    """Fetch the list of years present in the indexed emails."""
    try:
        r = requests.get(f"{API_BASE}/years", timeout=(5, 10))
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


def api_months(year: int) -> list[int]:
    """Fetch the list of months present for a given year."""
    try:
        r = requests.get(f"{API_BASE}/months/{year}", timeout=(5, 10))
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


def api_count_for_year(year: int) -> int:
    """Fetch total indexed email count for a specific calendar year."""
    try:
        r = requests.get(f"{API_BASE}/count/year/{year}", timeout=(5, 10))
        r.raise_for_status()
        return r.json()
    except Exception:
        return 0


def api_search(query: str, top_k: int, year: int | None = None, month: int | None = None) -> dict:
    payload: dict = {"query": query, "top_k": top_k}
    if year is not None:
        payload["year"] = year
    if year is not None and month is not None:
        payload["month"] = month
    r = requests.post(
        f"{API_BASE}/search",
        json=payload,
        timeout=(5, 60),
    )
    r.raise_for_status()
    return r.json()


def api_ingest_start(directory: str, year: int | None = None) -> str:
    """Start async ingest job. Returns job_id immediately (non-blocking)."""
    payload: dict = {"directory": directory}
    if year is not None:
        payload["year"] = year
    r = requests.post(
        f"{API_BASE}/ingest",
        json=payload,
        timeout=(5, 15),
    )
    r.raise_for_status()
    return r.json()["job_id"]


def api_job_status(job_id: str) -> dict:
    """Poll a running ingest job. Returns full job dict."""
    r = requests.get(f"{API_BASE}/jobs/{job_id}", timeout=(5, 10))
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.title("Settings")

    # API health indicator
    status = api_status()
    if status:
        st.success(f"API connected -- {status['total_indexed']} emails indexed")
    else:
        st.error("API offline -- start FastAPI first:\n`python -m uvicorn api.main:app --reload`")

    st.divider()
    st.subheader("Ingest Emails")
    email_dir = st.text_input(
        "Email folder path",
        placeholder="Y:\\Ai POC Projects\\email-intelligence\\emails",
        help="Folder containing .pst, .mbox, or .eml files",
    )

    ingest_year_str = st.text_input(
        "Year to ingest (optional)",
        placeholder="e.g. 2023  — leave blank to ingest all years",
        help=(
            "Enter a 4-digit year to load only that year's emails from the PST. "
            "Leave blank to load all years at once (e.g. if you have 2 years of data)."
        ),
    )
    # Parse year — None means all years
    ingest_year: int | None = None
    if ingest_year_str.strip():
        try:
            ingest_year = int(ingest_year_str.strip())
            if ingest_year < 1970 or ingest_year > 2100:
                st.warning("Year looks invalid — enter a 4-digit year like 2023.")
                ingest_year = None
        except ValueError:
            st.warning("Year must be a number, e.g. 2023.")

    if ingest_year:
        st.caption(f"Will ingest **{ingest_year}** emails only.")
    else:
        st.caption("Will ingest **all years** found in the folder.")

    if st.button("Run Ingestion", use_container_width=True):
        if not email_dir:
            st.error("Please enter a folder path.")
        else:
            try:
                job_id = api_ingest_start(email_dir, year=ingest_year)
                st.session_state["ingest_job_id"] = job_id
                st.session_state["ingest_done"] = False
            except requests.HTTPError as exc:
                detail = exc.response.json().get("detail", str(exc))
                st.error(f"Could not start ingestion: {detail}")
            except Exception as exc:
                st.error(f"Could not start ingestion: {exc}")

    # Progress polling -- active on every rerun while job is in flight
    job_id = st.session_state.get("ingest_job_id")
    if job_id and not st.session_state.get("ingest_done"):
        try:
            job = api_job_status(job_id)
            icon = {"queued": "...", "running": "...", "done": "Done", "error": "Failed"}.get(
                job["status"], "?"
            )
            st.progress(
                job["progress"] / 100,
                text=f"{icon}  {job['message']}  ({job['progress']}%)",
            )

            if job["status"] == "done":
                res = job["result"]
                st.success(res["message"])
                st.caption(
                    f"Found: {res['total_found']} | "
                    f"New: {res['new_ingested']} | "
                    f"Skipped: {res['skipped_duplicates']}"
                )
                st.session_state["ingest_done"] = True
                st.session_state.pop("last_result", None)  # invalidate search cache
            elif job["status"] == "error":
                st.error(f"Ingestion failed: {job['error']}")
                st.session_state["ingest_done"] = True
            else:
                # Still running -- wait 2s then rerun to poll again
                time.sleep(2)
                st.rerun()
        except Exception as exc:
            st.warning(f"Could not poll job status: {exc}")

    st.divider()
    top_k = st.slider("Results to retrieve", min_value=3, max_value=40, value=10)

    st.divider()
    st.subheader("Filter by Period")

    # Year dropdown
    available_years = api_years()
    if available_years:
        year_options = ["All Years"] + [str(y) for y in available_years]
        selected_year_label = st.selectbox(
            "Year",
            options=year_options,
            index=0,
            help="Restrict search to a specific year.",
        )
        selected_year = None if selected_year_label == "All Years" else int(selected_year_label)
        if selected_year is not None:
            year_count = api_count_for_year(selected_year)
            st.caption(f"📧 {year_count:,} emails in {selected_year}")
    else:
        st.caption("No data yet -- run ingestion first.")
        selected_year = None

    # Month dropdown -- only shown when a year is selected
    selected_month = None
    if selected_year is not None:
        available_months = api_months(selected_year)
        if available_months:
            month_options = ["All Months"] + [calendar.month_name[m] for m in available_months]
            selected_month_label = st.selectbox(
                "Month",
                options=month_options,
                index=0,
                help="Restrict search to a specific month within the selected year.",
            )
            if selected_month_label != "All Months":
                selected_month = list(calendar.month_name).index(selected_month_label)
        else:
            st.caption(f"No month data for {selected_year}.")

    st.divider()
    st.caption("Phase 1 -- Streamlit UI\nPhase 2 -- React + Tailwind (same API)")

# ---------------------------------------------------------------------------
# Main search UI
# ---------------------------------------------------------------------------
st.title("Email Intelligence Search")
st.caption("Type a natural language query. AI finds the most relevant emails and summarizes the findings.")

query = st.text_input(
    label="Search your emails",
    placeholder='e.g. "budget approval from finance team last quarter"',
    label_visibility="collapsed",
)

search_clicked = st.button("Search", type="primary")

if search_clicked:
    if not query.strip():
        st.warning("Please enter a search query.")
    else:
        with st.spinner("Searching..."):
            try:
                data = api_search(query.strip(), top_k, year=selected_year, month=selected_month)
                # Store in session_state -- survives expander clicks and reruns
                st.session_state["last_result"] = data
                st.session_state["last_query"] = query
            except requests.HTTPError as exc:
                detail = exc.response.json().get("detail", str(exc))
                st.session_state["last_result"] = {"error": detail}
            except Exception as exc:
                st.session_state["last_result"] = {"error": str(exc)}

# Render from session_state (persists across all reruns)
data = st.session_state.get("last_result")

if data:
    if "error" in data:
        st.error(f"{data['error']}")
    else:
        st.subheader("AI Summary")
        st.info(data["summary"])
        st.caption(f"Searched across {data['total_indexed']} indexed emails")

        st.divider()

        st.subheader(f"Top {len(data['emails'])} Matching Emails")
        for e in data["emails"]:
            date_short = e["date"][:10] if e.get("date") else ""
            with st.expander(
                f"#{e['rank']} -- {e['subject'] or '(no subject)'}  "
                f"| {e['from_'] or 'Unknown'}  "
                f"| {date_short}  "
                f"| Relevance: {e['relevance_pct']}%"
            ):
                col1, col2 = st.columns(2)
                with col1:
                    st.markdown(f"**From:** {e['from_']}")
                    st.markdown(f"**To:** {e['to']}")
                with col2:
                    st.markdown(f"**Date:** {e['date'][:19] if e.get('date') else 'Unknown'}")
                    st.markdown(f"**Source:** `{os.path.basename(e.get('source', ''))}`")
                st.markdown("**Preview:**")
                preview = e.get("body_preview", "")
                st.text(preview[:600] + ("..." if len(preview) > 600 else ""))

# Empty state
if not query and st.session_state.get("last_result") is None:
    st.markdown(
        """
        <div style='text-align:center; margin-top:60px; color:#888;'>
            <h3>Start by searching</h3>
            <p>Example queries:</p>
            <ul style='list-style:none; padding:0;'>
                <li>"invoices from 2024"</li>
                <li>"meeting reschedule from John"</li>
                <li>"project deadline extension requests"</li>
                <li>"password reset emails"</li>
            </ul>
            <br/>
            <small>First time? Add your email folder path in the sidebar and click Run Ingestion.</small>
        </div>
        """,
        unsafe_allow_html=True,
    )
