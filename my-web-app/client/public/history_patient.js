let findingsData = [];

const user = Auth.requireAuth();
if (!user) throw new Error("Unauthorized");
Auth.setupProfileMenu();

const USER_ID = user.id;

const ANSWERS = [
  { label: "POSITIVE",  value: 1 },
  { label: "NEGATIVE",  value: 2 },
  { label: "UNCERTAIN", value: 3 },
];

function getPatientIdFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const id = Number(params.get("id"));
  return Number.isFinite(id) ? id : null;
}

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error("GET failed: " + url);
  return res.json();
}

async function putJSON(url, body) {
  const res = await fetch(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error("PUT failed: " + url);
  return res.json();
}

/* ── Visual feedback στο button αντί για alert popup ── */
function showBtnFeedback(btn, text, durationMs = 2000) {
  const original    = btn.textContent;
  btn.textContent   = text;
  btn.disabled      = true;
  btn.style.opacity = "0.7";
  setTimeout(() => {
    btn.textContent   = original;
    btn.disabled      = false;
    btn.style.opacity = "";
  }, durationMs);
}

/* ── Image zoom ── */
function setupImageZoom() {
  const modal    = document.getElementById("imgModal");
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

  document.getElementById("img1")?.addEventListener("click", (e) => open(e.target.src));
  document.getElementById("img2")?.addEventListener("click", (e) => open(e.target.src));
  closeBtn?.addEventListener("click", close);
  modal?.addEventListener("click", (e) => { if (e.target === modal || e.target === modalImg) close(); });
  window.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
}

/* ── Render form ──
   FIX: Number(f.answer_choice) ώστε "2" === 2 να δουλεύει σωστά
── */
function render({ patient, findings }) {
  document.getElementById("progressText").textContent =
    `Editing answers • ${findings.length} findings`;
  document.getElementById("img1").src = patient.image1_path;
  document.getElementById("img2").src = patient.image2_path;

  const form = document.getElementById("form");
  form.innerHTML = findings.map((f, idx) => {
    const group      = `finding_${f.finding_id}`;
    const savedValue = Number(f.answer_choice);

    const radios = ANSWERS.map(a => {
      const checked = (savedValue === a.value) ? "checked" : "";
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

/* ── Συλλέγει όλα τα answers — αναπάντητα → NEGATIVE (2) ── */
function collectAllAnswers() {
  return findingsData.map((f) => {
    const name    = `finding_${f.finding_id}`;
    const checked = document.querySelector(`input[name="${name}"]:checked`);
    return {
      finding_id:    f.finding_id,
      answer_choice: checked ? Number(checked.value) : 2,
    };
  });
}

/* ── Boot ── */
(async function boot() {
  const patientId = getPatientIdFromUrl();
  if (!patientId) return alert("Missing patient id.");

  try {
    const data = await getJSON(`/api/patients/${patientId}/questionnaire?userId=${USER_ID}`);
    findingsData = data.findings;
    render(data);
    setupImageZoom();
  } catch (err) {
    console.error("Load error:", err);
    return alert("Failed to load patient data ❌");
  }

  document.getElementById("backBtn")?.addEventListener("click", () => {
    window.location.href = "/history_list.html";
  });

  /* ── Save: αποθηκεύει χωρίς popup — feedback στο button ── */
  const saveBtn = document.getElementById("saveBtn");
  saveBtn?.addEventListener("click", async () => {
    try {
      const answers = collectAllAnswers();
      await putJSON(`/api/patients/${patientId}/answers?userId=${USER_ID}`, { answers });
      showBtnFeedback(saveBtn, "Saved ✅");
    } catch (err) {
      console.error("Save error:", err);
      showBtnFeedback(saveBtn, "Error ❌", 3000);
    }
  });
})();

document.getElementById("logoutBtn")?.addEventListener("click", () => {
  Auth.logout();
});