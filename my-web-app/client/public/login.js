async function postJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify(body)
  });

  const text = await res.text();

  if(!res.ok) throw new Error(text || "Login failed");

  return JSON.parse(text);
}

document.getElementById("loginForm").addEventListener("submit", async (e)=>{
  e.preventDefault();

  const username = document.getElementById("username").value.trim();
  const password = document.getElementById("password").value;

  try {

    const data = await postJSON("/api/login",{username,password});

    sessionStorage.setItem(Auth.AUTH_KEY, JSON.stringify(data.user));

    window.location.href="/index.html";

  } catch(err){
    document.getElementById("error").textContent=err.message;
  }
});

function togglePassword(){

  const passwordInput = document.getElementById("password");

  if(passwordInput.type === "password"){
    passwordInput.type = "text";
  }else{
    passwordInput.type = "password";
  }

}