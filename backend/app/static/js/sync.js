/*
 * Watch a running sync, and reload the page when it finishes.
 *
 *   [data-watch-job="12"] ──▶ poll /jobs/12/status ──▶ finished? reload
 *
 * WHY NOT HTMX. This is the only dynamic thing in the workspace, and it is
 * twenty lines. Pulling in a library to avoid them would cost every page a
 * download for one feature — and the users are on cheap Android over expensive
 * data.
 *
 * PROGRESSIVE ENHANCEMENT. With this file blocked or still downloading the
 * page is still correct: it says "Syncing…", and refreshing shows the result.
 * All this removes is the need to refresh by hand.
 */

(function () {
  "use strict";

  var banner = document.querySelector("[data-watch-job]");
  if (!banner) return;

  var jobId = banner.getAttribute("data-watch-job");
  var message = banner.querySelector("[data-job-message]");

  /*
   * Two seconds. A scrape takes 30s–3min, so this is roughly ninety requests
   * at the far end — trivial, and it makes the finish feel immediate.
   */
  var INTERVAL = 2000;

  /*
   * Stop after ten minutes. The worker's own stale-job reclaimer gives up at
   * thirty, but a browser tab left open overnight polling a dead job is a
   * request every two seconds forever.
   */
  var DEADLINE = Date.now() + 10 * 60 * 1000;

  function say(text) {
    if (message) message.textContent = text;
  }

  function poll() {
    if (Date.now() > DEADLINE) {
      say("This is taking longer than expected. Refresh to check.");
      return;
    }

    fetch("/jobs/" + jobId + "/status", { headers: { Accept: "application/json" } })
      .then(function (response) {
        // 404 means the session ended or the job is not ours. Stop rather than
        // hammering an endpoint that will never say yes.
        if (!response.ok) throw new Error("status " + response.status);
        return response.json();
      })
      .then(function (data) {
        if (!data.finished) {
          setTimeout(poll, INTERVAL);
          return;
        }

        if (data.error) {
          say(data.error);
          return;
        }

        // Reload rather than patching the DOM: the whole page changes when
        // posts arrive — totals, the table, the empty state — and re-rendering
        // it server-side is both simpler and guaranteed consistent.
        say("Done. Loading your posts…");
        window.location.href = "/analytics";
      })
      .catch(function () {
        say("Lost contact with the sync. Refresh to check.");
      });
  }

  setTimeout(poll, INTERVAL);
})();
