/*
 * Two small enhancements for the auth screens.
 *
 *   name field ──▶ slugify() ──▶ live "Your workspace: …" preview
 *   password    ──▶ reveal toggle
 *
 * PROGRESSIVE ENHANCEMENT, NOT A DEPENDENCY. Both pages submit and work
 * perfectly with this file blocked, failed or still downloading — the server
 * decides the real slug either way, and a password field is a password field.
 * That matters on a patchy connection, which is the normal case here.
 *
 * No framework, no build, ~1KB. Loaded `defer`, so it never blocks paint.
 */

(function () {
  "use strict";

  /*
   * MUST MATCH `slugify()` IN app/services/accounts.py.
   *
   * If the two drift, the preview lies — a creator sees one address and gets
   * another, which is a small betrayal at exactly the moment they are deciding
   * whether to trust us. Kept deliberately literal, step for step, so the two
   * can be read side by side.
   *
   * The preview is still only a preview: the server appends -2, -3 … on
   * collision, and it alone knows what is taken.
   */
  function slugify(value) {
    var ascii = value
      .normalize("NFKD")                 // "Café" ──> "Cafe" + combining accent
      .replace(/[\u0300-\u036f]/g, "")   // drop the accent, keep the letter
      .replace(/[^\x00-\x7F]/g, "");     // anything still non-ASCII goes

    return ascii
      .replace(/[^a-zA-Z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .toLowerCase()
      .slice(0, 40)               // MAX_SLUG_LENGTH
      .replace(/^-+|-+$/g, "");   // the slice can leave a trailing dash
  }

  function wireSlugPreview() {
    var source = document.querySelector("[data-slug-source]");
    var preview = document.querySelector("[data-slug-preview]");
    if (!source || !preview) return;

    var placeholder = preview.textContent;

    function update() {
      var slug = slugify(source.value);
      preview.textContent = slug || placeholder;
    }

    source.addEventListener("input", update);
    update();  // the browser may have restored a value on back-navigation
  }

  function wirePasswordToggle() {
    var toggles = document.querySelectorAll("[data-password-toggle]");

    Array.prototype.forEach.call(toggles, function (button) {
      var input = button.parentNode.querySelector("[data-password]");
      if (!input) return;

      button.addEventListener("click", function () {
        var revealed = input.type === "text";
        input.type = revealed ? "password" : "text";

        // Both attributes matter: aria-pressed is the state, aria-label is what
        // a screen reader announces. Updating only one leaves the button
        // describing the opposite of what it now does.
        button.setAttribute("aria-pressed", String(!revealed));
        button.setAttribute("aria-label", revealed ? "Show password" : "Hide password");

        // Returning focus to the field means the reveal does not cost the
        // user their place in the form.
        input.focus();
      });
    });
  }

  /*
   * Copy the verification code.
   *
   * The fallback matters more than the happy path. `navigator.clipboard` needs
   * a secure context, and a seller on http (or an older Android WebView) gets
   * nothing — so a failure re-selects the code instead, leaving them one long
   * press from copying it manually. The code is `user-select: all`, so that
   * selection is the whole code every time.
   */
  function wireCopy() {
    var button = document.querySelector("[data-copy-button]");
    var value = document.querySelector("[data-copy-value]");
    if (!button || !value) return;

    var label = button.querySelector("[data-copy-label]");
    var original = label ? label.textContent : "";

    function confirm(text) {
      if (!label) return;
      label.textContent = text;
      setTimeout(function () { label.textContent = original; }, 2000);
    }

    function selectCode() {
      var range = document.createRange();
      range.selectNodeContents(value);
      var selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    }

    button.addEventListener("click", function () {
      var text = value.textContent.trim();

      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(
          function () { confirm("Copied"); },
          function () { selectCode(); confirm("Press and hold"); }
        );
      } else {
        selectCode();
        confirm("Press and hold");
      }
    });
  }

  /*
   * Count the verify button back in.
   *
   * The cooldown is enforced by the server — see CHECK_COOLDOWN in
   * services/verification.py — because a disabled button is a suggestion, not
   * a guard. This only makes the wait legible, so it does not read as a dead
   * control. If this script never runs, the button stays disabled and the page
   * still says how long is left in its own label.
   */
  function wireCooldown() {
    var button = document.querySelector("[data-cooldown]");
    if (!button) return;

    var label = button.querySelector("[data-cooldown-label]");
    var left = parseInt(button.getAttribute("data-cooldown"), 10);
    if (!left || left < 1) return;

    var tick = setInterval(function () {
      left -= 1;
      if (left > 0) {
        if (label) label.textContent = "Try again in " + left + "s";
        return;
      }
      clearInterval(tick);
      button.disabled = false;
      if (label) label.textContent = "I’ve added it — check now";
    }, 1000);
  }

  wireSlugPreview();
  wirePasswordToggle();
  wireCopy();
  wireCooldown();
})();
