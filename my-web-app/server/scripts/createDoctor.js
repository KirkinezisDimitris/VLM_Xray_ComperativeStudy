import bcrypt from "bcrypt";
import { db } from "../src/db.js";

const username = process.argv[2];
const password = process.argv[3];
const role = "doctor";

if (!username || !password) {
  console.log("Usage: node scripts/createDoctor.js <username> <password>");
  process.exit(1);
}

const hash = await bcrypt.hash(password, 10);

await db.query(
  "INSERT INTO accounts (username, password_hash, role) VALUES (?, ?, ?)",
  [username, hash, role]
);

console.log("Doctor created:", username);
process.exit(0);