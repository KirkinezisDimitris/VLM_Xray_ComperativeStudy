let findingsData = [];

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
  const rawId = params.get("id");

  if (rawId === null) return null;

  const id = Number(rawId);
  return Number.isInteger(id) && id > 0 ? id : null;
}

async function getJSON(url) {
  const res = await fetch(url);

  if (!res.ok) {
    throw new Error(`GET failed: ${url} (${res.status})`);
  }

  return res.json();
}

async function putJSON(url, body) {
  const res = await fetch(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (!res.ok) {
    throw new Error(`PUT failed: ${url} (${res.status})`);
  }

  const text = await res.text();
  return text ? JSON.parse(text) : null;
}

function showBtnFeedback(btn, text, durationMs = 2000) {
  if (!btn) return;

  const original = btn.textContent;
  btn.textContent = text;
  btn.disabled = true;
  btn.style.opacity = "0.7";

  setTimeout(() => {
    btn.textContent = original;
    btn.disabled = false;
    btn.style.opacity = "";
  }, durationMs);
}

function setupImageZoom() {
  const modal = document.getElementById("imgModal");
  const modalImg = document.getElementById("imgModalContent");
  const closeBtn = document.getElementById("imgModalClose");
  const img1 = document.getElementById("img1");
  const img2 = document.getElementById("img2");

  if (!modal || !modalImg) return;

  function open(src) {
    if (!src) return;
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

  img1?.addEventListener("click", (e) => open(e.target.src));
  img2?.addEventListener("click", (e) => open(e.target.src));
  closeBtn?.addEventListener("click", close);

  modal.addEventListener("click", (e) => {
    if (e.target === modal || e.target === modalImg) {
      close();
    }
  });

  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      close();
    }
  });
}

function render({ patient, findings }) {
  const progressText = document.getElementById("progressText");
  const img1 = document.getElementById("img1");
  const img2 = document.getElementById("img2");
  const form = document.getElementById("form");

  if (!progressText || !img1 || !img2 || !form) {
    throw new Error("Required DOM elements are missing.");
  }

  progressText.textContent = `Editing answers • ${findings.length} findings`;

  img1.src = patient.image1_path || "";
  img2.src = patient.image2_path || "";

  form.innerHTML = findings
    .map((f, idx) => {
      const group = `finding_${f.finding_id}`;
      const savedValue = Number(f.answer_choice);

      const radios = ANSWERS.map((a) => {
        const checked = savedValue === a.value ? "checked" : "";
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
    })
    .join("");
}

function collectAllAnswers() {
  return findingsData.map((f) => {
    const name = `finding_${f.finding_id}`;
    const checked = document.querySelector(`input[name="${name}"]:checked`);

    return {
      finding_id: f.finding_id,
      answer_choice: checked ? Number(checked.value) : 2,
    };
  });
}

(async function boot() {
  const patientId = getPatientIdFromUrl();

  if (!patientId) {
    alert("Missing or invalid patient id.");
    return;
  }

  try {
    const data = await getJSON(
      `/api/patients/${patientId}/questionnaire?userId=${USER_ID}`
    );

    findingsData = Array.isArray(data.findings) ? data.findings : [];
    render(data);
    setupImageZoom();
  } catch (err) {
    console.error("Load error:", err);
    alert("Failed to load patient data ❌");
    return;
  }

  const backBtn = document.getElementById("backBtn");
  backBtn?.addEventListener("click", () => {
    window.location.href = "/history_list.html";
  });

  const saveBtn = document.getElementById("saveBtn");

  if (saveBtn) {
    saveBtn.addEventListener("click", async () => {
      try {
        saveBtn.textContent = "Saving…";
        saveBtn.disabled = true;

        const answers = collectAllAnswers();

        await putJSON(
          `/api/patients/${patientId}/answers?userId=${USER_ID}`,
          { answers }
        );

        window.location.reload();
      } catch (err) {
        console.error("Save error:", err);
        saveBtn.textContent = "Error ❌";

        setTimeout(() => {
          saveBtn.textContent = "Save Changes";
          saveBtn.disabled = false;
        }, 3000);
      }
    });
  }
})();

document.getElementById("logoutBtn")?.addEventListener("click", () => {
  Auth.logout();
});