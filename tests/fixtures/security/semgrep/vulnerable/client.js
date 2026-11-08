// Фикстура SAST (P6-04): заведомо уязвимый JS.
const cp = require("child_process");

function render(el, html) {
  el.innerHTML = html;
}

function run(cmd) {
  cp.exec(cmd);
}

module.exports = { render, run };
