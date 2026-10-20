// AI artifacts: copying / downloading the bid draft (web-ux-3).
// Delegation on the document — works also for the HTMX fragments substituted
// after the generation (the buttons appear dynamically). No inline logic
// (CSP-compatible, as cpv_widget.js / confirm_dialogs.js).
(function () {
  "use strict";

  function srcText(btn) {
    var box = btn.closest("[data-artifact]");
    if (!box) return "";
    var src = box.querySelector("[data-artifact-src]");
    return src ? src.value : "";
  }

  function flash(btn, label) {
    var prev = btn.textContent;
    btn.textContent = label;
    btn.disabled = true;
    setTimeout(function () {
      btn.textContent = prev;
      btn.disabled = false;
    }, 1600);
  }

  document.addEventListener("click", function (ev) {
    var copyBtn = ev.target.closest("[data-copy]");
    if (copyBtn) {
      var text = srcText(copyBtn);
      if (!text) return;
      var done = function () {
        flash(copyBtn, "✓ Скопировано");
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () {
          legacyCopy(text);
          done();
        });
      } else {
        legacyCopy(text);
        done();
      }
      return;
    }

    var dlBtn = ev.target.closest("[data-download]");
    if (dlBtn) {
      var body = srcText(dlBtn);
      if (!body) return;
      var blob = new Blob([body], { type: "text/plain;charset=utf-8" });
      var url = URL.createObjectURL(blob);
      var a = document.createElement("a");
      a.href = url;
      a.download = dlBtn.getAttribute("data-download") || "artifact.txt";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(function () {
        URL.revokeObjectURL(url);
      }, 0);
    }
  });

  function legacyCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand("copy");
    } catch (e) {
      /* no clipboard — quietly ignore */
    }
    document.body.removeChild(ta);
  }
})();
