import fs from "fs/promises";
import path from "path";
import mysql from "mysql2/promise";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// 🔧 άλλαξέ το αν το dataset folder έχει άλλο όνομα
const XRAY_DIR = path.join(__dirname, "../../../Final Xray Collection");

// Τα filenames που περιμένουμε
const IMG1 = "view1_frontal.jpg";
const IMG2 = "view2_lateral.jpg";

// DB config (XAMPP)
const pool = mysql.createPool({
  host: "127.0.0.1",
  user: "root",
  password: "", // βάλε αν έχεις
  database: "vlmxray", // ή medresearch (ό,τι έχεις)
  waitForConnections: true,
  connectionLimit: 10,
});

async function exists(p) {
  try {
    await fs.access(p);
    return true;
  } catch {
    return false;
  }
}

async function main() {
  console.log("XRAY_DIR =", XRAY_DIR);

  const entries = await fs.readdir(XRAY_DIR, { withFileTypes: true });
  const patientDirs = entries
    .filter((e) => e.isDirectory())
    .map((e) => e.name)
    .filter((name) => name.toLowerCase().startsWith("patient")); // patient00032 etc

  console.log("Found patient folders:", patientDirs.length);

  let inserted = 0;
  let skipped = 0;

  for (const patientCode of patientDirs) {
    // dataset structure: patientXXXX/study1/*.jpg
    const studyDir = path.join(XRAY_DIR, patientCode, "study1");

    const img1Fs = path.join(studyDir, IMG1);
    const img2Fs = path.join(studyDir, IMG2);

    const ok1 = await exists(img1Fs);
    const ok2 = await exists(img2Fs);

    if (!ok1 || !ok2) {
      skipped++;
      console.log(`[SKIP] ${patientCode} missing files:`, {
        [IMG1]: ok1,
        [IMG2]: ok2,
      });
      continue;
    }

    // ✅ Αποθηκεύουμε URL paths (αυτά θα μπαίνουν σε <img src="...">)
    const image1Url = `/xray/${patientCode}/study1/${IMG1}`;
    const image2Url = `/xray/${patientCode}/study1/${IMG2}`;

    // Upsert by patient_code
    await pool.query(
      `
      INSERT INTO patients (patient_code, image1_path, image2_path)
      VALUES (?, ?, ?)
      ON DUPLICATE KEY UPDATE
        image1_path = VALUES(image1_path),
        image2_path = VALUES(image2_path)
      `,
      [patientCode, image1Url, image2Url]
    );

    inserted++;
  }

  console.log(`DONE. inserted/updated=${inserted}, skipped=${skipped}`);
  await pool.end();
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});