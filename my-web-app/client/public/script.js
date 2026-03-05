// =======================
// Navigation
// =======================

function goToPatient(id) {
  window.location.href = `patient.html?id=${id}`;
}

// =======================
// Patient Page Loader
// =======================

const params = new URLSearchParams(window.location.search);
const patientId = params.get("id");

const patients = {
  p1: { name: "Γεώργιος Παπαδόπουλος", age: 52 },
  p2: { name: "Μαρία Κωνσταντίνου", age: 34 },
  p3: { name: "Ιωάννης Δημητρίου", age: 68 },
};

if (patientId && patients[patientId]) {
  const nameEl = document.getElementById("patientName");
  const idEl = document.getElementById("patientId");

  if (nameEl) {
    nameEl.textContent = patients[patientId].name;
  }

  if (idEl) {
    idEl.textContent = `Patient ID: ${patientId}`;
  }
}

// =======================
// Profile Dropdown
// =======================

const profileBtn = document.getElementById("profileBtn");
const profileDropdown = document.getElementById("profileDropdown");
document.getElementById("logoutBtn")?.addEventListener("click", () => {
  localStorage.removeItem("mr_user");
  window.location.href = "/login.html";
});

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