// server.js
// Place this file directly in your Job_scrapper root folder (not in a subfolder).
// Run with: node server.js

const express  = require("express");
const fs       = require("fs");
const path     = require("path");
const { exec } = require("child_process");

const app        = express();
app.use(express.json());
app.use(require("cors")());

const PORT       = process.env.PORT           || 3000;
const SYNCUP_URL = process.env.SYNCUP_API_URL || "";
const SYNCUP_KEY = process.env.SYNCUP_API_KEY || "";
const DB_FILE    = path.join(__dirname, "jobs_store.json");
const ROOT       = __dirname;   // server.js sits directly in the project root

// Detect correct python command per OS
const PYTHON_CMD = process.platform === "win32" ? "python" : "python3";

app.use(express.static(ROOT));

// ── Persistent store (for SyncUp forwarding later) ───────────────────────────
function loadStore() {
  if (!fs.existsSync(DB_FILE)) return { seen: [], jobs: [] };
  try { return JSON.parse(fs.readFileSync(DB_FILE, "utf8")); }
  catch { return { seen: [], jobs: [] }; }
}
function saveStore(store) {
  fs.writeFileSync(DB_FILE, JSON.stringify(store, null, 2));
}
function dedupKey(job) {
  return `${job.title}__${job.company}__${job.posting_date}`.toLowerCase();
}

const store = loadStore();
console.log(`Loaded ${store.jobs.length} existing jobs from disk`);

// ── In-memory current search results (cleared on every new search) ────────────
let currentResults = [];


// ── POST /api/scrape — trigger scraper, stream results back ──────────────────
app.post("/api/scrape", (req, res) => {
  const { role, location, source, minSalary, maxSalary } = req.body;
  if (!role || !location || !source) {
    return res.status(400).json({ error: "role, location and source are required" });
  }

  currentResults = [];

  const script = source === "naukri" ? "naukri_scraper.py"
               : source === "indeed" ? "indeed_scraper.py"
               : "internshala_scraper.py";

  const scriptPath = path.join(ROOT, script);
  if (!fs.existsSync(scriptPath)) {
    return res.status(500).json({ error: `Scraper file not found: ${script}` });
  }

  let salaryInput = "n\n";
  if (source === "internshala" && (minSalary || maxSalary)) {
    salaryInput = `y\n${minSalary || ""}\n${maxSalary || ""}\n`;
  }
  const input = `${role}\n${location}\n${salaryInput}`;
  console.log(`\nTriggering ${script} for "${role}" in "${location}" (using ${PYTHON_CMD})`);

  const proc = exec(`${PYTHON_CMD} "${scriptPath}"`, {
    cwd: ROOT,
    env: { ...process.env, HEADLESS: "true", PYTHONIOENCODING: "utf-8" }
  });

  proc.stdin.write(input);
  proc.stdin.end();

  proc.stdout.on("data", d => process.stdout.write(d.toString()));
  proc.stderr.on("data", d => process.stderr.write(d.toString()));

  proc.on("error", (err) => {
    console.error("Failed to start scraper process:", err.message);
    res.status(500).json({ error: "Failed to start scraper", detail: err.message });
  });

  proc.on("close", (code) => {
    if (code !== 0) {
      console.error(`Scraper exited with code ${code}`);
      return res.status(500).json({ error: "Scraper failed", code });
    }

    const outputFile = path.join(ROOT,
      source === "naukri" ? "naukri_jobs.json" :
      source === "indeed" ? "indeed_jobs.json" :
      "internshala_jobs.json"
    );

    try {
      if (!fs.existsSync(outputFile)) {
        currentResults = [];
        console.log("No output file — scraper returned 0 results");
        return res.json({ status: "done", count: 0 });
      }
      const stats = fs.statSync(outputFile);
      const ageMs = Date.now() - stats.mtimeMs;
      if (ageMs > 60000) {
        currentResults = [];
        console.log("Output file is stale — returning empty results");
        return res.json({ status: "done", count: 0 });
      }
      const jobs = JSON.parse(fs.readFileSync(outputFile, "utf8"));
      currentResults = jobs;
      console.log(`Scraper finished — ${jobs.length} jobs`);
      res.json({ status: "done", count: jobs.length });
    } catch (e) {
      currentResults = [];
      console.error("Could not read scraper output:", e);
      res.json({ status: "done", count: 0 });
    }
  });
});


// ── GET /api/results — return current search results ─────────────────────────
app.get("/api/results", (req, res) => {
  res.json({ total: currentResults.length, jobs: currentResults });
});


// ── POST /api/jobs — receive job from scraper, store for SyncUp later ────────
app.post("/api/jobs", (req, res) => {
  const job = req.body;
  const required = ["title", "company", "posting_date", "apply_link", "source_platform"];
  for (const field of required) {
    if (!job[field]) return res.status(400).json({ error: `Missing field: ${field}` });
  }

  const key = dedupKey(job);
  if (store.seen.includes(key)) {
    return res.status(200).json({ status: "duplicate" });
  }

  job._forwarded   = false;
  job._received_at = new Date().toISOString();
  store.seen.push(key);
  store.jobs.push(job);
  saveStore(store);

  return res.status(200).json({ status: "stored" });
});


// ── GET /api/jobs — all stored jobs (for SyncUp forwarding) ──────────────────
app.get("/api/jobs", (req, res) => {
  let jobs = store.jobs;
  if (req.query.forwarded === "false") jobs = jobs.filter(j => !j._forwarded);
  if (req.query.forwarded === "true")  jobs = jobs.filter(j =>  j._forwarded);
  res.json({ total: jobs.length, jobs });
});


// ── POST /api/forward — push pending jobs to SyncUp ──────────────────────────
app.post("/api/forward", async (req, res) => {
  if (!SYNCUP_URL) return res.status(400).json({ error: "SYNCUP_API_URL not set" });
  const pending = store.jobs.filter(j => !j._forwarded);
  if (!pending.length) return res.status(200).json({ status: "nothing to forward" });

  let pushed = 0, failed = 0;
  for (const job of pending) {
    try {
      const r = await fetch(SYNCUP_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": `Bearer ${SYNCUP_KEY}` },
        body: JSON.stringify(job),
      });
      if (r.ok) { job._forwarded = true; pushed++; }
      else failed++;
    } catch { failed++; }
  }
  saveStore(store);
  return res.status(200).json({ status: "done", pushed, failed });
});


// ── GET /api/health ───────────────────────────────────────────────────────────
app.get("/api/health", (req, res) => {
  res.json({
    status: "ok",
    python_command: PYTHON_CMD,
    current_results: currentResults.length,
    total_stored: store.jobs.length,
    pending_sync: store.jobs.filter(j => !j._forwarded).length
  });
});


app.listen(PORT, () => {
  console.log(`Server on http://localhost:${PORT}`);
  console.log(`Using Python command: ${PYTHON_CMD}`);
  console.log(`Open http://localhost:${PORT} in your browser\n`);
});
