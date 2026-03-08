const user = Auth.requireAuth();
if (!user) throw new Error("Unauthorized");
Auth.setupProfileMenu();
const USER_ID = user.id;

// patient id που θέλουμε να δούμε
// μπορείς να το αλλάξεις δυναμικά αργότερα
const PATIENT_ID = 1;

async function loadChart() {

  const res = await fetch(`/api/patients/${PATIENT_ID}/questionnaire`);
  const data = await res.json();

  const findings = data.findings;

  let positive = 0;
  let negative = 0;
  let uncertain = 0;

  findings.forEach(f => {
    if (f.answer_choice === 1) positive++;
    if (f.answer_choice === 2) negative++;
    if (f.answer_choice === 3) uncertain++;
  });

  const ctx = document.getElementById("answersChart");

  new Chart(ctx, {
    type: "bar",
    data: {
      labels: ["Positive", "Negative", "Uncertain"],
      datasets: [{
        label: "Answers",
        data: [positive, negative, uncertain],
        backgroundColor: [
          "#22c55e",
          "#ef4444",
          "#f59e0b"
        ]
      }]
    },
    options: {
      responsive: true,
      plugins: {
        legend: { display: false }
      },
      scales: {
        y: {
          beginAtZero: true,
          ticks: {
            stepSize: 1
          }
        }
      }
    }
  });

}

loadChart();