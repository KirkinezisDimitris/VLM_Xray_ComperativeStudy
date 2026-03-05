import express from "express";
import bcrypt from "bcrypt";
import { db } from "../db.js";

const router = express.Router();

// POST /api/login
router.post("/login", async (req, res) => {
  try {
    const { username, password } = req.body ?? {};
    if (!username || !password) return res.status(400).send("Missing username or password");

    const [rows] = await db.query(
      "SELECT id, username, password_hash, role FROM accounts WHERE username = ? LIMIT 1",
      [username]
    );

    if (!rows.length) return res.status(401).send("Invalid credentials");

    const user = rows[0];
    const ok = await bcrypt.compare(password, user.password_hash);
    if (!ok) return res.status(401).send("Invalid credentials");

    // Minimal: επιστρέφουμε user info για localStorage
    res.json({
      user: { id: user.id, username: user.username, role: user.role }
    });
  } catch (err) {
    console.error(err);
    res.status(500).send("Server error");
  }
});

export default router;