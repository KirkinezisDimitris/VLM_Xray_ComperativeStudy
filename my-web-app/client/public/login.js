// keys used across the app
const LS_USER = "mr_user"; // { id, username, role }

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const txt = await res.text();
  if (!res.ok) throw new Error(txt || "Login failed");
  return JSON.parse(txt);
}

document.getElementById("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();

  const errorEl = document.getElementById("error");
  errorEl.textContent = "";

  const username = document.getElementById("username").value.trim();
  const password = document.getElementById("password").value;

  try {
    const data = await postJSON("/api/login", { username, password });
    localStorage.setItem(LS_USER, JSON.stringify(data.user));

    // μετά το login, πάμε κατευθείαν στο questionnaire flow
    window.location.href = "/index.html";
  } catch (err) {
    errorEl.textContent = err.message || "Login failed";
  }
})
localStorage.setItem("mr_user", JSON.stringify(data.user));
window.location.href = "/index.html";