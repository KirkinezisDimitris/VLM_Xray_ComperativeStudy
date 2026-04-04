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
const __dirname  = path.dirname(__filename);

const XRAY_DIR = "/app/Final Xray Collection";
const publicDir = path.join(__dirname, "../../client/public");

app.use("/xray", express.static(XRAY_DIR));
app.use(express.static(publicDir));

/* ─────────────────────────────────────────
   Helpers
───────────────────────────────────────── */
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
    const [[progress]] = await db.query(
      "SELECT 1 FROM user_progress WHERE user_id=?",
      [userId]
    );
    if (!progress) {
      await db.query(
        "INSERT INTO user_progress (user_id, current_pos) VALUES (?, 0)",
        [userId]
      );
    }
    return;
  }

  const [patients] = await db.query("SELECT id FROM patients");
  const ids    = shuffle(patients.map(p => p.id));
  const values = ids.map((pid, pos) => [userId, pid, pos]);

  if (values.length) {
    await db.query(
      `INSERT INTO user_patient_queue (user_id, patient_id, queue_pos)
       VALUES ${values.map(() => "(?, ?, ?)").join(", ")}`,
      values.flat()
    );
  }

  await db.query(
    `INSERT INTO user_progress (user_id, current_pos) VALUES (?, 0)
     ON DUPLICATE KEY UPDATE current_pos = current_pos`,
    [userId]
  );
}

/* ─────────────────────────────────────────
   GET /api/current?userId=1
   Returns the current patient in the queue
───────────────────────────────────────── */
app.get("/api/current", async (req, res) => {
  try {
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
      `SELECT q.queue_pos, p.id AS patient_id, p.patient_code, p.image1_path, p.image2_path
       FROM user_patient_queue q
       JOIN patients p ON p.id = q.patient_id
       WHERE q.user_id = ? AND q.queue_pos = ?
       LIMIT 1`,
      [userId, progress.current_pos]
    );

    if (!rows.length) {
      return res.json({ done: true, total: totalRow.total, current_pos: progress.current_pos });
    }

    res.json({
      done:        false,
      total:       totalRow.total,
      current_pos: progress.current_pos,
      patient:     rows[0],
    });
  } catch (err) {
    console.error("🔥 CURRENT ERROR:", err);
    res.status(500).json({ error: err.message });
  }
});

/* ─────────────────────────────────────────
   POST /api/next?userId=1
   Auto-fills NEGATIVE for any missing answers, then advances the pointer
───────────────────────────────────────── */
app.post("/api/next", async (req, res) => {
  try {
    const userId = Number(req.query.userId);
    if (!Number.isFinite(userId)) return res.status(400).json({ error: "userId required" });

    await ensureQueue(userId);

    const [[progress]] = await db.query(
      "SELECT current_pos FROM user_progress WHERE user_id=?",
      [userId]
    );

    const [rows] = await db.query(
      `SELECT patient_id FROM user_patient_queue WHERE user_id=? AND queue_pos=?`,
      [userId, progress.current_pos]
    );

    if (rows.length) {
      const patientId = rows[0].patient_id;

      const [findings] = await db.query("SELECT id FROM findings");
      const [answers]  = await db.query(
        "SELECT finding_id FROM patient_answers WHERE patient_id=? AND user_id=?",
        [patientId, userId]
      );

      const answered = new Set(answers.map(a => a.finding_id));

      const [users] = await db.query(
        "SELECT username, role FROM accounts WHERE id=?",
        [userId]
      );
      if (!users.length) return res.status(400).json({ error: "User not found" });

      const user = users[0];

      // Only insert findings that have NO answer yet (never overwrite existing)
      const missing = findings
        .filter(f => !answered.has(f.id))
        .map(f => [userId, user.username, user.role, patientId, f.id, 2]); // 2 = NEGATIVE

      if (missing.length > 0) {
        await db.query(
          `INSERT INTO patient_answers (user_id, username, role, patient_id, finding_id, answer_choice)
           VALUES ${missing.map(() => "(?, ?, ?, ?, ?, ?)").join(", ")}
           ON DUPLICATE KEY UPDATE answer_choice = VALUES(answer_choice), updated_at = CURRENT_TIMESTAMP`,
          missing.flat()
        );
      }
    }

    await db.query(
      "UPDATE user_progress SET current_pos = current_pos + 1 WHERE user_id=?",
      [userId]
    );

    res.json({ ok: true });
  } catch (err) {
    console.error("🔥 NEXT ERROR:", err);
    res.status(500).json({ error: err.message });
  }
});

/* ─────────────────────────────────────────
   GET /api/patients/:id/questionnaire?userId=1
   Returns patient images + all 14 findings with saved answers

   FIX: added proper ON clause to JOIN findings (was cartesian product before)
   FIX: answer_choice defaults to 2 (NEGATIVE) instead of null,
        so the frontend always has a valid number to compare against
───────────────────────────────────────── */
app.get("/api/patients/:id/questionnaire", async (req, res) => {
  try {
    const patientId = Number(req.params.id);
    if (!Number.isFinite(patientId)) return res.status(400).json({ error: "Invalid patient id" });

    const userId = Number(req.query.userId);
    if (!Number.isFinite(userId)) return res.status(400).json({ error: "userId required" });

    const [rows] = await db.query(
      `SELECT
         p.id          AS patient_id,
         p.patient_code,
         p.image1_path,
         p.image2_path,
         f.id          AS finding_id,
         f.name        AS finding_name,
         pa.answer_choice
       FROM patients p
       -- FIX: was "JOIN findings f" with no ON clause (cartesian product)
       JOIN findings f ON 1=1
       LEFT JOIN patient_answers pa
         ON  pa.patient_id = p.id
         AND pa.finding_id = f.id
         AND pa.user_id    = ?
       WHERE p.id = ?
       ORDER BY f.id`,
      [userId, patientId]
    );

    if (!rows.length) return res.status(404).json({ error: "Patient not found" });

    res.json({
      patient: {
        id:           rows[0].patient_id,
        patient_code: rows[0].patient_code,
        image1_path:  `/xray/${rows[0].image1_path}`,
        image2_path:  `/xray/${rows[0].image2_path}`,
      },
      findings: rows.map(r => ({
        finding_id:    r.finding_id,
        finding_name:  r.finding_name,
        // FIX: was ?? null — frontend Number(null)=0 never matched 1/2/3
        // Now sends 2 (NEGATIVE) as default so unanswered findings still render correctly
        answer_choice: r.answer_choice !== null && r.answer_choice !== undefined
          ? Number(r.answer_choice)
          : null,
      })),
    });
  } catch (err) {
    console.error("🔥 QUESTIONNAIRE ERROR:", err);
    res.status(500).json({ error: err.message });
  }
});

/* ─────────────────────────────────────────
   PUT /api/patients/:id/answers?userId=1
   Bulk upsert all 14 answers (auto-fills NEGATIVE for any missing)
───────────────────────────────────────── */
app.put("/api/patients/:id/answers", async (req, res) => {
  try {
    const patientId = Number(req.params.id);
    const userId    = Number(req.query.userId);

    if (!Number.isFinite(patientId)) return res.status(400).json({ error: "Invalid patient id" });
    if (!Number.isFinite(userId))    return res.status(400).json({ error: "userId required" });

    const answers = req.body?.answers;
    if (!Array.isArray(answers)) return res.status(400).json({ error: "Invalid answers payload" });

    const [findings] = await db.query("SELECT id FROM findings");

    const answerMap = new Map(
      answers.map(a => [Number(a.finding_id), Number(a.answer_choice)])
    );

    // Build full 14-row answer list; default to NEGATIVE for any gaps
    const fullAnswers = findings.map(f => {
      const value = answerMap.get(f.id);
      return {
        finding_id:    f.id,
        answer_choice: [1, 2, 3].includes(value) ? value : 2,
      };
    });

    const [users] = await db.query(
      "SELECT username, role FROM accounts WHERE id=?",
      [userId]
    );
    if (!users.length) return res.status(400).json({ error: "User not found" });

    const user = users[0];

    const values = fullAnswers.map(a => [
      userId,
      user.username,
      user.role,
      patientId,
      a.finding_id,
      a.answer_choice,
    ]);

    await db.query(
      `INSERT INTO patient_answers (user_id, username, role, patient_id, finding_id, answer_choice)
       VALUES ${values.map(() => "(?, ?, ?, ?, ?, ?)").join(", ")}
       ON DUPLICATE KEY UPDATE
         answer_choice = VALUES(answer_choice),
         updated_at    = CURRENT_TIMESTAMP`,
      values.flat()
    );

    res.json({ ok: true });
  } catch (err) {
    console.error("🔥 SAVE ERROR:", err);
    res.status(500).json({ error: err.message });
  }
});

/* ─────────────────────────────────────────
   GET /api/history?userId=1
───────────────────────────────────────── */
app.get("/api/history", async (req, res) => {
  try {
    const userId = Number(req.query.userId);
    if (!Number.isFinite(userId)) return res.status(400).json({ error: "userId required" });

    await ensureQueue(userId);

    const [[progress]] = await db.query(
      "SELECT current_pos FROM user_progress WHERE user_id=?",
      [userId]
    );

    const [rows] = await db.query(
      `SELECT q.queue_pos, p.id AS patient_id
       FROM user_patient_queue q
       JOIN patients p ON p.id = q.patient_id
       WHERE q.user_id = ? AND q.queue_pos < ?
       ORDER BY q.queue_pos DESC`,
      [userId, progress.current_pos]
    );

    res.json({
      current_pos: progress.current_pos,
      patients:    rows.map(r => ({
        patient_id: r.patient_id,
        queue_pos:  r.queue_pos,
      })),
    });
  } catch (err) {
    console.error("🔥 HISTORY ERROR:", err);
    res.status(500).json({ error: err.message });
  }
});

/* ─────────────────────────────────────────
   POST /api/goto?userId=1
   body: { queue_pos: number }
───────────────────────────────────────── */
app.post("/api/goto", async (req, res) => {
  try {
    const userId   = Number(req.query.userId);
    const queuePos = Number(req.body?.queue_pos);

    if (!Number.isFinite(userId))                    return res.status(400).json({ error: "userId required" });
    if (!Number.isFinite(queuePos) || queuePos < 0)  return res.status(400).json({ error: "queue_pos required" });

    await ensureQueue(userId);

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
  } catch (err) {
    console.error("🔥 GOTO ERROR:", err);
    res.status(500).json({ error: err.message });
  }
});

/* ─────────────────────────────────────────
   Catch-all → serve frontend
───────────────────────────────────────── */
app.get("*", (req, res) => {
  res.sendFile(path.join(publicDir, "index.html"));
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log(`Server running on port ${PORT}`));