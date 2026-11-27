// Long requests (a conversation turn, making approved changes) are ordinary form posts that
// take up to a minute or more. While one runs, say so, and stop a second click from sending
// the form again. Without JavaScript the forms work the same, just without the message.
(function () {
  "use strict";
  document.querySelectorAll("form[data-busy]").forEach(function (form) {
    var sent = false;
    form.addEventListener("submit", function (event) {
      if (sent) {
        event.preventDefault();
        return;
      }
      sent = true;
      var status = form.querySelector(".busy");
      if (status) {
        status.textContent = form.getAttribute("data-busy");
      }
      form.querySelectorAll("button").forEach(function (button) {
        button.setAttribute("aria-disabled", "true");
      });
    });
  });
})();
