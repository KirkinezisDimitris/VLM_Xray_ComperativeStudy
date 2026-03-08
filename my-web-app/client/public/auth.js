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

window.Auth = {
  AUTH_KEY,
  getCurrentUser,
  requireAuth,
  logout
};