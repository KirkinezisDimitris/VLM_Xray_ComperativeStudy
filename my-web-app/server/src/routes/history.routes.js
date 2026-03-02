import express from "express";
import { db } from "../db.js";

const router = express.Router();

/**
 * GET /api/history?userId=1
 * returns visited patients (queue_pos < current_pos)
 */
router.get("/history", async (req, res) => {
  try {
    const userId = Number(req.query.userId);
    if (!Number.isFinite(userId)) return res.status(400).json({ error: "userId required" });

    // Ensure queue exists
    const [[qCount]] = await db.query(
      "SELECT COUNT(*) AS c FROM user_patient_queue WHERE user_id=?",
      [userId]
    );

    if (qCount.c === 0) {
      return res.json({ current_pos: 0, patients: [] });
    }

    const [[progress]] = await db.query(
      "SELECT current_pos FROM user_progress WHERE user_id=?",
      [userId]
    );

    const [rows] = await db.query(
      `
      SELECT q.queue_pos, q.patient_id
      FROM user_patient_queue q
      WHERE q.user_id = ? AND q.queue_pos < ?
      ORDER BY q.queue_pos DESC
      `,
      [userId, progress?.current_pos ?? 0]
    );

    res.json({
      current_pos: progress?.current_pos ?? 0,
      patients: rows.map(r => ({
        patient_id: r.patient_id,
        queue_pos: r.queue_pos
      }))
    });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: "Server error" });
  }
});

/**
 * POST /api/goto?userId=1
 * body: { queue_pos: number }
 */
router.post("/goto", async (req, res) => {
  try {
    const userId = Number(req.query.userId);
    const queuePos = Number(req.body?.queue_pos);

    if (!Number.isFinite(userId)) return res.status(400).json({ error: "userId required" });
    if (!Number.isFinite(queuePos) || queuePos < 0) return res.status(400).json({ error: "queue_pos required" });

    const [exists] = await db.query(
      "SELECT 1 FROM user_patient_queue WHERE user_id=? AND queue_pos=? LIMIT 1",
      [userId, queuePos]
    );
    if (!exists.length) return res.status(404).json({ error: "queue_pos not found" });

    await db.query(
      "UPDATE user_progress SET current_pos=? WHERE user_id=?",
      [queuePos, userId]
    );

    res.json({ ok: true });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: "Server error" });
  }
});

export default router;