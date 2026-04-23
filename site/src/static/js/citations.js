/* Meridian citation copy buttons.
 *
 * Progressive enhancement: citation blocks are fully visible without JS.
 * If JS is enabled, a "Copy" button appears next to each <pre>/<p>
 * inside a .citation section.
 */
(function () {
  "use strict";
  if (!navigator.clipboard) return;

  var entries = document.querySelectorAll(".citation .citation-entry");
  for (var i = 0; i < entries.length; i++) {
    attach(entries[i]);
  }

  function attach(entry) {
    var detailsEl = entry.querySelector("details");
    if (!detailsEl) return;
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

    entry.appendChild(btn);
  }
})();
