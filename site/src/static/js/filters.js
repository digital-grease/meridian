/* Drift Audit table filter chips.
 *
 * Progressive enhancement: attaches to any <table data-filter-col="N">
 * and injects chips that toggle the `hidden` attribute on rows whose
 * Nth column text matches/doesn't-match the chip value. Without JS the
 * table is fully visible, unfiltered.
 */
(function () {
  "use strict";

  var tables = document.querySelectorAll("table[data-filter-col]");
  for (var t = 0; t < tables.length; t++) buildChips(tables[t]);

  function buildChips(table) {
    var colIdx = parseInt(table.getAttribute("data-filter-col"), 10);
    if (isNaN(colIdx)) return;
    var rows = table.tBodies[0] ? table.tBodies[0].rows : [];
    var values = {};
    for (var i = 0; i < rows.length; i++) {
      var cell = rows[i].cells[colIdx];
      if (!cell) continue;
      var v = cell.textContent.trim();
      values[v] = true;
    }
    var bar = document.createElement("div");
    bar.className = "filter-chips";
    bar.setAttribute("role", "toolbar");
    bar.setAttribute("aria-label", "Filter table rows");

    var allBtn = makeChip("All", true, apply);
    bar.appendChild(allBtn);
    Object.keys(values).sort().forEach(function (v) {
      bar.appendChild(makeChip(v, false, apply));
    });
    table.parentNode.insertBefore(bar, table);

    function apply(activeValue) {
      var chips = bar.querySelectorAll(".chip");
      for (var c = 0; c < chips.length; c++) {
        chips[c].setAttribute(
          "aria-pressed",
          chips[c].dataset.value === activeValue ? "true" : "false",
        );
      }
      for (var r = 0; r < rows.length; r++) {
        var cell = rows[r].cells[colIdx];
        if (!cell) continue;
        var match = activeValue === "All" || cell.textContent.trim() === activeValue;
        rows[r].hidden = !match;
      }
    }
  }

  function makeChip(value, active, cb) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "chip";
    b.dataset.value = value;
    b.textContent = value;
    b.setAttribute("aria-pressed", active ? "true" : "false");
    b.addEventListener("click", function () { cb(value); });
    return b;
  }
})();
