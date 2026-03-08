const LS_USER = "mr_user";
const user = Auth.requireAuth();
if (!user) throw new Error("Unauthorized");
const USER_ID = user.id;

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error("Request failed");
  return res.json();
}

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!res.ok) throw new Error("Request failed");
  return res.json();
}

(async function boot(){
  document.getElementById("backBtn").addEventListener("click", () => {
    window.location.href = "/patient.html";
  });

  const data = await getJSON(`/api/history?userId=${USER_ID}`);
  const list = document.getElementById("list");
  const sub = document.getElementById("subText");

  sub.textContent = `Visited: ${data.patients.length} • Current position: ${data.current_pos + 1}`;

  if (!data.patients.length) {
    list.innerHTML = `<p class="muted">No visited patients yet.</p>`;
    return;
  }

  list.innerHTML = data.patients.map(p => {
    const humanIndex = p.queue_pos + 1;
    return `
      <div class="hRow">
        <div class="hRow__left">
          <div class="hRow__title">Patient #${humanIndex}</div>
          <div class="hRow__meta">Queue position: ${humanIndex}</div>
        </div>
        <button class="btn btn--primary" data-pos="${p.queue_pos}" data-pid="${p.patient_id}">
          Open
        </button>
      </div>
    `;
  }).join("");

  list.addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-pos]");
    if (!btn) return;

    const queue_pos = Number(btn.getAttribute("data-pos"));
    const patient_id = Number(btn.getAttribute("data-pid"));

window.location.href = `/history_patient.html?id=${patient_id}`;
  });
})();

document.getElementById("logoutBtn")?.addEventListener("click", () => {
  Auth.logout();
});