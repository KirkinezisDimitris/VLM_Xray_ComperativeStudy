import historyRoutes from "./routes/history.routes.js";
import express from "express";
import path from "path";
import { fileURLToPath } from "url";
import { db } from "./db.js";
import authRoutes from "./routes/auth.routes.js";


const app = express();
app.use(express.json());
app.use("/api", historyRoutes);
app.use("/api", authRoutes);
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const XRAY_DIR = path.join(__dirname, "../../../Final Xray Collection");
const publicDir = path.join(__dirname, "../../client/public");
app.use("/xray", express.static(XRAY_DIR));
app.use(express.static(publicDir));

function shuffle(arr) {
  const a = arr.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

// Create random queue once per user + init progress
async function ensureQueue(userId) {
  const [[qCount]] = await db.query(
    "SELECT COUNT(*) AS c FROM user_patient_queue WHERE user_id=?",
    [userId]
  );
  if (qCount.c > 0) {
    // ensure progress exists
    await db.query(
      "INSERT INTO user_progress (user_id, current_pos) VALUES (?, 0) ON DUPLICATE KEY UPDATE user_id=user_id",
      [userId]
    );
    return;
  }

  const [patients] = await db.query("SELECT id FROM patients");
  const ids = shuffle(patients.map(p => p.id));

  const values = ids.map((pid, pos) => [userId, pid, pos]);
  if (values.length) {
    await db.query(
      `INSERT INTO user_patient_queue (user_id, patient_id, queue_pos)
       VALUES ${values.map(() => "(?, ?, ?)").join(", ")}`,
      values.flat()
    );
  }

  await db.query(
    "INSERT INTO user_progress (user_id, current_pos) VALUES (?, 0) ON DUPLICATE KEY UPDATE current_pos=current_pos",
    [userId]
  );
}

// GET current patient for user
// GET /api/current?userId=1
app.get("/api/current", async (req, res) => {
  const userId = Number(req.query.userId);
  if (!Number.isFinite(userId)) return res.status(400).json({ error: "userId required" });

  await ensureQueue(userId);

  const [[progress]] = await db.query(
    "SELECT current_pos FROM user_progress WHERE user_id=?",
    [userId]
  );

  const [[totalRow]] = await db.query(
    "SELECT COUNT(*) AS total FROM user_patient_queue WHERE user_id=?",
    [userId]
  );

  if (progress.current_pos >= totalRow.total) {
    return res.json({ done: true, total: totalRow.total, current_pos: progress.current_pos });
  }

  const [rows] = await db.query(
    `
    SELECT q.queue_pos, p.id AS patient_id, p.patient_code, p.image1_path, p.image2_path
    FROM user_patient_queue q
    JOIN patients p ON p.id = q.patient_id
    WHERE q.user_id = ? AND q.queue_pos = ?
    LIMIT 1
    `,
    [userId, progress.current_pos]
  );

  if (!rows.length) {
    return res.json({ done: true, total: totalRow.total, current_pos: progress.current_pos });
  }

  res.json({
    done: false,
    total: totalRow.total,
    current_pos: progress.current_pos,
    patient: rows[0],
  });
});

// POST next patient (advance pointer)
// POST /api/next?userId=1
app.post("/api/next", async (req, res) => {
  const userId = Number(req.query.userId);
  if (!Number.isFinite(userId)) return res.status(400).json({ error: "userId required" });

  await ensureQueue(userId);

  await db.query(
    "UPDATE user_progress SET current_pos = current_pos + 1 WHERE user_id=?",
    [userId]
  );

  res.json({ ok: true });
});

// GET questionnaire data (images + findings + saved answers)
// GET /api/patients/:id/questionnaire
app.get("/api/patients/:id/questionnaire", async (req, res) => {
  const patientId = Number(req.params.id);
  if (!Number.isFinite(patientId)) return res.status(400).json({ error: "Invalid patient id" });

  const [rows] = await db.query(
    `
    SELECT
      p.id AS patient_id,
      p.patient_code,
      p.image1_path,
      p.image2_path,
      f.id AS finding_id,
      f.name AS finding_name,
      pa.answer_choice
    FROM patients p
    JOIN findings f
    LEFT JOIN patient_answers pa
      ON pa.patient_id = p.id
     AND pa.finding_id = f.id
    WHERE p.id = ?
    ORDER BY f.id
    `,
    [patientId]
  );

  if (!rows.length) return res.status(404).json({ error: "Patient not found" });

  res.json({
    patient: {
      id: rows[0].patient_id,
      patient_code: rows[0].patient_code,
      image1_path: rows[0].image1_path,
      image2_path: rows[0].image2_path,
    },
    findings: rows.map(r => ({
      finding_id: r.finding_id,
      finding_name: r.finding_name,
      answer_choice: r.answer_choice ?? null
    }))
  });
});

// PUT save answers (bulk upsert 14)
// PUT /api/patients/:id/answers?userId=1
app.put("/api/patients/:id/answers", async (req, res) => {
  const patientId = Number(req.params.id);
  const userId = Number(req.query.userId);
  if (!Number.isFinite(patientId)) return res.status(400).json({ error: "Invalid patient id" });
  if (!Number.isFinite(userId)) return res.status(400).json({ error: "userId required" });

  const answers = req.body?.answers;
  if (!Array.isArray(answers) || answers.length !== 14) {
    return res.status(400).json({ error: "Expected answers array of length 14" });
  }

  for (const a of answers) {
    const fid = Number(a.finding_id);
    const ch = Number(a.answer_choice);
    if (!Number.isFinite(fid) || ![1,2,3].includes(ch)) {
      return res.status(400).json({ error: "Invalid answers payload" });
    }
  }

  const values = answers.map(a => [patientId, a.finding_id, a.answer_choice, userId]);

  await db.query(
    `
    INSERT INTO patient_answers (patient_id, finding_id, answer_choice, updated_by)
    VALUES ${values.map(() => "(?, ?, ?, ?)").join(", ")}
    ON DUPLICATE KEY UPDATE
      answer_choice = VALUES(answer_choice),
      updated_by    = VALUES(updated_by),
      updated_at    = CURRENT_TIMESTAMP
    `,
    values.flat()
  );

  res.json({ ok: true });
});

// default
app.get("/", (req, res) => {
  res.sendFile(path.join(publicDir, "index.html"));
});

const PORT = process.env.PORT || 3000;

app.listen(PORT, () => {
  console.log(`Server running on port ${PORT}`);
});

// GET /api/history?userId=1
// returns patients with queue_pos < current_pos (already visited)
app.get("/api/history", async (req, res) => {
  const userId = Number(req.query.userId);
  if (!Number.isFinite(userId)) return res.status(400).json({ error: "userId required" });

  await ensureQueue(userId);

  const [[progress]] = await db.query(
    "SELECT current_pos FROM user_progress WHERE user_id=?",
    [userId]
  );

  const [rows] = await db.query(
    `
    SELECT q.queue_pos, p.id AS patient_id
    FROM user_patient_queue q
    JOIN patients p ON p.id = q.patient_id
    WHERE q.user_id = ? AND q.queue_pos < ?
    ORDER BY q.queue_pos DESC
    `,
    [userId, progress.current_pos]
  );

  res.json({
    current_pos: progress.current_pos,
    patients: rows.map(r => ({
      patient_id: r.patient_id,
      queue_pos: r.queue_pos
    }))
  });
});

// POST /api/goto?userId=1
// body: { queue_pos: number }
app.post("/api/goto", async (req, res) => {
  const userId = Number(req.query.userId);
  const queuePos = Number(req.body?.queue_pos);

  if (!Number.isFinite(userId)) return res.status(400).json({ error: "userId required" });
  if (!Number.isFinite(queuePos) || queuePos < 0) return res.status(400).json({ error: "queue_pos required" });

  await ensureQueue(userId);

  // validate exists
  const [rows] = await db.query(
    "SELECT 1 FROM user_patient_queue WHERE user_id=? AND queue_pos=? LIMIT 1",
    [userId, queuePos]
  );
  if (!rows.length) return res.status(404).json({ error: "queue_pos not found" });

  await db.query(
    "UPDATE user_progress SET current_pos=? WHERE user_id=?",
    [queuePos, userId]
  );

  res.json({ ok: true });
});