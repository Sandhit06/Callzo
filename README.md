# Callzo Engineering Assessment

This repository contains two independent assessment projects:

- `task1-backend/`: FastAPI call usage metering API with PostgreSQL support.
- `task2-frontend/`: React account usage console with an Apple-inspired liquid glass interface.
- `Task/`: the assessment PDF and task specifications.

## Choose how to run

| Project | Run from source | Full assessment stack |
|---|---|---|
| Task 1 backend | Python 3.12 + SQLite; no Docker required | Docker Compose + PostgreSQL + webhook simulator |
| Task 2 frontend | Vite source app; the supplied mock API still needs Docker | Vite source app + mock API container |

For either Docker option, start Docker Desktop and wait until its **Linux engine** is running. If Docker reports that it cannot connect to `dockerDesktopLinuxEngine`, the daemon is not running yet. The supplied `meetrj` simulator and mock API images are published for `linux/arm64`; their Compose services request that platform explicitly so Docker Desktop can run them using its ARM emulation support on an x64 Windows machine.

## Run Task 1 locally

### Host run: Python 3.12 + SQLite

This is the quickest way to run the API without Docker or Python 3.11. In PowerShell, start at the repository root and enter `task1-backend` once:

```powershell
cd task1-backend
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:DATABASE_URL = 'sqlite:///./callzo_metering.db'
$env:WEBHOOK_SECRET = 'callzo_test_secret_change_me'
$env:ADMIN_API_KEY = 'callzo_admin_key_2026'
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open `http://localhost:8000/health` for the health response or `http://localhost:8000/docs` for the interactive API documentation. The API does not serve a homepage at `/`, so a `404 Not Found` at the site root is expected. This host mode uses SQLite and does not start the Docker webhook simulator. It is for local API development; use the Docker option below to exercise the PostgreSQL-backed assessment stack and simulator.

### Full stack: Docker Compose, PostgreSQL, and simulator

Start Docker Desktop first. In PowerShell, from the repository root:

```powershell
cd task1-backend
docker compose up --build
```

The API is at `http://localhost:8000`; the simulator sends all assessment scenarios automatically. Press `Ctrl+C` to stop the stack. If you are in `task1-backend` and want to switch projects, run `cd ..` first, then `cd task2-frontend`.

The default host ports are `5432` (PostgreSQL) and `8000` (API). If another local project already uses either port, set alternate host ports before starting Compose; the values below use `5433` and `8002`:

```powershell
$env:POSTGRES_HOST_PORT = '5433'
$env:API_HOST_PORT = '8002'
docker compose up --build
```

Then use `http://localhost:8002/health` and `http://localhost:8002/docs`. These variables affect only the Windows host ports; services inside the Compose network continue to use PostgreSQL port `5432` and API port `8000`.

## Run Task 2 locally

The React app can run from source with Node.js 20 or newer. The provided mock API is a Docker image, so Docker Desktop must be running for live account data.

The `mock-api` service exists only in `task2-frontend/docker-compose.yml`. In PowerShell, from the repository root, start it with:

```powershell
cd task2-frontend
docker compose up -d mock-api
```

If your prompt is currently in `task1-backend`, move to the repository root before entering the frontend folder:

```powershell
cd ..
cd task2-frontend
docker compose config --services
docker compose up -d mock-api
```

The service list should include `mock-api` and `frontend`. If it does not, check that the prompt path ends in `task2-frontend` before running Compose.

In a second PowerShell window, also starting at the repository root, run the frontend source:

```powershell
cd task2-frontend
npm ci
npm run dev
```

Open `http://localhost:3000`. Vite proxies API requests to the mock API at `http://localhost:8001`. If the mock API is not running, the console still opens but account and summary views show their API error states. Stop Vite with `Ctrl+C`; stop the mock API with `docker compose stop mock-api` from `task2-frontend`.

The Task 2 Compose file references the published frontend image rather than building source. After changing the frontend, build and push `sandhit06/callzo-task2-frontend:latest` before using that Compose option for the updated version. See [the Task 2 README](task2-frontend/README.md) for details.

## Project notes

See [Task 1 README](task1-backend/README.md) for its schema, webhook handling, cap policy, reconciliation, and API examples. See [Task 2 README](task2-frontend/README.md) for URL/server/client state choices, the usage prism, and UI behavior.
