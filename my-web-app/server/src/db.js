import mysql from "mysql2/promise";

export const db = mysql.createPool({
  host: "vlm-xray-web-app-database-wv66hu",
  user: "app_user",
  password: "1133vlmxraydb",
  database: "vlmxray",
  port: 3306,
  waitForConnections: true,
  connectionLimit: 10
});