# ExecuDash MPP Parser

A Railway-hosted FastAPI microservice that accepts binary Microsoft Project (`.mpp`)
file uploads, parses them using [MPXJ](https://www.mpxj.org/) via
[JPype](https://jpype.readthedocs.io/), and returns structured task data as JSON.

---

## Architecture

### Why Railway?

Vercel (which hosts the ExecuDash frontend) runs serverless functions that cannot
host a persistent JVM process. Railway provides always-on containers with full
control over the process lifecycle — a requirement for JPype's JVM singleton.

### Why MPXJ + JPype?

Microsoft Project `.mpp` files are proprietary binary formats. MPXJ is the only
robust open-source library capable of reading them across all versions (MPP8 through
MPP14+). MPXJ is a Java library; JPype bridges Python and Java within the same
process, allowing FastAPI to call MPXJ without subprocess overhead or serialization
round-trips.

### Data Flow

```
Browser (Admin.tsx)
  │  multipart/form-data  POST /parse-mpp
  ▼
Railway FastAPI (this service)
  │  MPXJ UniversalProjectReader (JVM)
  │  Parses binary .mpp → ProjectFile object
  │  Iterates tasks → Python dicts
  ▼
JSON response  { success, filename, task_count, tasks[] }
  │
  ▼
Admin.tsx  →  supabase.functions.invoke("ingest-project-file", { tasks, … })
  │
  ▼
Supabase Edge Function  →  task_assignments table
```

### Key Design Decisions

| Decision | Rationale |
|---|---|
| `--workers 1` | JVM is a per-process singleton; multiple workers cause startup conflicts |
| Shell-form `CMD` in Dockerfile | JSON-array CMD does not expand `$PORT` shell variables |
| `UniversalProjectReader` | Handles all MPP versions automatically; `MPPReader` is version-specific |
| `org.mpxj` namespace | MPXJ v13+ migrated from `net.sf.mpxj`; older namespace causes `ClassNotFoundException` |
| No `jpype.shutdownJVM()` | JPype's atexit hook handles this; manual shutdown causes segfaults |

---

## Local Development

### Prerequisites

- Python 3.11+
- Java 11+ (OpenJDK recommended) — verify with `java -version`

### Setup

```bash
cd mpp-parser-service

# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate          # Linux / macOS
# .venv\Scripts\activate           # Windows

# Install dependencies
pip install -r requirements.txt

# Copy environment variable template
cp .env.example .env
# Edit .env if needed (defaults work for local development)
```

### Run

```bash
# IMPORTANT: --workers 1 is mandatory (JVM singleton constraint)
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1 --reload
```

The service will be available at `http://localhost:8000`.

Note: First startup takes 3-8 seconds for JVM initialization and MPXJ class loading.
Subsequent requests are fast.

### Smoke Test

```bash
# Health check
curl http://localhost:8000/health

# Parse an MPP file
curl -X POST http://localhost:8000/parse-mpp \
  -F "file=@/path/to/your/project.mpp"
```

---

## Railway Deployment

### Initial Setup

1. Push this directory (or the entire `execdash` repo) to GitHub.
2. Go to [railway.app](https://railway.app) and create a new project.
3. Select "Deploy from GitHub repo" and choose your repository.
4. If the `mpp-parser-service` directory is a subdirectory of a larger repo,
   set the **Root Directory** in Railway's service settings to `mpp-parser-service`.
5. Railway detects `Dockerfile` automatically and uses it for the build.
6. Railway reads `railway.json` for health check and restart policy configuration.

### Environment Variables

Set these in Railway's "Variables" tab for the service:

| Variable | Required | Example |
|---|---|---|
| `ALLOWED_ORIGINS` | Yes | `https://execudashboard.vercel.app` |
| `PORT` | No — Railway injects automatically | (do not set) |

### Verify Deployment

After deployment, Railway shows a generated URL (e.g., `https://mpp-parser-service-production.up.railway.app`).

```bash
# Replace with your actual Railway URL
RAILWAY_URL=https://mpp-parser-service-production.up.railway.app

curl $RAILWAY_URL/health
# Expected: {"status":"healthy","jvm_started":true}

curl -X POST $RAILWAY_URL/parse-mpp \
  -F "file=@project.mpp"
```

---

## API Reference

### `GET /`

Service identity. Used to confirm the service is reachable.

**Response 200:**
```json
{
  "service": "ExecuDash MPP Parser",
  "version": "1.0.0",
  "status": "running"
}
```

---

### `GET /health`

Health check endpoint polled by Railway's load balancer every 30 seconds.

**Response 200:**
```json
{
  "status": "healthy",
  "jvm_started": true
}
```

---

### `POST /parse-mpp`

Upload a binary `.mpp` file and receive structured task data.

**Request:**
- Content-Type: `multipart/form-data`
- Field name: `file`
- File must have `.mpp` extension

**Example (curl):**
```bash
curl -X POST https://<your-railway-url>/parse-mpp \
  -F "file=@project_schedule.mpp"
```

**Example (JavaScript / fetch):**
```javascript
const formData = new FormData();
formData.append("file", mppFile);

const response = await fetch("https://<your-railway-url>/parse-mpp", {
  method: "POST",
  body: formData,
});

const result = await response.json();
```

**Response 200:**
```json
{
  "success": true,
  "filename": "project_schedule.mpp",
  "task_count": 142,
  "tasks": [
    {
      "id": 1,
      "wbs": "1",
      "name": "Project Kickoff",
      "outline_level": 1,
      "start_date": "2026-01-15T08:00:00",
      "finish_date": "2026-01-15T17:00:00",
      "duration_days": 1.0,
      "percent_complete": 100.0,
      "is_milestone": true,
      "summary": false
    },
    {
      "id": 2,
      "wbs": "2",
      "name": "Phase 1: Planning",
      "outline_level": 1,
      "start_date": "2026-01-16T08:00:00",
      "finish_date": "2026-02-28T17:00:00",
      "duration_days": 44.0,
      "percent_complete": 75.0,
      "is_milestone": false,
      "summary": true
    }
  ]
}
```

**Response 400 — invalid file type:**
```json
{
  "detail": "Invalid file type. Expected a .mpp file, got '.xlsx'."
}
```

**Response 400 — empty file:**
```json
{
  "detail": "Uploaded file is empty."
}
```

**Response 500 — parse failure:**
```json
{
  "detail": "Failed to parse the MPP file: <error details>. Ensure the file is a valid Microsoft Project .mpp file."
}
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `ALLOWED_ORIGINS` | `*` | Comma-separated list of allowed CORS origins. Use `*` for development; restrict to your frontend domain(s) in production. Example: `https://execudashboard.vercel.app` |
| `PORT` | `8000` | TCP port uvicorn listens on. **Injected automatically by Railway** — do not set this in the Railway dashboard. For local dev, set in `.env`. |

---

## Important Notes

### Workers constraint

This service MUST run with `--workers 1`. The JVM started by JPype is a singleton
within a process. If uvicorn spawns multiple workers, each worker process attempts
to start its own JVM — this causes race conditions and process crashes. The
`Dockerfile` `CMD` already enforces `--workers 1`.

### JVM startup latency

The first HTTP request after a cold start may take 3-8 seconds while the JVM
initializes and MPXJ's class loader resolves the project reader classes. Railway's
health check timeout is set to 120 seconds in `railway.json` to account for this.
Subsequent requests have no JVM startup penalty.

### File size limits

FastAPI and uvicorn do not impose a hard file size limit by default. However,
Railway's free tier has memory constraints. MPP files are typically 1-50 MB;
files over 100 MB may cause memory pressure. If you need to handle very large
files, consider adding a `MAX_UPLOAD_MB` environment variable and enforcing it
in `parse_mpp()` before writing to disk.

### Supported MPP versions

`UniversalProjectReader` handles all Microsoft Project file versions that MPXJ
supports: MPP8 (Project 98), MPP9 (Project 2000/2002), MPP12 (Project 2003),
MPP14 (Project 2007/2010/2013/2016/2019/2021), and the XML-based `.mspdi` format.

### MPXJ namespace (v13+)

MPXJ version 13 migrated its Java package namespace from `net.sf.mpxj` to `org.mpxj`.
This service uses `org.mpxj.reader.UniversalProjectReader`. If you downgrade to an
older MPXJ version, you must update this import.
