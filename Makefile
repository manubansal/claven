.PHONY: setup test ci dev-read-write dev-read-only dev-local-frontend

# Full local dev stack — local backend + frontend, writable DB connection.
# Required for testing sign-in/OAuth flows (oauth_callback writes to the DB).
dev-read-write:
	@[ -f .env.local ] || (echo "Error: .env.local not found. Copy .env.local.example and fill in values."; exit 1)
	@set -a; . ./.env.local; set +a; \
	lsof -ti :8000 | xargs kill -9 2>/dev/null; true; \
	alembic upgrade head && \
	trap 'lsof -ti :8000 | xargs kill 2>/dev/null; sleep 1; lsof -ti :8000 | xargs kill -9 2>/dev/null; kill 0' EXIT; \
	node -e "const fs = require('fs'); const md = fs.readFileSync('docs/how-claven-works.md', 'utf-8'); fs.writeFileSync('web/app/how-it-works/content.ts', 'export default ' + JSON.stringify(md) + ';\\n');" 2>/dev/null; true; \
	CLAVEN_LOG_FILE=/tmp/claven-server.log uvicorn claven.server:app --port 8000 --reload --timeout-graceful-shutdown 3 --ssl-keyfile certs/localhost+1-key.pem --ssl-certfile certs/localhost+1.pem & \
	NEXT_PUBLIC_API_URL=https://localhost:8000 npm --prefix web run dev & \
	wait

# Read-only view of the production database — safe to run alongside prod.
# Writes will fail at the DB level (SELECT-only role). Use this to observe
# dashboard state changes driven by live prod activity.
dev-read-only:
	@[ -f .env.local ] || (echo "Error: .env.local not found. Copy .env.local.example and fill in values."; exit 1)
	@set -a; . ./.env.local; set +a; \
	[ -n "$$DATABASE_URL_READONLY" ] || (echo "Error: DATABASE_URL_READONLY not set in .env.local."; exit 1); \
	lsof -ti :8000 | xargs kill -9 2>/dev/null; true; \
	trap 'lsof -ti :8000 | xargs kill 2>/dev/null; sleep 1; lsof -ti :8000 | xargs kill -9 2>/dev/null; kill 0' EXIT; \
	DATABASE_URL=$$DATABASE_URL_READONLY uvicorn claven.server:app --port 8000 --reload --timeout-graceful-shutdown 3 --ssl-keyfile certs/localhost+1-key.pem --ssl-certfile certs/localhost+1.pem & \
	NEXT_PUBLIC_API_URL=https://localhost:8000 npm --prefix web run dev & \
	wait

# Frontend-only dev against the prod API — no local backend.
# Sign-in and all API calls go through https://api.claven.app.
# Safe: no local DB connection, no backend code running.
# No longer signs in: the session cookie is SameSite=Lax, which localhost
# (another site) never receives, and prod doesn't allow the localhost origin.
# Use dev-read-write or dev-read-only, which run a local backend.
dev-local-frontend:
	NEXT_PUBLIC_API_URL=https://api.claven.app npm --prefix web run dev

setup:
	git config core.hooksPath .hooks
	chmod +x .hooks/*
	pip install -r requirements.txt -r requirements-dev.txt

test:
	pytest --tb=short -q -m "not live"

# Run the full CI workflow locally via act (requires Docker + act).
# Reads secrets from .secrets -- copy .secrets.example and fill in values first.
# Post-job cleanup steps exit 127 in act (known bug: cache-save runs in the
# postgres service container which has no node). We check pytest output directly
# rather than relying on act's exit code to distinguish real failures.
ci:
	@act push -j test 2>&1 | tee /tmp/act-output.txt; \
	if grep -qE "[0-9]+ failed" /tmp/act-output.txt; then \
		echo ""; echo "FAIL: tests failed"; exit 1; \
	elif grep -qE "[0-9]+ passed" /tmp/act-output.txt; then \
		echo ""; echo "OK: tests passed"; exit 0; \
	else \
		echo ""; echo "FAIL: no test results found"; exit 1; \
	fi
