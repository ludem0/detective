"use strict";

const POLL_MS = 4000;
const DETAIL_POLL_MS = 8000;
const ARM_MS = 4000;
const STATUS = { waiting: "Ожидание старта", running: "Идёт игра", paused: "Пауза", finished: "Время вышло" };

const A = { state: null, caseRef: null, detailId: null, detailTimer: null, endsAt: null, status: null };

async function init() {
  const me = await api("GET", "/api/me").catch(() => ({ role: null }));
  if (me.role !== "admin") { $("#denied").hidden = false; return; }
  $("#admin").hidden = false;
  A.caseRef = await api("GET", "/api/admin/case");
  $("#debrief").replaceChildren(...paragraphs(A.caseRef.debrief),
    el("h3", null, "Эталон для оценки"),
    el("ol", null, A.caseRef.rubric.map((r) => el("li", null, r.answer, el("br"), el("small", null, `Полный балл: ${r.full}`)))));
  poll();
  setInterval(tickClock, 1000);
}

$("#logout").addEventListener("click", async () => {
  await api("POST", "/api/logout").catch(() => {});
  location.href = "/";
});

/** Опасное действие — по второму нажатию. */
function armed(btn, action) {
  if (btn.dataset.armed) {
    delete btn.dataset.armed;
    btn.textContent = btn.dataset.label;
    btn.classList.remove("armed");
    return action();
  }
  btn.dataset.label = btn.textContent;
  btn.dataset.armed = "1";
  btn.textContent = "Точно? Ещё раз";
  btn.classList.add("armed");
  setTimeout(() => {
    if (!btn.dataset.armed) return;
    delete btn.dataset.armed;
    btn.textContent = btn.dataset.label;
    btn.classList.remove("armed");
  }, ARM_MS);
  return null;
}

async function act(fn, okText) {
  try {
    const r = await fn();
    if (okText) toast(okText);
    await poll();
    return r;
  } catch (err) {
    toast(err.message);
    return null;
  }
}

// ---------- состояние ----------

async function poll() {
  try {
    A.state = await api("GET", "/api/admin/state");
  } catch (err) {
    if (err.status === 401 || err.status === 403) { location.href = "/"; return; }
    return;
  } finally {
    clearTimeout(A.pollTimer);
    A.pollTimer = setTimeout(poll, POLL_MS);
  }
  const st = A.state;
  A.status = st.clock.status;
  A.remaining = st.clock.remaining;
  A.endsAt = st.clock.remaining === null ? null : Date.now() / 1000 + st.clock.remaining;
  tickClock();
  renderSettings(st.settings);
  renderBoard(st.board);
  renderHints(st.hints);
  $("#messages").replaceChildren(...st.messages.slice().reverse().map((m) => el("li", null, `${timeOf(m.at)} — ${m.text}`)));
}

function tickClock() {
  $("#status-label").textContent = STATUS[A.status] || "—";
  if (A.endsAt === null) { $("#clock").textContent = "—"; return; }
  const left = A.status === "running" ? Math.max(0, Math.round(A.endsAt - Date.now() / 1000)) : A.remaining;
  $("#clock").textContent = clockText(left);
}

function renderSettings(s) {
  for (const key of ["duration_min", "order_delay_sec", "fee_coef"]) {
    const input = $(`#set-${key}`);
    if (document.activeElement !== input) input.value = s[key];
  }
  for (const key of ["answers_closed", "results_published", "debrief_published"]) {
    $(`#set-${key}`).checked = s[key];
  }
}

for (const key of ["duration_min", "order_delay_sec", "fee_coef"]) {
  $(`#set-${key}`).addEventListener("change", (e) =>
    act(() => api("POST", "/api/admin/settings", { [key]: Number(e.target.value) }), "Сохранено"));
}
for (const key of ["answers_closed", "results_published", "debrief_published"]) {
  $(`#set-${key}`).addEventListener("change", (e) =>
    act(() => api("POST", "/api/admin/settings", { [key]: e.target.checked }), "Сохранено"));
}

for (const btn of document.querySelectorAll("[data-clock]")) {
  btn.addEventListener("click", () => {
    const body = { action: btn.dataset.clock, minutes: Number(btn.dataset.min || 10) };
    const run = () => act(() => api("POST", "/api/admin/clock", body), "Готово");
    if (body.action === "start" && A.status !== "waiting") armed(btn, run);
    else run();
  });
}

// ---------- команды ----------

function renderBoard(board) {
  $("#board").replaceChildren(...board.map((r) => el("tr", null,
    el("td.place", null, r.place),
    el("td", null, el("button.open", { type: "button", onclick: () => openDetail(r.id) }, r.name)),
    el("td", null, r.login),
    el("td.num", null, r.trips),
    el("td", null, el(`span.badge.${r.submitted ? "ok" : "no"}`, null, r.submitted ? "отправлен" : "нет")),
    el("td.num", null, `${r.points}${r.scored < A.state.questions.length ? ` (${r.scored}/${A.state.questions.length})` : ""}`),
    el("td.num", null, `$${r.fee}`),
    el("td", null, el("button.btn.small.ghost", { type: "button", onclick: () => openDetail(r.id) }, "Открыть")),
  )));
  if (!board.length) $("#board").append(el("tr", null, el("td", { colspan: 8 }, "Команд пока нет — добавьте выше.")));
}

function showCreds(c) {
  $("#creds").hidden = false;
  $("#creds-list").prepend(el("div.cred", null, el("b", null, c.name),
    "Сайт: ", el("code", null, location.origin), el("br"),
    "Логин: ", el("code", null, c.login), el("br"),
    "Пароль: ", el("code", null, c.password)));
}
$("#creds-print").addEventListener("click", () => window.print());

$("#team-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const body = { name: f.name.value.trim() };
  if (f.login.value.trim()) body.login = f.login.value.trim();
  const r = await act(() => api("POST", "/api/admin/teams", body), "Команда добавлена");
  if (r) { showCreds(r); f.reset(); f.name.focus(); }
});

// ---------- карточка команды ----------

async function openDetail(id) {
  A.detailId = id;
  $("#detail").hidden = false;
  await loadDetail(true);
  $("#detail").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function loadDetail(force = false) {
  clearTimeout(A.detailTimer);
  if (A.detailId === null) return;
  try {
    const d = await api("GET", `/api/admin/teams/${A.detailId}`);
    renderDetail(d, force);
  } catch (err) {
    toast(err.message);
    closeDetail();
    return;
  }
  A.detailTimer = setTimeout(() => loadDetail(), DETAIL_POLL_MS);
}

function closeDetail() {
  A.detailId = null;
  clearTimeout(A.detailTimer);
  $("#detail").hidden = true;
}
$("#detail-close").addEventListener("click", closeDetail);

function renderDetail(d) {
  $("#detail-name").textContent = `${d.name} · ${d.login}`;
  $("#detail-trips").textContent = `поездок: ${d.trips}${d.pending.length ? ` · ждут ордера: ${d.pending.map((p) => p.label).join(", ")}` : ""}`;
  $("#detail-unsubmit").hidden = !d.submitted_at;
  const refs = Object.fromEntries(A.caseRef.rubric.map((r) => [r.n, r]));
  $("#detail-answers").replaceChildren(...A.caseRef.questions.map((q) => {
    const answer = d.answers[q.n] || "";
    const score = d.scores[q.n];
    const buttons = [];
    for (let p = 0; p <= q.max; p += 1) {
      buttons.push(el("button", {
        type: "button", "aria-pressed": String(score === p),
        onclick: () => setScore(d.id, q.n, p),
      }, p));
    }
    buttons.push(el("button", { type: "button", title: "Снять оценку", onclick: () => setScore(d.id, q.n, null) }, "—"));
    return el("div.qa", null,
      el("div.qa-q", null, `${q.n}. ${q.text} (до ${q.max})`),
      el(`div.qa-a${answer ? "" : ".blank"}`, null, answer || "нет ответа"),
      el("div.qa-ref", null, el("b", null, "Эталон: "), refs[q.n].answer, el("br"), el("i", null, `Полный балл: ${refs[q.n].full}`)),
      el("div.qa-score", null, "Баллы:", buttons));
  }));
  $("#detail-events").replaceChildren(...d.events.slice().reverse().map((ev) => el(`li${ev.empty ? ".empty" : ""}`, null,
    el("span.addr", null, ev.label), " ", timeOf(ev.at), ev.counted ? "" : " · бесплатно",
    ev.empty ? " · пусто" : el("details", null, el("summary", null, ev.title || ""),
      ...paragraphs(ev.text),
      ev.appendices.map((a) => el("button.linkish", { type: "button", onclick: () => lightbox.open(a.previews[0], `Приложение ${a.n}. ${a.title}`) }, `Приложение ${a.n} `))),
  )));
}

async function setScore(teamId, q, points) {
  await act(() => api("PUT", `/api/admin/teams/${teamId}/scores/${q}`, { points }));
  loadDetail();
}

$("#detail-unsubmit").addEventListener("click", () =>
  act(() => api("POST", `/api/admin/teams/${A.detailId}/unsubmit`), "Бланк возвращён команде").then(() => loadDetail()));
$("#detail-password").addEventListener("click", (e) => armed(e.target, async () => {
  const r = await act(() => api("POST", `/api/admin/teams/${A.detailId}/password`), "Пароль изменён, старые сессии закрыты");
  if (r) showCreds(r);
}));
$("#detail-wipe").addEventListener("click", (e) => armed(e.target, () =>
  act(() => api("POST", `/api/admin/teams/${A.detailId}/wipe`), "Прогресс команды обнулён").then(() => loadDetail())));
$("#detail-delete").addEventListener("click", (e) => armed(e.target, () =>
  act(() => api("DELETE", `/api/admin/teams/${A.detailId}`), "Команда удалена").then(closeDetail)));

// ---------- подсказки ----------

function renderHints(hints) {
  $("#hints").replaceChildren(...hints.map((h) => el("div.hint-row", null,
    el(`span.state${h.visible ? ".on" : ""}`, null,
      h.visible ? `выпущена ${timeOf(h.at)}` : h.auto_minute !== null ? `авто на ${h.auto_minute}-й мин` : "вручную"),
    el("div.body", null, h.text, el("span.note", null, h.note)),
    h.visible ? null : el("button.btn.small", { type: "button", onclick: (e) => armed(e.target, () =>
      act(() => api("POST", `/api/admin/hints/${h.id}/release`), "Подсказка отправлена командам")) }, "Выпустить"),
  )));
}

$("#message-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const r = await act(() => api("POST", "/api/admin/messages", { text: f.text.value.trim() }), "Сообщение отправлено");
  if (r) f.reset();
});

$("#reset-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const r = await act(() => api("POST", "/api/admin/reset", { confirm: f.confirm.value.trim() }), "Игра сброшена");
  if (r) { f.reset(); closeDetail(); }
});

init();
