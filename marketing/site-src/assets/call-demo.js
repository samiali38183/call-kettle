/* Accessible audio-led test-call examples, plus nav. No dependencies. */
(function () {
  "use strict";
  var btn = document.getElementById("menu-btn"), nav = document.getElementById("site-nav");
  if (btn && nav) btn.addEventListener("click", function () { var open = nav.classList.toggle("open"); btn.setAttribute("aria-expanded", open ? "true" : "false"); });
  var root = document.getElementById("callui"), dataEl = document.getElementById("calls-data");
  if (root && dataEl) {
    var calls; try { calls = JSON.parse(dataEl.textContent); } catch (_) { calls = []; }
    var body = root.querySelector(".cu-body"), status = root.querySelector(".cu-status"), result = root.querySelector(".cu-result");
    var title = root.querySelector("[data-cu-title]"), sub = root.querySelector("[data-cu-sub]");
    var tabs = Array.prototype.slice.call(root.querySelectorAll(".cu-tab"));
    var audio = root.querySelector("[data-cu-audio]"), playBtn = root.querySelector("[data-cu-play]");
    var replayBtn = root.querySelector("[data-cu-replay]"), transcriptBtn = root.querySelector("[data-cu-transcript]"), audioNote = root.querySelector("[data-cu-audio-note]");
    var bar = root.querySelector("[data-cu-progress]"), timeEl = root.querySelector("[data-cu-time]");
    var current = 0, rendered = -1, ended = false, unavailable = false, generation = 0;
    function esc(s) { var d = document.createElement("div"); d.textContent = s; return d.innerHTML; }
    function setStatus(text, cls) { status.textContent = text; status.className = "cu-status" + (cls ? " " + cls : ""); }
    function addMsg(turn, active, n) { var el = document.createElement("div"); el.className = "msg " + (turn.who === "ai" ? "ai" : "caller") + (active ? " current" : ""); el.setAttribute("data-i", String(n)); el.setAttribute("role", "button"); el.setAttribute("tabindex", "0"); el.setAttribute("title", "Play from here"); el.innerHTML = "<small>" + (turn.who === "ai" ? "AI receptionist" : "Sample caller") + "</small>" + esc(turn.text); body.appendChild(el); }
    function showResult(c) {
      var text = "";
      if (c.booking) { text = '<div class="res-card"><span class="lbl">' + (c.source === "scripted" ? "Example booking" : "Recorded test booking") + '</span><b>' + esc(c.booking.service) + "</b> &middot; " + esc(c.booking.when) + " &middot; " + esc(c.booking.name) + "</div>"; text += '<div class="res-card"><span class="lbl">Owner workflow</span>Bookings appear in the owner portal. Email and calendar notifications are checked during your setup.</div>'; }
      else if (c.outcomeNote) text = '<div class="res-card"><span class="lbl">Recorded test outcome</span>' + esc(c.outcomeNote) + "</div>";
      result.innerHTML = text; result.classList.toggle("show", !!text);
    }
    function renderThrough(index) { if (index === rendered) return; rendered = index; body.innerHTML = ""; calls[current].turns.slice(0, index + 1).forEach(function (turn, i) { addMsg(turn, i === index, i); }); body.scrollTop = body.scrollHeight; }
    function fmt(seconds) { var s = Math.max(0, Math.floor(seconds || 0)); return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); }
    function paintTime() { var total = audio.duration || (calls[current].audio && calls[current].audio.duration) || 0; if (bar) bar.style.width = total ? Math.min(100, (audio.currentTime / total) * 100) + "%" : "0%"; if (timeEl) timeEl.textContent = fmt(audio.currentTime) + " / " + fmt(total); }
    function buttonState() { if (!playBtn) return; playBtn.disabled = unavailable; playBtn.textContent = ended ? "Replay this call" : (!audio.paused ? "Pause" : audio.currentTime > 0 ? "Resume" : "Play this call"); playBtn.setAttribute("aria-label", playBtn.textContent + ": " + calls[current].label); }
    function failAudio() { unavailable = true; audio.pause(); root.classList.remove("speaking"); setStatus("Audio unavailable", "done"); audioNote.textContent = "Audio unavailable. Read the transcript below or call the live demo."; buttonState(); }
    function choose(i) {
      generation++; audio.pause(); current = i; ended = false; unavailable = false; rendered = -1;
      tabs.forEach(function (tb, k) { tb.setAttribute("aria-selected", k === i ? "true" : "false"); tb.setAttribute("tabindex", k === i ? "0" : "-1"); });
      var c = calls[i]; title.textContent = c.business; sub.textContent = (c.source === "scripted" ? "Scripted example · " : "Saved test-call example · ") + c.label; result.classList.remove("show"); result.innerHTML = ""; root.classList.remove("speaking");
      if (!c.audio || !c.audio.src || !Array.isArray(c.audio.cues)) { renderThrough(0); paintTime(); failAudio(); return; }
      audio.src = c.audio.src; audio.load(); audioNote.textContent = c.source === "scripted" ? "Scripted example. Synthetic narration, not a customer recording or a saved test call." : "Saved test-call text with synthetic narration. Call the live demo to hear the current phone voice."; renderThrough(0); paintTime(); setStatus("Ready to play", ""); buttonState();
    }
    function start(reset) { if (unavailable) return; if (reset || ended) { ended = false; audio.currentTime = 0; rendered = -1; result.classList.remove("show"); renderThrough(0); } var mine = generation; try { var pending = audio.play(); if (pending && pending.catch) pending.catch(function () { if (mine === generation) failAudio(); }); } catch (_) { failAudio(); } }
    if (audio && calls.length) {
      audio.addEventListener("play", function () { ended = false; setStatus("Playing example", "live"); root.classList.add("speaking"); buttonState(); });
      audio.addEventListener("pause", function () { root.classList.remove("speaking"); if (!ended && !unavailable) setStatus(audio.currentTime > 0 ? "Paused" : "Ready to play", ""); buttonState(); });
      audio.addEventListener("timeupdate", function () { var cues = calls[current].audio && calls[current].audio.cues, index = 0; paintTime(); if (!cues) return; for (var i = 0; i < cues.length; i++) if (audio.currentTime >= cues[i]) index = i; renderThrough(index); });
      audio.addEventListener("ended", function () { ended = true; renderThrough(calls[current].turns.length - 1); showResult(calls[current]); root.classList.remove("speaking"); setStatus("Example complete", "done"); buttonState(); });
      audio.addEventListener("error", failAudio);
      if (playBtn) playBtn.addEventListener("click", function () { if (!audio.paused && !ended) audio.pause(); else start(false); });
      if (replayBtn) replayBtn.addEventListener("click", function () { start(true); });
      if (transcriptBtn) transcriptBtn.addEventListener("click", function () { audio.pause(); rendered = -1; renderThrough(calls[current].turns.length - 1); showResult(calls[current]); setStatus("Full transcript", "done"); });
      function seekTurn(n) { var cues = calls[current].audio && calls[current].audio.cues; if (unavailable || !cues || !(n >= 0 && n < cues.length)) return; ended = false; result.classList.remove("show"); audio.currentTime = cues[n] + 0.01; rendered = -1; renderThrough(n); paintTime(); start(false); }
      function msgFrom(e) { var t = e.target; return t && t.closest ? t.closest(".msg") : null; }
      body.addEventListener("click", function (e) { var m = msgFrom(e); if (m) seekTurn(parseInt(m.getAttribute("data-i"), 10)); });
      body.addEventListener("keydown", function (e) { if (e.key !== "Enter" && e.key !== " ") return; var m = msgFrom(e); if (m) { e.preventDefault(); seekTurn(parseInt(m.getAttribute("data-i"), 10)); } });
      var track = root.querySelector(".cu-progress");
      if (track && track.getBoundingClientRect) track.addEventListener("click", function (e) { var total = audio.duration || (calls[current].audio && calls[current].audio.duration) || 0, r = track.getBoundingClientRect(); if (!total || !r.width || unavailable) return; audio.currentTime = Math.max(0, Math.min(total - 0.05, ((e.clientX - r.left) / r.width) * total)); ended = false; result.classList.remove("show"); paintTime(); });
      tabs.forEach(function (tb, i) { tb.addEventListener("click", function () { choose(i); }); tb.addEventListener("keydown", function (e) { var next = e.key === "ArrowRight" ? (i + 1) % calls.length : e.key === "ArrowLeft" ? (i + calls.length - 1) % calls.length : e.key === "Home" ? 0 : e.key === "End" ? calls.length - 1 : -1; if (next >= 0) { e.preventDefault(); choose(next); tabs[next].focus(); } }); });
      choose(0);
    }
  }
  Array.prototype.forEach.call(document.querySelectorAll("[data-copy]"), function (el) { el.addEventListener("click", function () { if (navigator.clipboard) navigator.clipboard.writeText(el.getAttribute("data-copy")).then(function () { var old = el.textContent; el.textContent = "Copied"; setTimeout(function () { el.textContent = old; }, 1400); }).catch(function () {}); }); });
})();
