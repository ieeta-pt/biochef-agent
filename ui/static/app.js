// Adds the CSRF token to every htmx request. hx-headers cannot read a <meta>
// without eval, which is off, and an inline script is refused by the CSP.
//
// The live log polls only while it can be seen. A trigger filter would do this,
// and filters need eval.
document.addEventListener("htmx:beforeRequest", function (event) {
  if (document.hidden && event.target.hasAttribute("data-live")) {
    event.preventDefault();
  }
});

document.addEventListener("htmx:configRequest", function (event) {
  var token = document.querySelector('meta[name="csrf-token"]');
  if (token) {
    event.detail.headers["X-CSRF-Token"] = token.content;
  }
});
