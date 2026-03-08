import bcrypt from "bcrypt";
import { db } from "../src/db.js";

const users = [

  {username:"rad_01", password:"T9!kLm3#Qx72", role:"doctor"},
  {username:"rad_02", password:"Z4@pLm82!TxQ", role:"doctor"},
  {username:"rad_03", password:"Lm!82Qx@7TpK", role:"doctor"},
  {username:"rad_04", password:"Q7@LmT9!Px21", role:"doctor"},
  {username:"rad_05", password:"Tx!82Lm@Qp61", role:"doctor"},
  {username:"rad_06", password:"Pq9!Lm@72TxK", role:"doctor"},
  {username:"rad_07", password:"Lm@71Tx!Pq29", role:"doctor"},
  {username:"rad_08", password:"Qx!92Lm@T7Pk", role:"doctor"},

  {username:"chief_rad_01", password:"D!92Lm@Qx71P", role:"director"},
  {username:"chief_rad_02", password:"R@71Tx!Lm92Q", role:"director"},
  {username:"chief_rad_03", password:"Qp!72Lm@Tx81", role:"director"},
  {username:"chief_rad_04", password:"Lm!91Tx@Qp27", role:"director"},

  {username:"admin_sys_01", password:"A@82Lm!Qp71T", role:"admin"},
  {username:"admin_sys_02", password:"S!92Tx@Lm71Q", role:"admin"},

  {username:"Researcher", password:"Pap@K0stAs!91", role:"Papakostas"}

];

async function run(){

  for(const u of users){

    const hash = await bcrypt.hash(u.password,10);

    await db.query(
      "INSERT INTO accounts (username,password_hash,role) VALUES (?,?,?)",
      [u.username,hash,u.role]
    );

    console.log("Created user:",u.username);
  }

  console.log("All users created");
  process.exit();
}

run();