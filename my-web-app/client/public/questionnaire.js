const user = Auth.requireAuth();
if (!user) throw new Error("Unauthorized");
Auth.setupProfileMenu();
const USER_ID = user.id;

const ANSWERS = [
  { label: "POSITIVE",  value: 1 },
  { label: "NEGATIVE",  value: 2 },
  { label: "UNCERTAIN", value: 3 },
];

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error("Request failed: " + url);
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

async function postJSON(url, body = {}) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error("POST failed: " + url);
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

function render(data, meta) {
  const { patient, findings } = data;

  document.getElementById("img1").src = patient.image1_path;
  document.getElementById("img2").src = patient.image2_path;
  document.getElementById("patientTitle").textContent =
    `Patient ${meta.current_pos + 1} / ${meta.total}`;

  const form = document.getElementById("form");
  form.innerHTML = findings.map((f) => {
    const group = `finding_${f.finding_id}`;
    // FIX: coerce σε Number ώστε "2" === 2 να δουλεύει σωστά
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
        <h4>${f.finding_name}</h4>
        <div class="choices">${radios}</div>
      </div>
    `;
  }).join("");
}

/* ── Συλλέγει όλα τα answers — αναπάντητα → NEGATIVE (2) ── */
function collectAllAnswers(findings) {
  return findings.map((f) => {
    const name    = `finding_${f.finding_id}`;
    const checked = document.querySelector(`input[name="${name}"]:checked`);
    return {
      finding_id:    f.finding_id,
      answer_choice: checked ? Number(checked.value) : 2,
    };
  });
}

(async function boot() {
  const current = await getJSON(`/api/current?userId=${USER_ID}`);
  if (current.done) {
    alert("Finished! No more patients.");
    window.location.href = "/patients.html";
    return;
  }

  const patientId = current.patient.patient_id;
  const data      = await getJSON(`/api/patients/${patientId}/questionnaire?userId=${USER_ID}`);

  render(data, current);
  setupImageZoom();

  /* ── Save: αποθηκεύει χωρίς popup, feedback στο button ── */
  const saveBtn = document.getElementById("saveBtn");
  saveBtn?.addEventListener("click", async () => {
    try {
      const answers = collectAllAnswers(data.findings);
      await putJSON(`/api/patients/${patientId}/answers?userId=${USER_ID}`, { answers });
      showBtnFeedback(saveBtn, "Saved ✅");
    } catch (err) {
      console.error("Save error:", err);
      showBtnFeedback(saveBtn, "Error ❌", 3000);
    }
  });

  /* ── Next Patient:
        1. Συλλέγει όλα τα answers (αναπάντητα → NEGATIVE)
        2. Αποθηκεύει στο DB
        3. Προχωράει στον επόμενο patient
  ── */
  const nextBtn = document.getElementById("nextBtn");
  nextBtn?.addEventListener("click", async () => {
    try {
      nextBtn.textContent = "Saving…";
      nextBtn.disabled    = true;

      const answers = collectAllAnswers(data.findings);
      await putJSON(`/api/patients/${patientId}/answers?userId=${USER_ID}`, { answers });
      await postJSON(`/api/next?userId=${USER_ID}`);

      window.location.reload();
    } catch (err) {
      console.error("Next error:", err);
      nextBtn.textContent = "Error ❌";
      setTimeout(() => {
        nextBtn.textContent = "Next Patient";
        nextBtn.disabled    = false;
      }, 3000);
    }
  });

  document.getElementById("logoutBtn")?.addEventListener("click", () => {
    Auth.logout();
  });
})();