/* 登录页：一条会呼吸的流量曲线做背景，加上密码提交。 */
(function () {
  "use strict";

  var form = document.getElementById("gate-form");
  var input = document.getElementById("password");
  var button = document.getElementById("gate-go");
  var error = document.getElementById("gate-err");

  function fail(message) {
    error.textContent = message;
    error.hidden = false;
    input.select();
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    error.hidden = true;
    button.classList.add("busy");
    button.disabled = true;
    fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: input.value })
    })
      .then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (data) {
          if (!response.ok) { throw new Error(data.detail || "登录失败（HTTP " + response.status + "）"); }
          return data;
        });
      })
      .then(function () { location.replace("/"); })
      .catch(function (exc) {
        fail(exc.message || "登录失败");
        button.classList.remove("busy");
        button.disabled = false;
      });
  });

  // 已经登录过就别停在登录页
  fetch("/api/session").then(function (r) { return r.json(); }).then(function (data) {
    if (data && data.logged_in) { location.replace("/"); }
  }).catch(function () {});

  var canvas = document.getElementById("gate-flow");
  // 老旧 WebView 可能没有 matchMedia，这里做兜底，别让动画代码拖垮登录逻辑
  var calm = false;
  try { calm = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches); } catch (e) {}
  if (!canvas || calm || !canvas.getContext) { return; }
  var ctx = canvas.getContext("2d");
  if (!ctx) { return; }
  var phase = 0;

  function size() {
    var ratio = window.devicePixelRatio || 1;
    canvas.width = Math.floor(canvas.clientWidth * ratio);
    canvas.height = Math.floor(canvas.clientHeight * ratio);
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  }

  function wave(offset, color, amp, speed) {
    var w = canvas.clientWidth;
    var h = canvas.clientHeight;
    ctx.beginPath();
    for (var x = 0; x <= w; x += 4) {
      var t = x / w * Math.PI * 2;
      var y = h * 0.62 + Math.sin(t * 1.6 + phase * speed + offset) * amp
        + Math.sin(t * 3.1 + phase * speed * 0.6) * amp * 0.35;
      if (x === 0) { ctx.moveTo(x, y); } else { ctx.lineTo(x, y); }
    }
    ctx.lineTo(w, h);
    ctx.lineTo(0, h);
    ctx.closePath();
    var fill = ctx.createLinearGradient(0, h * 0.3, 0, h);
    fill.addColorStop(0, color + "26");
    fill.addColorStop(1, color + "00");
    ctx.fillStyle = fill;
    ctx.fill();
    ctx.strokeStyle = color + "80";
    ctx.lineWidth = 1.2;
    ctx.stroke();
  }

  function frame() {
    ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
    wave(0, "#37E5B5", 16, 0.018);
    wave(1.9, "#6E8BFF", 11, 0.012);
    phase += 1;
    requestAnimationFrame(frame);
  }

  size();
  window.addEventListener("resize", size);
  requestAnimationFrame(frame);
})();
