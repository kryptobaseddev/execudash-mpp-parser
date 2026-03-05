import asyncio
import os
import tempfile
import threading
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


# Global readiness state — written by background thread, read by endpoints
_jvm_ready = threading.Event()
_jvm_error: str | None = None


def _start_jvm_background() -> None:
    """Initialize JVM and MPXJ in a daemon thread so uvicorn binds immediately."""
    global _jvm_error
    try:
        logger.info("Background JVM startup beginning...")

        if not jpype.isJVMStarted():
            jvm_path = jpype.getDefaultJVMPath()
            logger.info("JVM path resolved to: %s", jvm_path)
            # Log the pre-startup classpath (registered by `import mpxj` via addClassPath)
            registered_cp_str = jpype.getClassPath()
            registered_cp_list = [p for p in registered_cp_str.split(os.pathsep) if p]
            logger.info("Pre-startJVM classpath: %d JARs", len(registered_cp_list))
            # Verify the JARs actually exist and are readable on the filesystem
            missing = [p for p in registered_cp_list if not os.path.isfile(p)]
            readable = [p for p in registered_cp_list if os.path.isfile(p) and os.access(p, os.R_OK)]
            logger.info("JAR check: %d exist+readable, %d missing: %s",
                        len(readable), len(missing), missing[:3] if missing else "none")
            # Call startJVM with NO explicit classpath — let JPype use its internal
            # _CLASSPATHS registry (populated by `import mpxj`). This is the documented
            # correct pattern: import mpxj → startJVM() → from org.mpxj...
            jpype.startJVM(convertStrings=False)
            logger.info("JVM started successfully")
            # Check post-startup classloader state
            jvm_cp = str(jpype.java.lang.System.getProperty("java.class.path"))
            jpype_cp = str(jpype.java.lang.System.getProperty("jpype.class.path"))
            sys_cl = jpype.java.lang.ClassLoader.getSystemClassLoader()
            logger.info(
                "Post-start: java.class.path len=%d, mpxj.jar=%s, jpype.class.path=%s, syscl=%s",
                len(jvm_cp), "mpxj.jar" in jvm_cp, jpype_cp,
                sys_cl.getClass().getName()
            )
            # Try loading via Class.forName with AppClassLoader directly
            try:
                cls = jpype.java.lang.Class.forName(
                    "org.mpxj.reader.UniversalProjectReader", True, sys_cl
                )
                logger.info("Class.forName with AppClassLoader SUCCEEDED: %s", cls)
            except Exception as fn_err:
                logger.info("Class.forName with AppClassLoader FAILED: %s | cause: %s",
                            fn_err, getattr(fn_err, '__cause__', None))
            # Try JClass as alternative loading mechanism
            try:
                UPR_via_jclass = jpype.JClass("org.mpxj.reader.UniversalProjectReader")
                logger.info("JClass loading SUCCEEDED: %s", UPR_via_jclass)
            except Exception as jc_err:
                logger.info("JClass loading FAILED: %s | cause: %s",
                            jc_err, getattr(jc_err, '__cause__', None))
        else:
            logger.info("JVM already running (started externally)")

        # Attempt 1: standard Python import syntax (works when JARs are on classpath)
        try:
            from org.mpxj.reader import UniversalProjectReader  # noqa: F401
            logger.info("UniversalProjectReader loaded via direct import")
        except ImportError as imp_err:
            # Log the chained Java exception so we know the real cause
            logger.warning(
                "Direct import failed: %s | cause: %s", imp_err, imp_err.__cause__
            )
            # Attempt 2: JPackage fallback (same classpath, different access path)
            logger.info("Trying JPackage fallback...")
            org = jpype.JPackage("org")
            _reader_class = org.mpxj.reader.UniversalProjectReader
            # Instantiate to confirm it's a real class (JPackage returns a stub if missing)
            _test = _reader_class()
            logger.info("UniversalProjectReader loaded via JPackage fallback")

        logger.info("JVM started and MPXJ warmed successfully")
        _jvm_ready.set()
    except Exception as exc:
        cause = getattr(exc, "__cause__", None)
        _jvm_error = str(exc)
        logger.error(
            "JVM startup failed: %s | cause: %s", exc, cause, exc_info=True
        )
        # Still set event so /parse-mpp can return the error rather than hang
        _jvm_ready.set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start JVM in daemon thread — uvicorn binds port immediately
    t = threading.Thread(target=_start_jvm_background, daemon=True)
    t.start()
    logger.info("JVM startup thread launched, server accepting connections")
    yield
    # No shutdownJVM() — let process exit naturally
    logger.info("Service shutting down")


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
    """Health check used by Railway's healthcheck probe.

    Always returns HTTP 200 — even while JVM is still warming.
    Railway only needs a 200; the jvm_ready/jvm_starting fields are informational.
    """
    jvm_ready = _jvm_ready.is_set() and _jvm_error is None
    jvm_starting = not _jvm_ready.is_set()
    return {
        "status": "healthy",
        "jvm_ready": jvm_ready,
        "jvm_starting": jvm_starting,
        "jvm_error": _jvm_error,
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
    # ── Gate on JVM readiness ─────────────────────────────────────────────────
    if not _jvm_ready.is_set():
        # Wait in a thread pool so we don't block the event loop
        loop = asyncio.get_event_loop()
        ready = await loop.run_in_executor(None, lambda: _jvm_ready.wait(timeout=180))
        if not ready:
            raise HTTPException(
                status_code=503,
                detail="JVM is still initializing. Retry in a moment.",
            )

    if _jvm_error is not None:
        raise HTTPException(
            status_code=500,
            detail=f"JVM failed to start: {_jvm_error}",
        )

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
