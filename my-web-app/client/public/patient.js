const user = Auth.requireAuth();
if (!user) throw new Error("Unauthorized");

Auth.setupProfileMenu();

const USER_ID = user.id;
document.getElementById("continueBtn").addEventListener("click", () => {
  const params = new URLSearchParams(window.location.search);
  const id = params.get("id");
  window.location.href = `questionnaire.html?id=${id}`;
});
document.getElementById("historyBtn")?.addEventListener("click", () => {
  const params = new URLSearchParams(window.location.search);
  const id = params.get("id");
  window.location.href = `history_list.html?id=${id}`;
});

document.getElementById("logoutBtn")?.addEventListener("click", () => {
  Auth.logout();
});