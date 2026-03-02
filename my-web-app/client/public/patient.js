document.getElementById("continueBtn").addEventListener("click", () => {
  const params = new URLSearchParams(window.location.search);
  const id = params.get("id");
  window.location.href = `questionnaire.html?id=${id}`;
});