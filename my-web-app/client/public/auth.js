const AUTH_KEY = "mr_user";

function getCurrentUser() {
  return JSON.parse(sessionStorage.getItem(AUTH_KEY) || "null");
}

function requireAuth() {
  const user = getCurrentUser();
  if (!user) {
    window.location.href = "/login.html";
    return null;
  }
  return user;
}

function logout() {
  sessionStorage.removeItem(AUTH_KEY);
  window.location.href = "/login.html";
}

function setupProfileMenu() {
  const profileBtn = document.getElementById("profileBtn");
  const profileDropdown = document.getElementById("profileDropdown");
  const logoutBtn = document.getElementById("logoutBtn");

  if (!profileBtn || !profileDropdown) return;

  profileBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    profileDropdown.classList.toggle("active");
  });

  logoutBtn?.addEventListener("click", () => {
    logout();
  });

  document.addEventListener("click", (e) => {
    if (
      !profileBtn.contains(e.target) &&
      !profileDropdown.contains(e.target)
    ) {
      profileDropdown.classList.remove("active");
    }
  });
}

window.Auth = {
  AUTH_KEY,
  getCurrentUser,
  requireAuth,
  logout,
  setupProfileMenu,
};