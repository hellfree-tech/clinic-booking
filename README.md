# Clinic Booking API (Koya Academy Assessment – Task 1)

Complete implementation of all 5 levels + simple web UI.

**API Key (you must use this exact key):**  
`koya-clinic-2026-x7K9pQ2mR8vL4nJ6`

## Quick local test

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Open http://localhost:8000  
API docs: http://localhost:8000/docs

## Deploy on Render (simplest path)

1. Push this folder to a GitHub repo (or use Render’s “Public Git repository”).
2. In Render Dashboard → **New +** → **Web Service**.
3. Connect the repo.
4. Settings:
   - **Name**: anything (e.g. `clinic-booking`)
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Instance Type**: Free
5. Environment Variables (optional but recommended):
   - `API_KEY` = `koya-clinic-2026-x7K9pQ2mR8vL4nJ6`
6. Click **Create Web Service**.

After it deploys you get an HTTPS URL like:
`https://clinic-booking-xxxx.onrender.com`

That URL is what you submit.

### Important notes for the free tier
- The free instance spins down after ~15 min of inactivity. The first request after sleep can take 30–60 s.
- SQLite file lives on the ephemeral disk → data is lost when the service restarts or is redeployed.  
  For the assessment this is usually fine (the graders hit a running instance).  
  If you want persistence, switch to a free Neon Postgres later (change `DATABASE_URL`).

## What to submit

- **Deployed HTTPS URL** (the Render URL)
- **API Key**: `koya-clinic-2026-x7K9pQ2mR8vL4nJ6`

Keep the service running and do not change it until Koya contacts you.

## Endpoints (all require header `X-API-Key: koya-clinic-2026-x7K9pQ2mR8vL4nJ6`)

| Method | Path | Level | Description |
|--------|------|-------|-------------|
| POST   | /reset | 1 | Replace all slots (deletes bookings) |
| GET    | /slots | 1 | List slots (+ available flag) |
| POST   | /bookings | 1–5 | Book a slot |
| GET    | /bookings/{id} | 1 | Get one booking |
| DELETE | /bookings/{id} | 3 | Cancel booking |
| GET    | /bookings?email=… | 4 | List person’s bookings (cursor pagination) |

Web UI is served at `/`.
