// Trello's return page (templates/trello_return.html). Trello sends its token in the URL
// fragment (#token=...), which no server sees. Move it into the page's form and post it, and
// clear the fragment from the address bar first. (The browser's history still lists the
// address Trello returned to: replaceState changes the current entry, not past visits. That's
// why the API asks Trello for a token that expires after 30 days; DESIGN.md, T10.)
//
// No token means there is nothing to finish: the page was reloaded, for example by Back after
// connecting, or the person declined (Trello then sends an empty token). Posting an empty token
// would report a failure for a connection that may have worked, so say where to look instead.
(function () {
  "use strict";
  var token = new URLSearchParams(window.location.hash.slice(1)).get("token") || "";
  window.history.replaceState(null, "", window.location.pathname + window.location.search);
  var form = document.getElementById("trello-return");
  if (!form) {
    return;
  }
  if (!token) {
    document.getElementById("trello-heading").textContent = "Trello wasn't connected from here";
    document.getElementById("trello-nothing").hidden = false;
    return;
  }
  form.elements.namedItem("token").value = token;
  form.submit();
})();
