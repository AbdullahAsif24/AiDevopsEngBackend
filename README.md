# 🚀 AI DevOps Engineer — Backend Service

An asynchronous, AI-powered backend service that takes any public **GitHub repository URL**, intelligently inspects the repository architecture, and automatically generates an optimized, production-ready **Dockerfile**. 

The system features an autonomous **self-healing retry loop** powered by Groq LLM inference (`llama-3.3-70b-versatile`), live WebSocket progress streaming, and strict contract validation using Pydantic.

---

## 📑 Table of Contents

- [Overview & Architecture](#-overview--architecture)
- [Tech Stack](#-tech-stack)
- [Project Directory Structure](#-project-directory-structure)
- [Getting Started](#-getting-started)
  - [Prerequisites](#prerequisites)
  - [Installation & Setup](#installation--setup)
  - [Environment Variables](#environment-variables)
  - [Running the Server](#running-the-server)
- [Complete API Reference](#-complete-api-reference)
  - [1. Health Check (`GET /health`)](#1-health-check-get-health)
  - [2. Create Job (`POST /jobs`)](#2-create-job-post-jobs)
  - [3. Get Job Status (`GET /jobs/{job_id}`)](#3-get-job-status-get-jobsjob_id)
  - [4. Real-Time WebSocket Stream (`WS /ws/jobs`)](#4-real-time-websocket-stream-ws-wsjobs)
- [Job Lifecycle & State Machine](#-job-lifecycle--state-machine)
- [Data Models & Contracts](#-data-models--contracts)
- [Self-Healing Engine & DevOps Integration](#-self-healing-engine--devops-integration)
- [Supported Frameworks & Stacks](#-supported-frameworks--stacks)
- [Interactive API Documentation](#-interactive-api-documentation)

---

## 🏗 Overview & Architecture

The backend operates as an asynchronous pipeline with background job execution to guarantee non-blocking HTTP responses.

```
                  ┌─────────────────────────────────────────────────────────┐
                  │                    React Frontend                       │
                  └───────────────┬─────────────────────────▲───────────────┘
                                  │                         │
                 1. POST /jobs    │                         │ 4. WS /ws/jobs
                 (Repo URL)       │                         │ (Live JobEvents)
                                  ▼                         │
┌───────────────────────────────────────────────────────────┴─────────────────────────┐
│ FastAPI Backend Server                                                              │
│                                                                                     │
│  ┌─────────────────┐       ┌──────────────────────┐       ┌──────────────────────┐  │
│  │  Route Handlers │ ───►  │ In-Memory Job Store  │ ◄───  │ WebSocket Event Hub  │  │
│  │ (Async / 202)   │       │   (Thread-Safe Lock) │       │ (Broadcaster)        │  │
│  └────────┬────────┘       └──────────────────────┘       └──────────▲───────────┘  │
│           │                                                          │              │
│           ▼ (async task)                                             │              │
│  ┌───────────────────────────────────────────────────────────────────┴───────────┐  │
│  │ Job Pipeline (Background Worker)                                              │  │
│  │                                                                               │  │
│  │  1. CLONE       Shallow clone repo into isolated temp directory (GitPython)   │  │
│  │  2. FINGERPRINT Extract file tree, manifests, entrypoint (exclude junk)       │  │
│  │  3. GENERATE    Select base template + Groq LLM one-shot prompt               │  │
│  │  4. SELF-HEAL   Test build → Feed error back to Groq → Patch Dockerfile       │  │
│  │  5. FINALIZE    Store DockerfileResult & emit terminal event                  │  │
│  └───────────────────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────┬────────────────────────────────────────────┘
                                         │
                                         ▼
                            ┌─────────────────────────┐
                            │    Groq Cloud API       │
                            │ (llama-3.3-70b-versatile│
                            └─────────────────────────┘
```

---

## 🛠 Tech Stack

- **Framework**: [FastAPI](https://fastapi.tiangolo.com/) (Async Python 3.10+)
- **ASGI Server**: [Uvicorn](https://www.uvicorn.org/) (Standard with WebSockets & uvloop support)
- **Data Validation**: [Pydantic v2](https://docs.pydantic.dev/latest/)
- **LLM Provider**: [Groq SDK](https://github.com/groq/groq-python) (`llama-3.3-70b-versatile` JSON mode)
- **Git Operations**: [GitPython](https://gitpython.readthedocs.io/) (Shallow clones & temp directory lifecycle)
- **Configuration**: [python-dotenv](https://github.com/theskumar/python-dotenv) & [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)

---

## 📁 Project Directory Structure

```
backend/
├── .env.example              # Template for environment configuration
├── .gitignore                # Git ignore rules (.venv, .env, __pycache__)
├── requirements.txt          # Python dependencies
├── README.md                 # Complete documentation (this file)
└── app/
    ├── __init__.py
    ├── main.py               # FastAPI application setup, CORS, route registration
    ├── config.py             # Environment configuration settings
    ├── contracts.py          # Shared Pydantic models (Fingerprint, Result, Events)
    ├── routes/
    │   ├── __init__.py
    │   ├── jobs.py           # HTTP endpoints: POST /jobs, GET /jobs/{job_id}
    │   └── ws.py             # WebSocket endpoint: /ws/jobs
    └── services/
        ├── __init__.py
        ├── agent.py          # LLM orchestration & self-healing retry loop
        ├── cloner.py         # Shallow Git cloner with async context manager
        ├── events.py         # In-memory fan-out event hub for WebSockets
        ├── fingerprint.py    # Repo analyzer (file tree, manifest inspection)
        ├── github.py         # GitHub URL validation and parser
        ├── groq_client.py    # Async wrapper around Groq API with JSON validation
        ├── jobs.py           # In-memory thread-safe job store and worker scheduler
        ├── prompts.py        # System and user prompt builders for Groq
        └── templates.py      # Base skeleton templates (Node.js, Python, Static)
```

---

## 🚀 Getting Started

### Prerequisites
- **Python 3.10+** installed
- **Git** installed on your system path
- A **Groq API Key** (Free tier available at [console.groq.com](https://console.groq.com/keys))

### Installation & Setup

1. **Clone or navigate to the backend directory:**
   ```bash
   cd backend
   ```

2. **Create and activate a virtual environment:**
   - **On Windows (PowerShell):**
     ```powershell
     python -m venv .venv
     .venv\Scripts\Activate.ps1
     ```
   - **On Linux / macOS:**
     ```bash
     python3 -m venv .venv
     source .venv/bin/activate
     ```

3. **Install the dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

### Environment Variables

Create a `.env` file in the `backend/` root directory:

```bash
# Windows
copy .env.example .env

# Linux / macOS
cp .env.example .env
```

Configure the following variables in `.env`:

| Variable | Type | Required | Default | Description |
| :--- | :--- | :--- | :--- | :--- |
| `GROQ_API_KEY` | `string` | **Yes** | `""` | Your Groq API key from Groq Console. |
| `GROQ_MODEL` | `string` | No | `llama-3.3-70b-versatile` | Groq model used for generation and healing. |
| `GROQ_TEMPERATURE`| `float` | No | `0.1` | Temperature (low value ensures strict template adherence). |
| `MAX_HEAL_RETRIES`| `integer`| No | `3` | Maximum self-healing attempts on build failures. |

### Running the Server

Start the FastAPI application with Uvicorn:

```bash
uvicorn app.main:app --reload --port 8000
```

The server will start at `http://127.0.0.1:8000` with hot-reloading enabled.

---

## 📡 Complete API Reference

### 1. Health Check (`GET /health`)

Performs a lightweight liveness probe to verify that the backend service is running.

- **URL**: `/health`
- **Method**: `GET`
- **Authentication**: None
- **Headers**: None

#### Response
- **Status Code**: `200 OK`
- **Content-Type**: `application/json`

```json
{
  "status": "ok"
}
```

#### Example Usage

**cURL:**
```bash
curl -X GET http://localhost:8000/health
```

**JavaScript (Fetch):**
```javascript
const res = await fetch("http://localhost:8000/health");
const data = await res.json();
console.log(data); // { status: "ok" }
```

---

### 2. Create Job (`POST /jobs`)

Validates the submitted GitHub URL and schedules an asynchronous background job to clone, analyze, and generate a Dockerfile. **This endpoint does not block**; it immediately returns `202 Accepted` with a unique `job_id`.

- **URL**: `/jobs`
- **Method**: `POST`
- **Authentication**: None
- **Headers**: `Content-Type: application/json`

#### Request Body
| Field | Type | Required | Description | Example |
| :--- | :--- | :--- | :--- | :--- |
| `repo_url` | `string` | **Yes** | Valid public GitHub repository URL | `"https://github.com/expressjs/express"` |

*Accepted URL formats:*
- `https://github.com/owner/repo`
- `http://github.com/owner/repo.git`
- `git@github.com:owner/repo.git`
- `git+https://github.com/owner/repo`

#### Responses

##### `202 Accepted` (Job Successfully Queued)
```json
{
  "job_id": "a1b2c3d4e5f6",
  "status": "queued"
}
```

##### `400 Bad Request` (Invalid GitHub URL)
```json
{
  "detail": "'https://notgithub.com/owner/repo' is not a valid github.com/owner/repo URL"
}
```

##### `422 Unprocessable Entity` (Missing or malformed payload)
```json
{
  "detail": [
    {
      "type": "missing",
      "loc": ["body", "repo_url"],
      "msg": "Field required"
    }
  ]
}
```

#### Example Usage

**cURL:**
```bash
curl -X POST http://localhost:8000/jobs \
  -H "Content-Type: application/json" \
  -d '{"repo_url": "https://github.com/fastapi/fastapi"}'
```

**Python (`httpx`):**
```python
import httpx

response = httpx.post(
    "http://localhost:8000/jobs",
    json={"repo_url": "https://github.com/fastapi/fastapi"}
)
job_info = response.json()
print("Created Job ID:", job_info["job_id"])
```

**JavaScript (Fetch):**
```javascript
const response = await fetch("http://localhost:8000/jobs", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ repo_url: "https://github.com/fastapi/fastapi" })
});
const data = await response.json();
console.log("Job ID:", data.job_id); // e.g. "a1b2c3d4e5f6"
```

---

### 3. Get Job Status (`GET /jobs/{job_id}`)

Fetches a snapshot of the current state of a job, including all sequential logs, lifecycle stage, error messages (if failed), and the final generated Dockerfile result (if done).

- **URL**: `/jobs/{job_id}`
- **Method**: `GET`
- **Authentication**: None
- **Path Parameters**:
  - `job_id` (`string`, required): The 12-character hexadecimal ID returned from `POST /jobs`.

#### Responses

##### `200 OK` (Job In Progress)
```json
{
  "job_id": "a1b2c3d4e5f6",
  "status": "generating",
  "repo_url": "https://github.com/fastapi/fastapi",
  "logs": [
    {
      "job_id": "a1b2c3d4e5f6",
      "stage": "queued",
      "message": "Job queued",
      "timestamp": "2026-08-31T07:30:00.123456Z"
    },
    {
      "job_id": "a1b2c3d4e5f6",
      "stage": "cloning",
      "message": "Cloning repository",
      "timestamp": "2026-08-31T07:30:01.234567Z"
    },
    {
      "job_id": "a1b2c3d4e5f6",
      "stage": "analyzing",
      "message": "Analyzing repository structure",
      "timestamp": "2026-08-31T07:30:03.345678Z"
    },
    {
      "job_id": "a1b2c3d4e5f6",
      "stage": "generating",
      "message": "Generating Dockerfile via Groq",
      "timestamp": "2026-08-31T07:30:04.456789Z"
    }
  ],
  "result": null,
  "error": null
}
```

##### `200 OK` (Job Completed Successfully)
```json
{
  "job_id": "a1b2c3d4e5f6",
  "status": "done",
  "repo_url": "https://github.com/fastapi/fastapi",
  "logs": [
    {
      "job_id": "a1b2c3d4e5f6",
      "stage": "queued",
      "message": "Job queued",
      "timestamp": "2026-08-31T07:30:00.123456Z"
    },
    {
      "job_id": "a1b2c3d4e5f6",
      "stage": "cloning",
      "message": "Cloning repository",
      "timestamp": "2026-08-31T07:30:01.234567Z"
    },
    {
      "job_id": "a1b2c3d4e5f6",
      "stage": "analyzing",
      "message": "Analyzing repository structure",
      "timestamp": "2026-08-31T07:30:03.345678Z"
    },
    {
      "job_id": "a1b2c3d4e5f6",
      "stage": "generating",
      "message": "Generating Dockerfile via Groq",
      "timestamp": "2026-08-31T07:30:04.456789Z"
    },
    {
      "job_id": "a1b2c3d4e5f6",
      "stage": "done",
      "message": "Dockerfile generated successfully",
      "timestamp": "2026-08-31T07:30:06.567890Z"
    }
  ],
  "result": {
    "language": "python",
    "framework": "python",
    "entry_point": "main.py",
    "port": 8000,
    "start_command": "uvicorn main:app --host 0.0.0.0 --port 8000",
    "dockerfile_content": "FROM python:3.11-slim\n\nWORKDIR /app\n\nCOPY requirements.txt .\nRUN pip install --no-cache-dir -r requirements.txt\n\nCOPY . .\n\nEXPOSE 8000\n\nCMD [\"uvicorn\", \"main:app\", \"--host\", \"0.0.0.0\", \"--port\", \"8000\"]\n",
    "metadata": {
      "raw_response": {
        "language": "python",
        "framework": "python",
        "entry_point": "main.py",
        "port": 8000,
        "start_command": "uvicorn main:app --host 0.0.0.0 --port 8000",
        "dockerfile_content": "FROM python:3.11-slim..."
      }
    }
  },
  "error": null
}
```

##### `200 OK` (Job Failed)
```json
{
  "job_id": "a1b2c3d4e5f6",
  "status": "failed",
  "repo_url": "https://github.com/owner/unsupported-repo",
  "logs": [
    {
      "job_id": "a1b2c3d4e5f6",
      "stage": "queued",
      "message": "Job queued",
      "timestamp": "2026-08-31T07:30:00.123456Z"
    },
    {
      "job_id": "a1b2c3d4e5f6",
      "stage": "cloning",
      "message": "Cloning repository",
      "timestamp": "2026-08-31T07:30:01.234567Z"
    },
    {
      "job_id": "a1b2c3d4e5f6",
      "stage": "failed",
      "message": "Clone/validation failed: Repository not found or private",
      "timestamp": "2026-08-31T07:30:03.345678Z"
    }
  ],
  "result": null,
  "error": "Clone/validation failed: Repository not found or private"
}
```

##### `404 Not Found` (Job Does Not Exist)
```json
{
  "detail": "Job not found"
}
```

#### Example Usage

**cURL:**
```bash
curl -X GET http://localhost:8000/jobs/a1b2c3d4e5f6
```

**JavaScript (Polling Example):**
```javascript
async function pollJob(jobId) {
  const interval = setInterval(async () => {
    const res = await fetch(`http://localhost:8000/jobs/${jobId}`);
    if (!res.ok) {
      console.error("Failed to fetch job status");
      clearInterval(interval);
      return;
    }
    const job = await res.json();
    console.log(`Current Stage: ${job.status}`);

    if (job.status === "done") {
      console.log("Generated Dockerfile:\n", job.result.dockerfile_content);
      clearInterval(interval);
    } else if (job.status === "failed") {
      console.error("Job failed:", job.error);
      clearInterval(interval);
    }
  }, 1000);
}

pollJob("a1b2c3d4e5f6");
```

---

### 4. Real-Time WebSocket Stream (`WS /ws/jobs`)

Provides a high-throughput, low-latency live event stream of `JobEvent` notifications pushed in real-time as jobs transition through the pipeline.

- **URL**: `ws://localhost:8000/ws/jobs`
- **Protocol**: WebSocket

#### Event Stream Contract
Every message sent by the server is a JSON-encoded `JobEvent`:

```json
{
  "job_id": "a1b2c3d4e5f6",
  "stage": "generating",
  "message": "Generating Dockerfile via Groq",
  "timestamp": "2026-08-31T07:30:04.456789Z"
}
```

#### Client Filtering (Optional)
By default, a connected WebSocket receives events for **all** running jobs. To filter the stream to only receive events for a specific `job_id`, the client can send a plain text message containing the desired `job_id` over the WebSocket connection.

- **Send text**: `"a1b2c3d4e5f6"` → Streams only events matching `job_id == "a1b2c3d4e5f6"`.
- **Send empty string**: `""` → Clears filter and streams all events.

#### Example Usage

**JavaScript (Browser / React):**
```javascript
const socket = new WebSocket("ws://localhost:8000/ws/jobs");

socket.onopen = () => {
  console.log("WebSocket connected to backend event stream");
  // Optionally filter events to a specific job:
  // socket.send("a1b2c3d4e5f6");
};

socket.onmessage = (event) => {
  const jobEvent = JSON.parse(event.data);
  console.log(`[${jobEvent.stage.toUpperCase()}] ${jobEvent.message}`, jobEvent);

  if (jobEvent.stage === "done") {
    console.log("Pipeline finished! Fetching final result...");
  }
};

socket.onerror = (error) => {
  console.error("WebSocket error:", error);
};

socket.onclose = () => {
  console.log("WebSocket connection closed");
};
```

**Python (`websockets` library):**
```python
import asyncio
import json
import websockets

async def listen_events(target_job_id: str | None = None):
    uri = "ws://localhost:8000/ws/jobs"
    async with websockets.connect(uri) as ws:
        if target_job_id:
            await ws.send(target_job_id)
        
        while True:
            msg = await ws.recv()
            event = json.loads(msg)
            print(f"[{event['stage']}] {event['message']}")
            if event['stage'] in ("done", "failed"):
                break

asyncio.run(listen_events("a1b2c3d4e5f6"))
```

---

## 🔄 Job Lifecycle & State Machine

Every job undergoes a strictly defined state progression:

```
┌──────────┐
│  QUEUED  │ (Job accepted, worker task spawned)
└────┬─────┘
     ▼
┌──────────┐
│ CLONING  │ (Git shallow clone to per-job isolated directory)
└────┬─────┘
     ▼
┌───────────┐
│ ANALYZING │ (Build RepoFingerprint: scan files, manifests, entrypoint)
└────┬──────┘
     ▼
┌────────────┐
│ GENERATING │ (Groq LLM one-shot prompt produces initial Dockerfile)
└────┬───────┘
     │
     ├──────────────────────────────────────────┐
     ▼ (if build validation enabled)            ▼ (if build succeeds or skipped)
┌──────────┐                               ┌──────────┐
│ BUILDING │                               │   DONE   │ (Dockerfile ready)
└────┬─────┘                               └──────────┘
     │ (build failure detected)
     ▼
┌──────────┐
│ HEALING  │ ──(Retry up to MAX_HEAL_RETRIES)──► [BUILDING]
└────┬─────┘
     │ (retries exhausted or unexpected error)
     ▼
┌──────────┐
│  FAILED  │ (Terminal failure: error details attached)
└──────────┘
```

| Stage | Description |
| :--- | :--- |
| `queued` | Request received and scheduled onto the background event loop. |
| `cloning` | Executing `git clone --depth 1` into a temporary filesystem path. |
| `analyzing` | Filtering junk files (`node_modules`, `.venv`), parsing `package.json`/`requirements.txt`, and detecting the entry point. |
| `generating` | Selecting template skeleton and querying Groq (`llama-3.3-70b-versatile`) for Dockerfile parameters. |
| `building` | Container build validation step. |
| `healing` | Build failed; error logs sent back to Groq with patch instructions. |
| `done` | Generation completed successfully. Full `DockerfileResult` available. |
| `failed` | Pipeline encountered an unrecoverable error (e.g. invalid repo, private repo, rate limit, syntax failure). |

---

## 📦 Data Models & Contracts

All contracts are defined in [`app/contracts.py`](file:///f:/web%20projects/bano%20qabil%20hackathon/backend/app/contracts.py) and are treated as stable JSON interfaces across the entire pipeline.

### 1. `RepoFingerprint`
The curated summary extracted from the repository. Only this metadata is sent to the LLM (never the entire repository code).

```json
{
  "repo_url": "https://github.com/owner/my-app",
  "file_tree": [
    "package.json",
    "package-lock.json",
    "src/index.js",
    "src/routes/api.js",
    "public/index.html"
  ],
  "manifests": {
    "package.json": "{\n  \"name\": \"my-app\",\n  \"scripts\": {\n    \"start\": \"node src/index.js\"\n  }\n}"
  },
  "entry_point": {
    "path": "src/index.js",
    "content": "const express = require('express');\nconst app = express();\n..."
  },
  "existing_dockerfile": null,
  "existing_compose": null
}
```

### 2. `DockerfileResult`
The structured output returned upon successful Dockerfile generation.

```json
{
  "language": "javascript",
  "framework": "node",
  "entry_point": "src/index.js",
  "port": 3000,
  "start_command": "node src/index.js",
  "dockerfile_content": "FROM node:20-alpine\nWORKDIR /app\nCOPY package*.json ./\nRUN npm ci --only=production\nCOPY . .\nEXPOSE 3000\nCMD [\"node\", \"src/index.js\"]\n",
  "metadata": {
    "build_attempts": 1
  }
}
```

### 3. `JobEvent`
Individual progress event emitted over WebSockets.

```json
{
  "job_id": "a1b2c3d4e5f6",
  "stage": "generating",
  "message": "Generating Dockerfile via Groq",
  "timestamp": "2026-08-31T07:30:04.456789Z"
}
```

---

## 🔄 Self-Healing Engine & DevOps Integration

The backend is architected with a decoupled build validation interface in [`app/services/agent.py`](file:///f:/web%20projects/bano%20qabil%20hackathon/backend/app/services/agent.py):

```python
async def generate_dockerfile(
    repo_path: str,
    build_fn: Optional[Callable[[str], Optional[str]]] = None,
    repo_url: Optional[str] = None,
    job_id: Optional[str] = None,
) -> DockerfileResult | DockerfileError
```

### How the Self-Healing Loop Works:
1. **Initial Generation**: Groq fills a framework skeleton template based on the repo fingerprint.
2. **Build Execution**: If a `build_fn` is provided, it receives the generated `dockerfile_content` and attempts a container build (e.g. `docker build .`).
3. **Outcome Evaluation**:
   - Returns `None` ➔ Build succeeded. Result is returned immediately.
   - Returns `error_string` ➔ Build failed with standard error output.
4. **Autonomous Patching**: The error message and previous Dockerfile are fed into a specialized patch prompt (`build_patch_prompt`). Groq diagnoses the error and returns a corrected Dockerfile.
5. **Bounded Loop**: The process repeats up to `MAX_HEAL_RETRIES` (default `3`). If the container still fails, a structured `DockerfileError` is surfaced.

---

## 💻 Supported Frameworks & Stacks

The analyzer automatically detects and supports three primary application stacks:

| Framework | Detection Criteria | Base Docker Image | Key Features |
| :--- | :--- | :--- | :--- |
| **Node.js** (`node`) | Presence of `package.json` | `node:20-alpine` / `node:18-alpine` | `npm ci` / `yarn install`, build scripts, port extraction |
| **Python** (`python`) | Presence of `requirements.txt`, `pyproject.toml`, `setup.py`, `Pipfile` | `python:3.11-slim` | Virtual environment caching, WSGI/ASGI command generation (`uvicorn`, `gunicorn`, `flask`) |
| **Static Web** (`static`) | `index.html` without server manifests | `nginx:alpine` | Multi-stage build or static file copy into `/usr/share/nginx/html` |

### Intelligent Filtering
To optimize LLM context and prevent leaking unnecessary data, repository scanning automatically excludes:
- `node_modules/`, `.git/`, `dist/`, `build/`
- `__pycache__/`, `.venv/`, `venv/`, `.env`
- Binary files, archives, and lock files beyond standard manifests

---

## 📖 Interactive API Documentation

Once the server is running, explore and test the endpoints directly in your browser:

- **Swagger UI (Interactive)**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc (Specification)**: [http://localhost:8000/redoc](http://localhost:8000/redoc)
- **OpenAPI Schema (JSON)**: [http://localhost:8000/openapi.json](http://localhost:8000/openapi.json)
