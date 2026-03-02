import express from "express";
import { db } from "../db.js";

const router = express.Router();

/**
 * GET /api/patients/:id/questionnaire
 * Returns: patient info + findings + current answers
 */
router.get("/patients/:id/questionnaire", async (req, res) => {
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
    ORDER BY f.id;
    `,
    [patientId]
  );

  if (!rows.length) return res.status(404).json({ error: "Patient not found" });

  const patient = {
    id: rows[0].patient_id,
    patient_code: rows[0].patient_code,
    image1_path: rows[0].image1_path,
    image2_path: rows[0].image2_path,
  };

  const findings = rows.map(r => ({
    finding_id: r.finding_id,
    finding_name: r.finding_name,
    answer_choice: r.answer_choice ?? null, // 1/2/3 or null
  }));

  res.json({ patient, findings });
});

/**
 * PUT /api/patients/:id/answers
 * body: { answers: [{ finding_id: number, answer_choice: 1|2|3 }, ...] }
 * bulk upsert
 */
router.put("/patients/:id/answers", async (req, res) => {
  const patientId = Number(req.params.id);
  if (!Number.isFinite(patientId)) return res.status(400).json({ error: "Invalid patient id" });

  const answers = req.body?.answers;
  if (!Array.isArray(answers) || answers.length !== 14) {
    return res.status(400).json({ error: "Expected answers array of length 14" });
  }

  // TODO: όταν κάνεις auth, πάρε updated_by από session/user
  const updatedBy = null;

  // Validate
  for (const a of answers) {
    const fid = Number(a.finding_id);
    const ch = Number(a.answer_choice);
    if (!Number.isFinite(fid) || ![1, 2, 3].includes(ch)) {
      return res.status(400).json({ error: "Invalid finding_id or answer_choice" });
    }
  }

  // Build bulk insert
  const values = answers.map(a => [patientId, a.finding_id, a.answer_choice, updatedBy]);

  await db.query(
    `
    INSERT INTO patient_answers (patient_id, finding_id, answer_choice, updated_by)
    VALUES ${values.map(() => "(?, ?, ?, ?)").join(", ")}
    ON DUPLICATE KEY UPDATE
      answer_choice = VALUES(answer_choice),
      updated_by = VALUES(updated_by),
      updated_at = CURRENT_TIMESTAMP;
    `,
    values.flat()
  );

  res.json({ ok: true });
});

export default router;