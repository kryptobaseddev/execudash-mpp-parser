import os
import tempfile
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware

import jpype
import mpxj  # noqa: F401 — side-effect import: registers MPXJ JARs on the JVM classpath

logger = logging.getLogger("mpp_parser")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def _parse_allowed_origins() -> list[str]:
    """Parse ALLOWED_ORIGINS env var (comma-separated) into a list of origin strings."""
    raw = os.environ.get("ALLOWED_ORIGINS", "*").strip()
    if not raw:
        return ["*"]
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    return origins if origins else ["*"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────────────
    if not jpype.isJVMStarted():
        jpype.startJVM()
        logger.info("JVM started successfully")
    else:
        logger.info("JVM was already running — skipping startJVM()")

    # Warm import: triggers class-loader resolution so the first request is fast
    from org.mpxj.reader import UniversalProjectReader as _  # noqa: F401
    logger.info("MPXJ UniversalProjectReader loaded — service ready")

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────────
    # Do NOT call jpype.shutdownJVM() — JPype's atexit handler does this safely.
    # Calling it manually here causes segfaults when the process exits afterward.
    logger.info("ExecuDash MPP Parser service shutting down")


ALLOWED_ORIGINS = _parse_allowed_origins()

app = FastAPI(
    title="ExecuDash MPP Parser",
    version="1.0.0",
    description=(
        "Microservice that accepts binary Microsoft Project (.mpp) file uploads, "
        "parses them with MPXJ via JPype/JVM, and returns structured task data as JSON."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/")
async def root():
    """Service identity endpoint."""
    return {
        "service": "ExecuDash MPP Parser",
        "version": "1.0.0",
        "status": "running",
    }


@app.get("/health")
async def health():
    """Health check used by Railway's healthcheck probe."""
    return {
        "status": "healthy",
        "jvm_started": jpype.isJVMStarted(),
    }


@app.post("/parse-mpp")
async def parse_mpp(file: UploadFile = File(...)):
    """
    Accept a binary .mpp file upload, parse it with MPXJ, and return task data.

    Returns a JSON object with:
      - success (bool)
      - filename (str)
      - task_count (int)
      - tasks (list of task dicts)
    """
    # ── Validate extension ────────────────────────────────────────────────────
    filename = file.filename or ""
    if not filename.lower().endswith(".mpp"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid file type. Expected a .mpp file, "
                f"got '{os.path.splitext(filename)[1] or 'no extension'}'."
            ),
        )

    # ── Read uploaded bytes ───────────────────────────────────────────────────
    try:
        file_bytes = await file.read()
    except Exception as exc:
        logger.error("Failed to read uploaded file '%s': %s", filename, exc)
        raise HTTPException(
            status_code=500,
            detail="Could not read the uploaded file. Please try again.",
        )

    if not file_bytes:
        raise HTTPException(
            status_code=400,
            detail="Uploaded file is empty.",
        )

    # ── Write to temp file ────────────────────────────────────────────────────
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".mpp")
    os.close(tmp_fd)  # Close file descriptor — we'll write via open()

    tasks: list[dict] = []

    try:
        with open(tmp_path, "wb") as tmp_file:
            tmp_file.write(file_bytes)

        logger.info(
            "Parsing '%s' (%d bytes) via MPXJ UniversalProjectReader …",
            filename,
            len(file_bytes),
        )

        # ── Parse with MPXJ ───────────────────────────────────────────────────
        from org.mpxj.reader import UniversalProjectReader

        reader = UniversalProjectReader()
        project = reader.read(tmp_path)

        for task in project.getTasks():
            # Skip the implicit root task (ID == 0 or None) and blank tasks
            task_id = task.getID()
            task_name = task.getName()

            if task_id is None:
                continue
            if task_name is None:
                continue
            if str(task_name).strip() == "":
                continue

            # ── Duration ──────────────────────────────────────────────────────
            raw_duration = task.getDuration()
            duration_days: float = 0.0
            if raw_duration is not None:
                try:
                    duration_days = float(raw_duration.getDuration())
                except Exception:
                    duration_days = 0.0

            # ── Percent complete ──────────────────────────────────────────────
            raw_pct = task.getPercentageComplete()
            percent_complete: float = 0.0
            if raw_pct is not None:
                try:
                    percent_complete = float(raw_pct)
                except Exception:
                    percent_complete = 0.0

            # ── Milestone flag ────────────────────────────────────────────────
            raw_milestone = task.getMilestone()
            is_milestone: bool = False
            if raw_milestone is not None:
                try:
                    is_milestone = bool(raw_milestone)
                except Exception:
                    is_milestone = False

            # ── Summary flag ──────────────────────────────────────────────────
            raw_summary = task.getSummary()
            is_summary: bool = False
            if raw_summary is not None:
                try:
                    is_summary = bool(raw_summary)
                except Exception:
                    is_summary = False

            # ── WBS ───────────────────────────────────────────────────────────
            raw_wbs = task.getWBS()
            wbs: str | None = str(raw_wbs) if raw_wbs is not None else None

            # ── Outline level ─────────────────────────────────────────────────
            raw_outline = task.getOutlineLevel()
            outline_level: int = 0
            if raw_outline is not None:
                try:
                    outline_level = int(raw_outline)
                except Exception:
                    outline_level = 0

            # ── Dates ─────────────────────────────────────────────────────────
            raw_start = task.getStart()
            start_date: str | None = str(raw_start) if raw_start is not None else None

            raw_finish = task.getFinish()
            finish_date: str | None = str(raw_finish) if raw_finish is not None else None

            tasks.append(
                {
                    "id": int(task_id),
                    "wbs": wbs,
                    "name": str(task_name),
                    "outline_level": outline_level,
                    "start_date": start_date,
                    "finish_date": finish_date,
                    "duration_days": duration_days,
                    "percent_complete": percent_complete,
                    "is_milestone": is_milestone,
                    "summary": is_summary,
                }
            )

        logger.info(
            "Parsed '%s' successfully — %d tasks extracted", filename, len(tasks)
        )

    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "MPXJ parsing failed for '%s': %s", filename, exc, exc_info=True
        )
        raise HTTPException(
            status_code=500,
            detail=(
                f"Failed to parse the MPP file: {exc}. "
                "Ensure the file is a valid Microsoft Project .mpp file."
            ),
        )
    finally:
        # Always clean up the temp file regardless of success or failure
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception as cleanup_exc:
                logger.warning(
                    "Could not delete temp file '%s': %s", tmp_path, cleanup_exc
                )

    return {
        "success": True,
        "filename": filename,
        "task_count": len(tasks),
        "tasks": tasks,
    }
