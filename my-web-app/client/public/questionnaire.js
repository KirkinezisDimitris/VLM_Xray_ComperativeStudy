const user = Auth.requireAuth();
if (!user) throw new Error("Unauthorized");
const USER_ID = user.id;
function setupImageZoom() {
  const modal = document.getElementById("imgModal");
  const modalImg = document.getElementById("imgModalContent");
  const closeBtn = document.getElementById("imgModalClose");

  function open(src) {
    modalImg.src = src;
    modal.classList.add("active");
    modal.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
  }

  function close() {
    modal.classList.remove("active");
    modal.setAttribute("aria-hidden", "true");
    modalImg.src = "";
    document.body.style.overflow = "";
  }

  // click on images
  const img1 = document.getElementById("img1");
  const img2 = document.getElementById("img2");

  img1?.addEventListener("click", () => open(img1.src));
  img2?.addEventListener("click", () => open(img2.src));

  // close actions
  closeBtn?.addEventListener("click", close);

  // click outside image closes
  modal?.addEventListener("click", (e) => {
    if (e.target === modal || e.target === modalImg) close();
  });

  // ESC closes
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") close();
  });
}

const LS_USER = "mr_user";


const ANSWERS = [
  { label: "POSITIVE", value: 1 },
  { label: "NEGATIVE", value: 2 },
  { label: "UNCERTAIN", value: 3 },
];

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error("Request failed");
  return res.json();
}

async function putJSON(url, body) {
  const res = await fetch(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error("Request failed");
  return res.json();
}

async function postJSON(url) {
  const res = await fetch(url, { method: "POST" });
  if (!res.ok) throw new Error("Request failed");
  return res.json();
}

function render(data, meta) {
  const { patient, findings } = data;

  document.getElementById("img1").src = patient.image1_path;
  document.getElementById("img2").src = patient.image2_path;

  document.getElementById("patientTitle").textContent =
    `Patient ${meta.current_pos + 1} / ${meta.total}`;

  const form = document.getElementById("form");
  form.innerHTML = findings.map((f) => {
    const group = `finding_${f.finding_id}`;
const radios = ANSWERS.map(a => {
  const checked = (f.answer_choice === a.value) ? "checked" : "";
  return `
    <label class="choice">
      <input type="radio" name="${group}" value="${a.value}" ${checked}/>
      ${a.label}
    </label>
  `;
}).join("");

    return `
      <div class="qItem">
        <h4>${f.finding_name}</h4>
        <div class="choices">${radios}</div>
      </div>
    `;
  }).join("");
}

function collectAnswers(findings) {
  const answers = [];
  for (const f of findings) {
    const name = `finding_${f.finding_id}`;
    const checked = document.querySelector(`input[name="${name}"]:checked`);
    if (!checked) return null;
    answers.push({ finding_id: f.finding_id, answer_choice: Number(checked.value) });
  }
  return answers;
}

(async function boot() {
  // Always load the "current" patient from server progress
  const current = await getJSON(`/api/current?userId=${USER_ID}`);
  if (current.done) {
    alert("Finished! No more patients.");
    window.location.href = "/patients.html";
    return;
  }

  const patientId = current.patient.patient_id;

  const data = await getJSON(`/api/patients/${patientId}/questionnaire`);
  render(data, current);
  setupImageZoom();

  // Save
  document.getElementById("saveBtn").addEventListener("click", async () => {
    const answers = collectAnswers(data.findings);
    if (!answers) return alert("Answer all 14 first.");

    await putJSON(`/api/patients/${patientId}/answers?userId=${USER_ID}`, { answers });
    alert("Saved ✅");
  });

  // Next patient
document.getElementById("nextBtn").addEventListener("click", async () => {

  const answers = [];

  for (const f of data.findings) {
    const name = `finding_${f.finding_id}`;
    const checked = document.querySelector(`input[name="${name}"]:checked`);
    if (checked) {
      answers.push({
        finding_id: f.finding_id,
        answer_choice: Number(checked.value)
      });
    }
  }

  await putJSON(`/api/patients/${patientId}/answers?userId=${USER_ID}`, { answers });
  await postJSON(`/api/next?userId=${USER_ID}`);

  window.location.reload();
});
})();

document.getElementById("logoutBtn")?.addEventListener("click", () => {
  Auth.logout();
});