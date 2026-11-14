(() => {
  "use strict";
  // The public demo credentials arrive prefilled from the server. This button only clears them
  // so the administrator can type the separate secret credentials.
  const button = document.getElementById("login-admin");
  const user = document.getElementById("login-username");
  const pass = document.getElementById("login-password");
  if (!button || !user || !pass) {
    return;
  }
  button.hidden = false;
  button.addEventListener("click", () => {
    user.value = "";
    pass.value = "";
    user.focus();
  });
})();
