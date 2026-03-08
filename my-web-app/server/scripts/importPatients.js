import fs from "fs";
import path from "path";
import { db } from "../src/db.js";

const XRAY_DIR = path.join(process.cwd(), "../../Final Xray Collection");
async function run() {

  const folders = fs.readdirSync(XRAY_DIR);

  let inserted = 0;

  for (const folder of folders) {

    const patientPath = path.join(XRAY_DIR, folder);

    if (!fs.statSync(patientPath).isDirectory()) continue;

    const files = fs.readdirSync(patientPath)
      .filter(f => f.endsWith(".png") || f.endsWith(".jpg"));

    const image1 = files[0] ? `${folder}/${files[0]}` : null;
    const image2 = files[1] ? `${folder}/${files[1]}` : null;

    await db.query(
      `
      INSERT INTO patients (patient_code, image1_path, image2_path)
      VALUES (?, ?, ?)
      `,
      [folder, image1, image2]
    );

    inserted++;
  }

  console.log("Patients inserted:", inserted);
  process.exit();


}

run();