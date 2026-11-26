// Adds the CSRF token to every htmx request. hx-headers cannot read a <meta>
// without eval, which is off, and an inline script is refused by the CSP.
document.addEventListener("htmx:configRequest", function (event) {
  var token = document.querySelector('meta[name="csrf-token"]');
  if (token) {
    event.detail.headers["X-CSRF-Token"] = token.content;
  }
});
