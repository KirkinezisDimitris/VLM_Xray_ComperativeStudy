import mysql from "mysql2/promise";

export const db = mysql.createPool({
  host: "127.0.0.1",
  user: "root",
  database: "vlmxray",
  waitForConnections: true,
  connectionLimit: 10,
});