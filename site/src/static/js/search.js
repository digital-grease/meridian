/* Meridian client-side search.
 *
 * Progressive enhancement: the page works without this script. If JS is
 * enabled, an input appears and searches the prebuilt index under
 * /static/search-index.json with naive substring / word-prefix matching.
 */
(function () {
  "use strict";

  var container = document.getElementById("site-search");
  if (!container) return;

  var input = document.createElement("input");
  input.type = "search";
  input.placeholder = "Search prompts, models, reports\u2026";
  input.setAttribute("aria-label", "Search");
  input.autocomplete = "off";

  var results = document.createElement("ul");
  results.className = "search-results";
  results.setAttribute("aria-live", "polite");

  container.appendChild(input);
  container.appendChild(results);

  var index = null;
  var indexLoadPromise = null;

  function loadIndex() {
    if (indexLoadPromise) return indexLoadPromise;
    indexLoadPromise = fetch("/static/search-index.json")
      .then(function (r) {
        if (!r.ok) throw new Error("index load failed: " + r.status);
        return r.json();
      })
      .then(function (data) { index = data; return data; })
      .catch(function (err) {
        console.error("[meridian search]", err);
        results.textContent = "Search unavailable.";
      });
    return indexLoadPromise;
  }

  function search(query) {
    if (!index) return [];
    var q = query.trim().toLowerCase();
    if (q.length < 2) return [];
    var tokens = q.split(/\s+/).filter(Boolean);
    var out = [];
    for (var i = 0; i < index.length && out.length < 20; i++) {
      var rec = index[i];
      var hay = (rec.title + " " + rec.text).toLowerCase();
      var ok = true;
      for (var t = 0; t < tokens.length; t++) {
        if (hay.indexOf(tokens[t]) < 0) { ok = false; break; }
      }
      if (ok) out.push(rec);
    }
    return out;
  }

  function render(list, query) {
    results.innerHTML = "";
    if (!query || query.trim().length < 2) return;
    if (list.length === 0) {
      var li = document.createElement("li");
      li.className = "muted";
      li.textContent = "No matches.";
      results.appendChild(li);
      return;
    }
    list.forEach(function (rec) {
      var li = document.createElement("li");
      var a = document.createElement("a");
      a.href = rec.url;
      a.textContent = rec.title;
      var tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = rec.kind;
      li.appendChild(a);
      li.appendChild(document.createTextNode(" "));
      li.appendChild(tag);
      results.appendChild(li);
    });
  }

  input.addEventListener("focus", loadIndex, { once: true });

  var debounce;
  input.addEventListener("input", function () {
    clearTimeout(debounce);
    debounce = setTimeout(function () {
      loadIndex().then(function () {
        render(search(input.value), input.value);
      });
    }, 80);
  });
})();
