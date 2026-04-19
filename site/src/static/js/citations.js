/* Drift Audit citation copy buttons.
 *
 * Progressive enhancement: citation blocks are fully visible without JS.
 * If JS is enabled, a "Copy" button appears next to each <pre>/<p>
 * inside a .citation section.
 */
(function () {
  "use strict";
  if (!navigator.clipboard) return;

  var sections = document.querySelectorAll(".citation");
  for (var s = 0; s < sections.length; s++) {
    var blocks = sections[s].querySelectorAll("details");
    for (var i = 0; i < blocks.length; i++) {
      attach(blocks[i]);
    }
  }

  function attach(detailsEl) {
    var content = detailsEl.querySelector("pre, p");
    if (!content) return;

    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "copy-btn";
    btn.textContent = "Copy";
    btn.setAttribute("aria-label", "Copy citation");

    btn.addEventListener("click", function () {
      var text = content.innerText.trim();
      navigator.clipboard
        .writeText(text)
        .then(function () {
          var original = btn.textContent;
          btn.textContent = "Copied";
          setTimeout(function () { btn.textContent = original; }, 1200);
        })
        .catch(function () { btn.textContent = "Copy failed"; });
    });

    var summary = detailsEl.querySelector("summary");
    if (summary) {
      summary.appendChild(document.createTextNode(" "));
      summary.appendChild(btn);
    }
  }
})();
