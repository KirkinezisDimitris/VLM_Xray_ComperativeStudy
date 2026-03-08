import fs from "fs";
import path from "path";
import { db } from "../src/db.js";

const XRAY_DIR = path.join(process.cwd(), "xray"); // φάκελος εικόνων

async function run() {
  const files = fs.readdirSync(XRAY_DIR);

  const patients = {};

  for (const file of files) {
    if (!file.endsWith(".png") && !file.endsWith(".jpg")) continue;

    const patientCode = file.split("_")[0];

    if (!patients[patientCode]) {
      patients[patientCode] = [];
    }

    patients[patientCode].push(file);
  }

  let inserted = 0;

  for (const code of Object.keys(patients)) {
    const images = patients[code];

    const image1 = images[0] || null;
    const image2 = images[1] || null;

    await db.query(
      `
      INSERT INTO patients (patient_code, image1_path, image2_path)
      VALUES (?, ?, ?)
      `,
      [code, image1, image2]
    );

    inserted++;
  }

  console.log("Inserted patients:", inserted);
  process.exit();
}

run();