let findingsData = [];
const LS_USER = "mr_user";
const user = Auth.requireAuth();
if (!user) throw new Error("Unauthorized");
Auth.setupProfileMenu();
const USER_ID = user.id;
const ANSWERS = [
  { label: "POSITIVE", value: 1 },
  { label: "NEGATIVE", value: 2 },
  { label: "UNCERTAIN", value: 3 },
];

function getPatientIdFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const id = Number(params.get("id"));
  return Number.isFinite(id) ? id : null;
}

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

  const img1 = document.getElementById("img1");
  const img2 = document.getElementById("img2");

  img1?.addEventListener("click", () => open(img1.src));
  img2?.addEventListener("click", () => open(img2.src));

  closeBtn?.addEventListener("click", close);
  modal?.addEventListener("click", (e) => {
    if (e.target === modal || e.target === modalImg) close();
  });

  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") close();
  });
}

function render({ patient, findings }) {
  // do NOT display patient_code (anonymity)
  document.getElementById("progressText").textContent = `Editing answers • 14 findings`;

  document.getElementById("img1").src = patient.image1_path;
  document.getElementById("img2").src = patient.image2_path;

  const form = document.getElementById("form");
  form.innerHTML = findings.map((f, idx) => {
    const group = `finding_${f.finding_id}`;
    const radios = ANSWERS.map(a => {
      const checked = (f.answer_choice === a.value) ? "checked" : "";
      return `
        <label class="choice">
          <input type="radio" name="${group}" value="${a.value}" ${checked} />
          ${a.label}
        </label>
      `;
    }).join("");

    return `
      <div class="qItem">
        <h4>${idx + 1}. ${f.finding_name}</h4>
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
  const patientId = getPatientIdFromUrl();
  if (!patientId) return alert("Missing patient id.");
  const data = await getJSON(`/api/patients/${patientId}/questionnaire?userId=${USER_ID}`);
  findingsData = data.findings;
  render(data);
  setupImageZoom();

  document.getElementById("backBtn").addEventListener("click", () => {
    window.location.href = "/history_list.html";
  });

  document.getElementById("saveBtn").addEventListener("click", async () => {

  const answers = [];

  for (const f of findingsData) {
    const name = `finding_${f.finding_id}`;
    const checked = document.querySelector(`input[name="${name}"]:checked`);

    if (checked) {
      answers.push({
        finding_id: f.finding_id,
        answer_choice: Number(checked.value)
      });
    } else {
      // 👉 AUTO NEGATIVE
      answers.push({
        finding_id: f.finding_id,
        answer_choice: 2
      });
    }
  }

  await putJSON(`/api/patients/${patientId}/answers?userId=${USER_ID}`, { answers });

  alert("Saved ✅");
});
})();

document.getElementById("logoutBtn")?.addEventListener("click", () => {
  Auth.logout();
});