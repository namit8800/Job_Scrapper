# SyncUp Job Scraper — Project 02

Scrapes Internshala and stores jobs locally via Express API.
Nothing is pushed to SyncUp until you explicitly call `/api/forward`.

## Project structure

```
syncup_job_scraper/
├── internshala_scraper.py   ← Python scraper (Playwright)
├── api/
│   └── server.js            ← Node/Express API (stores jobs to disk)
├── package.json
└── README.md
```

## First-time setup

### 1. Install Node dependencies
```bash
npm install
```

### 2. Install Python dependencies
```bash
pip install playwright requests
playwright install chromium
```

## Running

Always start the server first, then run the scraper in a second terminal.

### Terminal 1 — start the server
```bash
npm run server
```
You'll see:
```
📦 Loaded 0 existing jobs from disk
🚀 Server running on http://localhost:3000
```

### Terminal 2 — run the scraper
```bash
python3 internshala_scraper.py
```
You'll be prompted for:
- Role (e.g. software, marketing, design)
- Location (e.g. bangalore, delhi, remote)
- Salary filter (optional)

## API endpoints

| Method | Endpoint | What it does |
|--------|----------|--------------|
| POST | `/api/jobs` | Scraper sends jobs here (auto) |
| GET | `/api/jobs` | View all stored jobs |
| GET | `/api/jobs?forwarded=false` | View jobs not yet sent to SyncUp |
| GET | `/api/health` | See counts: stored / pending / forwarded |
| POST | `/api/forward` | Send pending jobs to SyncUp (when ready) |

## When you're ready to connect SyncUp

Set these environment variables before starting the server:
```bash
SYNCUP_API_URL=https://your-syncup.com/api/jobs \
SYNCUP_API_KEY=your-key \
node api/server.js
```
Then call:
```bash
curl -X POST http://localhost:3000/api/forward
```

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `PORT` | 3000 | Server port |
| `HEADLESS` | false | Set to `true` for GitHub Actions (no browser window) |
| `SLACK_WEBHOOK_URL` | — | Slack alert on scraper failure |
| `SYNCUP_API_URL` | — | SyncUp endpoint (set when ready to forward) |
| `SYNCUP_API_KEY` | — | SyncUp API key |
| `SYNCUP_SERVER_URL` | http://localhost:3000/api/jobs | Override server URL |
