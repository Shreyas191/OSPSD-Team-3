// Trello's return page (templates/trello_return.html). Trello sends its token in the URL
// fragment (#token=...), which no server sees. Move it into the page's form and post it, and
// clear the fragment from the address bar first. (The browser's history still lists the
// address Trello returned to: replaceState changes the current entry, not past visits.)
(function () {
  "use strict";
  var token = new URLSearchParams(window.location.hash.slice(1)).get("token") || "";
  window.history.replaceState(null, "", window.location.pathname + window.location.search);
  var form = document.getElementById("trello-return");
  if (!form) {
    return;
  }
  form.elements.namedItem("token").value = token;
  form.submit();
})();
