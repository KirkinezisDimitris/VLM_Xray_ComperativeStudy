const user = Auth.requireAuth();
if (!user) throw new Error("Unauthorized");
Auth.setupProfileMenu();
const USER_ID = user.id;

const ANSWERS = [
  { label: "POSITIVE", value: 1 },
  { label: "NEGATIVE", value: 2 },
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
        <h4>${f.finding_name}</h4>
        <div class="choices">${radios}</div>
      </div>
    `;
  }).join("");
}

/**
 * Collects all answers from the form.
 * Any finding without a selected radio defaults to NEGATIVE (2).
 * Never returns null — always returns a full array of 14 answers.
 */
function collectAllAnswers(findings) {
  return findings.map((f) => {
    const name = `finding_${f.finding_id}`;
    const checked = document.querySelector(`input[name="${name}"]:checked`);
    return {
      finding_id: f.finding_id,
      answer_choice: checked ? Number(checked.value) : 2, // default NEGATIVE
    };
  });
}

(async function boot() {
  // Load the current patient from server progress
  const current = await getJSON(`/api/current?userId=${USER_ID}`);
  if (current.done) {
    alert("Finished! No more patients.");
    window.location.href = "/patients.html";
    return;
  }

  const patientId = current.patient.patient_id;
  const data = await getJSON(`/api/patients/${patientId}/questionnaire?userId=${USER_ID}`);

  render(data, current);
  setupImageZoom();

  // Save: always sends all 14 findings, defaults unchecked to NEGATIVE
  document.getElementById("saveBtn")?.addEventListener("click", async () => {
    try {
      const answers = collectAllAnswers(data.findings);
      await putJSON(`/api/patients/${patientId}/answers?userId=${USER_ID}`, { answers });
      alert("Saved ✅");
    } catch (err) {
      console.error("Save error:", err);
      alert("Save failed ❌ — check console.");
    }
  });

  // Next: save all 14 answers (with NEGATIVE fallback), then advance the queue
  document.getElementById("nextBtn")?.addEventListener("click", async () => {
    try {
      const answers = collectAllAnswers(data.findings);

      // Always save before advancing — even if nothing was checked
      await putJSON(`/api/patients/${patientId}/answers?userId=${USER_ID}`, { answers });
      await postJSON(`/api/next?userId=${USER_ID}`);

      window.location.reload();
    } catch (err) {
      console.error("Next error:", err);
      alert("Failed to advance to next patient ❌ — check console.");
    }
  });

  document.getElementById("logoutBtn")?.addEventListener("click", () => {
    Auth.logout();
  });
})();