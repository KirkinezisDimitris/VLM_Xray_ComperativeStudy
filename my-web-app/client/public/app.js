/* =========================
   Demo data (αργότερα το αντικαθιστάς με fetch από backend)
   ========================= */
const DEMO_PATIENTS = [
  { id: 1, patient_code: "P001", image1_path: "uploads/P001_1.jpg", image2_path: "uploads/P001_2.jpg" },
  { id: 2, patient_code: "P002", image1_path: "uploads/P002_1.jpg", image2_path: "uploads/P002_2.jpg" },
  { id: 3, patient_code: "P003", image1_path: "uploads/P003_1.jpg", image2_path: "uploads/P003_2.jpg" },
  // ... μέχρι 300
];

const FINDINGS = [
  "No Finding",
  "Enlarged Cardiomediastinum",
  "Cardiomegaly",
  "Lung Opacity",
  "Lung Lesion",
  "Edema",
  "Consolidation",
  "Pneumonia",
  "Atelectasis",
  "Pneumothorax",
  "Pleural Effusion",
  "Pleural Other",
  "Fracture",
  "Support Devices",
];

// answer_choice mapping:
const ANSWERS = [
  { label: "POSITIVE", value: 1 },
  { label: "NEGATIVE", value: 2 },
  { label: "UNCERTAIN", value: 3 },
];

/* =========================
   Keys (per user)
   Αν έχεις login, κάνε το USER_KEY = username
   ========================= */
const USER_KEY = "default_user"; // TODO: βάλε εδώ το username όταν κάνεις login
const LS_QUEUE = `mr_queue_${USER_KEY}`;
const LS_INDEX = `mr_index_${USER_KEY}`;
const LS_QINDEX = `mr_qindex_${USER_KEY}`; // σε ποιο finding έχει μείνει ο γιατρός
const LS_CURRENT_PATIENT = `mr_current_patient_${USER_KEY}`;
const LS_ANSWERS_PREFIX = `mr_answers_${USER_KEY}_patient_`;

/* =========================
   Utils
   ========================= */
function shuffle(array) {
  const a = array.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

function getQueue() {
  const raw = localStorage.getItem(LS_QUEUE);
  return raw ? JSON.parse(raw) : null;
}

function setQueue(q) {
  localStorage.setItem(LS_QUEUE, JSON.stringify(q));
}

function getIndex() {
  const raw = localStorage.getItem(LS_INDEX);
  return raw ? parseInt(raw, 10) : 0;
}

function setIndex(i) {
  localStorage.setItem(LS_INDEX, String(i));
}

function getQuestionIndex() {
  const raw = localStorage.getItem(LS_QINDEX);
  return raw ? parseInt(raw, 10) : 0;
}

function setQuestionIndex(i) {
  localStorage.setItem(LS_QINDEX, String(i));
}

function setCurrentPatientId(id) {
  localStorage.setItem(LS_CURRENT_PATIENT, String(id));
}

function getCurrentPatientId() {
  const raw = localStorage.getItem(LS_CURRENT_PATIENT);
  return raw ? parseInt(raw, 10) : null;
}

function getPatientById(id) {
  return DEMO_PATIENTS.find(p => p.id === id);
}

/* =========================
   Init queue once per user
   ========================= */
function ensureQueueInitialized() {
  let q = getQueue();
  if (!q || !Array.isArray(q) || q.length !== DEMO_PATIENTS.length) {
    // create new randomized queue of patient IDs
    const ids = DEMO_PATIENTS.map(p => p.id);
    q = shuffle(ids);
    setQueue(q);
    setIndex(0);
    setQuestionIndex(0);
    setCurrentPatientId(q[0] ?? null);
  }
  return q;
}

/* =========================
   Patients page: open next patient (random but only once)
   ========================= */
function goNextPatient() {
  const q = ensureQueueInitialized();
  const idx = getIndex();

  if (idx >= q.length) {
    alert("Finished! No more patients.");
    return;
  }

  const nextId = q[idx];
  setCurrentPatientId(nextId);
  // go to patient.html?id=...
  window.location.href = `patient.html?id=${nextId}`;
}

/* =========================
   Patient page: Continue -> questionnaire
   ========================= */
function wirePatientContinue() {
  const params = new URLSearchParams(window.location.search);
  const id = parseInt(params.get("id"), 10);
  if (!id) return;

  setCurrentPatientId(id);

  const continueBtn = document.getElementById("continueBtn");
  if (continueBtn) {
    continueBtn.addEventListener("click", () => {
      // go to questionnaire
      window.location.href = `questionnaire.html?id=${id}`;
    });
  }
}

/* =========================
   Questionnaire page rendering
   ========================= */
function loadSavedAnswers(patientId) {
  const key = `${LS_ANSWERS_PREFIX}${patientId}`;
  const raw = localStorage.getItem(key);
  return raw ? JSON.parse(raw) : {}; // { findingIndex: answerChoice }
}

function saveAnswers(patientId, answersObj) {
  const key = `${LS_ANSWERS_PREFIX}${patientId}`;
  localStorage.setItem(key, JSON.stringify(answersObj));
}

function renderQuestionnaire() {
  const params = new URLSearchParams(window.location.search);
  const id = parseInt(params.get("id"), 10) || getCurrentPatientId();
  if (!id) return;

  const patient = getPatientById(id);
  if (!patient) {
    alert("Patient not found (demo).");
    return;
  }

  setCurrentPatientId(id);

  const patientTitle = document.getElementById("patientTitle");
  const progressText = document.getElementById("progressText");
  const img1 = document.getElementById("img1");
  const img2 = document.getElementById("img2");
  const form = document.getElementById("form");

  // progress out of 300 (or whatever)
  const q = ensureQueueInitialized();
  const idx = getIndex(); // current patient position in queue
  const total = q.length;
  const qidx = getQuestionIndex(); // current finding index 0..13
  if (patientTitle) patientTitle.textContent = `Patient ${patient.patient_code}`;
  if (progressText) progressText.textContent = `Patient ${Math.min(idx + 1, total)} / ${total} • Question ${Math.min(qidx + 1, 14)} / 14`;

  // images
  if (img1) img1.src = patient.image1_path;
  if (img2) img2.src = patient.image2_path;

  // build form
  const saved = loadSavedAnswers(id);

  form.innerHTML = FINDINGS.map((name, i) => {
    const groupName = `f_${i}`; // 0..13
    const savedValue = saved[i] ?? null;

    const radios = ANSWERS.map(a => {
      const checked = (savedValue === a.value) ? "checked" : "";
      // required only on first radio of the group to enforce selection
      return `
        <label class="choice">
          <input type="radio" name="${groupName}" value="${a.value}" ${checked} ${required} />
          ${a.label}
        </label>
      `;
    }).join("");

    return `
      <div class="qItem" data-qindex="${i}">
        <h4>${name}</h4>
        <div class="choices">${radios}</div>
      </div>
    `;
  }).join("");

  // restore scroll to where doctor left off (question index)
  requestAnimationFrame(() => {
    const qidx2 = getQuestionIndex();
    const el = form.querySelector(`.qItem[data-qindex="${qidx2}"]`);
    if (el) el.scrollIntoView({ behavior: "instant", block: "center" });
  });

  // track which question doctor is on (whenever selects something)
  form.addEventListener("change", (e) => {
    const input = e.target;
    if (input && input.name && input.name.startsWith("f_")) {
      const qIndex = parseInt(input.name.replace("f_", ""), 10);
      if (!Number.isNaN(qIndex)) setQuestionIndex(qIndex);
    }
  });

  // Save button: store locally (later you replace with API save -> MySQL)
  const saveBtn = document.getElementById("saveBtn");
  saveBtn?.addEventListener("click", () => {
    const answers = collectAnswersFromForm(form);
    saveAnswers(id, answers);
    alert("Saved (local).");
  });

  // Next patient: validate, save, then move to next in queue
  const nextBtn = document.getElementById("nextBtn");
  nextBtn?.addEventListener("click", () => {
    if (!form.reportValidity()) {
      // browser will highlight missing answers
      return;
    }
    const answers = collectAnswersFromForm(form);
    saveAnswers(id, answers);

    // move queue forward: patient appears only once
    const newIndex = getIndex() + 1;
    setIndex(newIndex);
    setQuestionIndex(0);

    if (newIndex >= q.length) {
      alert("Finished! No more patients.");
      window.location.href = "patients.html";
      return;
    }

    const nextId = q[newIndex];
    setCurrentPatientId(nextId);
    window.location.href = `patient.html?id=${nextId}`;
  });
}

function collectAnswersFromForm(form) {
  const data = new FormData(form);
  const answers = {};
  for (const [key, value] of data.entries()) {
    // key is f_0..f_13
    const idx = parseInt(key.replace("f_", ""), 10);
    answers[idx] = parseInt(value, 10); // 1/2/3
  }
  return answers;
}

/* =========================
   Profile dropdown (if present)
   ========================= */
function wireProfileDropdown() {
  const profileBtn = document.getElementById("profileBtn");
  const profileDropdown = document.getElementById("profileDropdown");
  const logoutBtn = document.getElementById("logoutBtn");

  profileBtn?.addEventListener("click", () => {
    profileDropdown.classList.toggle("active");
  });

  window.addEventListener("click", (e) => {
    if (!profileBtn?.contains(e.target) && !profileDropdown?.contains(e.target)) {
      profileDropdown?.classList.remove("active");
    }
  });

  logoutBtn?.addEventListener("click", () => {
    alert("Logout (Demo)");
    window.location.href = "index.html";
  });
}

/* =========================
   Boot per page
   ========================= */
(function boot() {
  wireProfileDropdown();

  const path = window.location.pathname.toLowerCase();

  // patients.html: auto-show next patient button
  if (path.endsWith("/patients.html") || path.endsWith("patients.html")) {
    // If you want: auto redirect to next patient immediately:
    // goNextPatient();

    // Or bind a button with id="nextPatientBtn" (recommended)
    const btn = document.getElementById("nextPatientBtn");
    if (btn) btn.addEventListener("click", goNextPatient);
  }

  // patient.html: wire continue
  if (path.endsWith("/patient.html") || path.endsWith("patient.html")) {
    wirePatientContinue();
  }

  // questionnaire.html: render form
  if (path.endsWith("/questionnaire.html") || path.endsWith("questionnaire.html")) {
    renderQuestionnaire();
  }
})();