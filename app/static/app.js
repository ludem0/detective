"use strict";

const POLL_MS = 4000;
const ARM_MS = 4000;
const SAVE_DELAY_MS = 700;
const BOOK_LIMIT = 300;
// Ведущий открывает /?view=<id>: экран этой команды без поездок и правки бланка.
const VIEW_TEAM = new URLSearchParams(location.search).get("view");

const S = {
  caseInfo: null,
  district: null,
  lastEventId: 0,
  cards: new Map(),
  seenBulletins: new Set(),
  firstLoad: true,
  endsAt: null,
  status: null,
  armed: null,
  armTimer: null,
  saveTimers: {},
  dirty: {},
  book: null,
  locked: false,
  submitArmed: false,
  pollTimer: null,
};

// ---------- вход ----------

async function init() {
  const me = await api("GET", "/api/me").catch(() => ({ role: null }));
  if (me.role === "admin" && VIEW_TEAM) { showApp(""); return; }
  if (me.role === "admin") { location.href = "/admin"; return; }
  if (me.role === "team") { showApp(me.team); return; }
  showLogin();
}

function showLogin() {
  clearTimeout(S.pollTimer);
  $("#app-view").hidden = true;
  $("#login-view").hidden = false;
  $("#login-form").login.focus();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  $("#login-error").textContent = "";
  try {
    const r = await api("POST", "/api/login", { login: f.login.value.trim(), password: f.password.value });
    if (r.role === "admin") { location.href = "/admin"; return; }
    f.password.value = "";
    init();
  } catch (err) {
    $("#login-error").textContent = err.message;
  }
});

$("#logout").addEventListener("click", async () => {
  await api("POST", "/api/logout").catch(() => {});
  location.reload();
});

function showApp(team) {
  $("#login-view").hidden = true;
  $("#app-view").hidden = false;
  $("#team-name").textContent = team;
  $("#view-banner").hidden = !VIEW_TEAM;
  $("#logout").hidden = Boolean(VIEW_TEAM); // выход из экрана команды разлогинил бы ведущего
  poll();
  setInterval(tickClock, 1000);
}

// ---------- опрос сервера ----------

async function poll() {
  clearTimeout(S.pollTimer);
  try {
    const view = VIEW_TEAM ? `&view=${encodeURIComponent(VIEW_TEAM)}` : "";
    const st = await api("GET", `/api/state?since=${S.lastEventId}${view}`);
    await applyState(st);
  } catch (err) {
    if (err.status === 401) { showLogin(); return; }
    if (VIEW_TEAM && err.status >= 400 && err.status < 500) { location.href = "/admin"; return; }
  }
  S.pollTimer = setTimeout(poll, POLL_MS);
}

async function applyState(st) {
  $("#team-name").textContent = st.team;
  $("#trips").textContent = st.trips;
  S.status = st.clock.status;
  S.remaining = st.clock.remaining;
  S.endsAt = st.clock.remaining === null ? null : Date.now() / 1000 + st.clock.remaining;
  tickClock();

  const waiting = st.clock.status === "waiting";
  $("#waiting").hidden = !waiting;
  $("#game").hidden = waiting;
  if (waiting) {
    // Ведущий мог сбросить игру: начинаем журнал заново.
    S.caseInfo = null;
    S.cards.clear();
    S.lastEventId = 0;
    $("#events").replaceChildren();
    $("#journal-empty").hidden = false;
    return;
  }
  if (!S.caseInfo) await loadCase();

  let fresh = 0;
  for (const ev of st.events) {
    if (!S.cards.has(ev.id) && !S.firstLoad && ev.source !== "trip") fresh += 1;
    addEvent(ev);
  }
  if (fresh) toast("Для вас есть новая информация по адресам");
  renderPending(st.pending);
  renderBulletins(st.hints, st.messages);
  renderAnswers(st.answers, st.answers_locked, st.submitted_at);
  renderResults(st);
  setDispatchEnabled(st.clock.status === "running" && !VIEW_TEAM);
  S.firstLoad = false;
}

function tickClock() {
  const gauge = $("#timer-gauge");
  if (S.endsAt === null) { $("#timer").textContent = "—"; return; }
  // На паузе часы стоят: показываем остаток с сервера, а не отсчитываем.
  const left = S.status === "running" ? Math.max(0, Math.round(S.endsAt - Date.now() / 1000)) : S.remaining;
  $("#timer").textContent = clockText(left);
  gauge.classList.toggle("urgent", S.status === "running" && left < 600);
  gauge.classList.toggle("stopped", S.status !== "running");
  if (S.status === "running" && left === 0) poll();
}

// ---------- дело и материалы ----------

async function loadCase() {
  S.caseInfo = await api("GET", "/api/case");
  const c = S.caseInfo;
  $("#case-no").textContent = `Дело №${c.id}`;
  $("#case-title").textContent = c.title;
  document.title = `Сейнт-Твинс · Дело №${c.id} «${c.title}»`;
  renderCompass(c.districts);
  renderBrief(c);
  renderMaterials(c.materials);
  renderQuestions(c.questions);
}

function renderCompass(districts) {
  const box = $("#compass");
  box.replaceChildren();
  for (const d of districts) {
    box.append(el(`button.district${d.code === "П" ? ".suburb" : ""}`, {
      type: "button", "aria-pressed": "false", dataset: { code: d.code },
      onclick: () => selectDistrict(d.code),
    }, el("b", null, d.code), el("span", null, d.name)));
  }
  const sel = $("#book-district");
  sel.replaceChildren(el("option", { value: "" }, "Все районы"),
    ...districts.map((d) => el("option", { value: d.code }, `${d.code} — ${d.name}`)));
  if (S.district) selectDistrict(S.district);
}

function selectDistrict(code) {
  S.district = code;
  for (const b of document.querySelectorAll(".district")) {
    b.setAttribute("aria-pressed", String(b.dataset.code === code));
  }
  $("#prefix").textContent = code;
  disarm();
}

function renderBrief(c) {
  const letter = $("#brief");
  letter.replaceChildren(
    el("div.when", null, `${c.case_time}\nДетективное агентство\nСейнт-Твинс`),
    ...paragraphs(c.brief),
    el("h3", null, "Контакты и полномочия"),
    el("ul", null, c.contacts.map((t) => el("li", null, t))),
  );
}

function renderMaterials(materials) {
  const box = $("#material-groups");
  box.replaceChildren();
  const groups = new Map();
  for (const m of materials) {
    if (!groups.has(m.group)) groups.set(m.group, []);
    groups.get(m.group).push(m);
  }
  for (const [name, items] of groups) {
    const images = items.filter((m) => m.kind === "image");
    const pdfs = items.filter((m) => m.kind === "pdf");
    box.append(el("div.group", null,
      el("h3", null, name),
      pdfs.length ? el("div.downloads", { style: images.length ? "margin-bottom:12px" : null },
        pdfs.map((m) => el("a.btn.small", { href: `${m.url}?dl=1` }, `⤓ ${m.title}`))) : null,
      images.length ? el("div.tiles", null, images.map((m) => el("div.tile", null,
        el("button", { type: "button", title: m.title, onclick: () => lightbox.open(m.url, m.title) },
          el("img", { src: m.url, alt: m.title, loading: "lazy" })),
        el("span", null, m.title),
        el("a", { href: `${m.url}?dl=1` }, "Скачать"),
      ))) : null,
    ));
  }
}

// ---------- поездки ----------

function fullAddress() {
  const number = $("#number").value.trim();
  const m = number.match(/^([А-Яа-яЁё]{1,2})\s*-\s*(.+)$/);
  if (m && S.caseInfo.districts.some((d) => d.code === m[1].toUpperCase())) {
    return { district: m[1].toUpperCase(), number: m[2], label: `${m[1].toUpperCase()}-${m[2]}` };
  }
  return { district: S.district, number, label: `${S.district}-${number}` };
}

function msg(text, bad = false) {
  const m = $("#visit-msg");
  m.textContent = text;
  m.classList.toggle("bad", bad);
}

function disarm() {
  S.armed = null;
  clearTimeout(S.armTimer);
  const go = $("#go");
  go.classList.remove("armed");
  go.textContent = "Запросить адрес";
}

async function requestVisit() {
  if (S.status !== "running") return;
  const a = fullAddress();
  if (!a.number) { msg("Введите номер дома.", true); $("#number").focus(); return; }
  if (!a.district) { msg("Сначала выберите район.", true); return; }
  if (S.armed !== a.label) {
    S.armed = a.label;
    const go = $("#go");
    go.classList.add("armed");
    go.textContent = `Подтвердить: ${a.label}`;
    msg("Нажмите ещё раз, чтобы поехать.");
    clearTimeout(S.armTimer);
    S.armTimer = setTimeout(() => { disarm(); msg(""); }, ARM_MS);
    return;
  }
  disarm();
  const go = $("#go");
  go.disabled = true;
  try {
    const r = await api("POST", "/api/visit", { district: a.district, number: a.number });
    $("#trips").textContent = r.trips;
    if (r.event) {
      const known = S.cards.has(r.event.id);
      addEvent(r.event);
      if (known) flash(r.event.id);
      msg(r.new ? (r.counted ? `Поездка ${a.label} засчитана.` : "") : "Вы уже были здесь — повторная поездка бесплатна.");
      $("#number").value = "";
    } else if (r.message) {
      msg(r.message);
    }
  } catch (err) {
    msg(err.message, true);
  } finally {
    go.disabled = S.status !== "running";
  }
}

$("#go").addEventListener("click", requestVisit);
$("#number").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); requestVisit(); } });
$("#number").addEventListener("input", () => { if (S.armed) { disarm(); msg(""); } });

function setDispatchEnabled(on) {
  $("#go").disabled = !on;
  $("#number").disabled = !on;
  for (const b of document.querySelectorAll(".district")) b.disabled = !on;
  if (!on && S.status === "finished") msg("Время вышло: поездки закрыты. Заполните бланк ответов.");
  if (!on && S.status === "paused") msg("Игра на паузе: ведущий остановил время.");
  if (on && /паузе|Время вышло/.test($("#visit-msg").textContent)) msg("");
}

function flash(id) {
  const node = S.cards.get(id);
  if (!node) return;
  node.classList.remove("flash");
  void node.offsetWidth;
  node.classList.add("flash");
  node.scrollIntoView({ behavior: "smooth", block: "center" });
}

const TAGS = { trigger: "Пришло само", search: "Обыск" };

function addEvent(ev) {
  if (S.cards.has(ev.id)) return;
  S.lastEventId = Math.max(S.lastEventId, ev.id);
  const tag = TAGS[ev.source] || (ev.title === "Запрос ордера" ? "Ордер" : null);
  const meta = `${timeOf(ev.at)} · ${ev.counted ? "поездка" : "бесплатно"}`;
  const node = el(`li.card${ev.empty ? ".is-empty" : ""}`, { dataset: { id: ev.id } },
    tag ? el("span.tag", null, tag) : null,
    el("div.card-head", null, el("span.addr", null, ev.label), el("span.card-meta", null, meta)),
    ev.title ? el("h3", null, ev.title) : null,
    el("div.card-text", null, paragraphs(ev.text)),
    ev.appendices.length ? el("div.attachments", null, ev.appendices.map(attachment)) : null,
  );
  S.cards.set(ev.id, node);
  const list = $("#events");
  const after = [...list.children].find((li) => Number(li.dataset.id) < ev.id);
  list.insertBefore(node, after || null);
  $("#journal-empty").hidden = true;
  applyFilter();
}

function attachment(a) {
  const title = `Приложение ${a.n}. ${a.title}`;
  return el("div.attachment", null,
    el("button.thumb", { type: "button", title, onclick: () => lightbox.open(a.previews[0], title) },
      el("img", { src: a.previews[0], alt: title, loading: "lazy" })),
    el("div.att-body", null,
      el("span.att-n", null, `Приложение ${a.n}`), el("br"), a.title,
      a.previews.length > 1 ? el("div", null, a.previews.map((p, i) =>
        el("button.linkish", { type: "button", style: "margin-right:8px", onclick: () => lightbox.open(p, `${title}, стр. ${i + 1}`) }, `стр. ${i + 1}`))) : null,
      el("div", null, el("a", { href: `${a.download}?dl=1` }, "Скачать")),
    ),
  );
}

function applyFilter() {
  const q = $("#journal-filter").value.trim().toLowerCase();
  for (const node of S.cards.values()) {
    node.hidden = Boolean(q) && !node.textContent.toLowerCase().includes(q);
  }
}
$("#journal-filter").addEventListener("input", applyFilter);

function renderPending(pending) {
  const p = $("#pending");
  p.hidden = !pending.length;
  p.replaceChildren(el("b", null, "Оформляются ордера: "), pending.map((x) => x.label.replace(/\/обыск$/, "")).join(", "));
}

function renderBulletins(hints, messages) {
  const items = [
    ...hints.map((h) => ({ key: `h${h.id}`, text: h.text, at: h.at, kind: "Подсказка" })),
    ...messages.map((m) => ({ key: `m${m.id}`, text: m.text, at: m.at, kind: "Сводка от ведущего" })),
  ].sort((a, b) => b.at - a.at);
  const fresh = items.filter((i) => !S.seenBulletins.has(i.key));
  if (fresh.length && !S.firstLoad) toast(`${fresh[0].kind}: ${fresh[0].text}`);
  fresh.forEach((i) => S.seenBulletins.add(i.key));
  $("#bulletins").replaceChildren(...items.map((i) =>
    el("div.bulletin", null, el("small", null, `${i.kind} · ${timeOf(i.at)}`), i.text)));
}

// ---------- адресная книга ----------

async function openBook() {
  if (!S.book) {
    $("#book-count").textContent = "Загружаю…";
    S.book = await api("GET", "/api/addressbook");
    const cats = [...new Set(S.book.map((r) => r.c))];
    $("#book-cat").replaceChildren(el("option", { value: "" }, "Все разделы"),
      ...cats.map((c) => el("option", { value: c }, c.charAt(0) + c.slice(1).toLowerCase())));
  }
  renderBook();
}

function renderBook() {
  const q = $("#book-q").value.trim().toLowerCase().replace(/ё/g, "е");
  const district = $("#book-district").value;
  const cat = $("#book-cat").value;
  const rows = S.book.filter((r) =>
    (!district || r.a.split("-")[0] === district)
    && (!cat || r.c === cat)
    && (!q || `${r.a} ${r.t} ${r.g}`.toLowerCase().replace(/ё/g, "е").includes(q)));
  const shown = rows.slice(0, BOOK_LIMIT);
  $("#book-count").textContent = rows.length > BOOK_LIMIT
    ? `Найдено ${rows.length}, показаны первые ${BOOK_LIMIT} — уточните поиск.`
    : `Найдено: ${rows.length}`;
  $("#book-rows").replaceChildren(...shown.map((r) => el("tr", null,
    el("td", null, el("button", { type: "button", title: "Подставить в поездку", onclick: () => useAddress(r.a) }, r.a)),
    el("td", null, r.t),
    el("td", null, r.g || r.c.charAt(0) + r.c.slice(1).toLowerCase()),
  )));
}
["#book-q", "#book-district", "#book-cat"].forEach((s) => $(s).addEventListener("input", () => S.book && renderBook()));

function useAddress(address) {
  const [district, ...rest] = address.split("-");
  selectDistrict(district);
  $("#number").value = rest.join("-");
  switchTab("investigate");
  $("#number").focus();
}

// ---------- бланк ответов ----------

function renderQuestions(questions) {
  $("#answers-max").textContent = `${questions.length} вопросов, максимум ${questions.reduce((s, q) => s + q.max, 0)} баллов. `;
  $("#questions").replaceChildren(...questions.map((q) => el("div.question", null,
    el("label", { for: `q${q.n}` }, el("span.qn", null, q.n), el("span", null, q.text), el("span.qmax", null, `до ${q.max} б.`)),
    el("textarea", { id: `q${q.n}`, dataset: { q: q.n }, oninput: (e) => scheduleSave(q.n, e.target.value) }),
    el("div.save-state", { id: `qs${q.n}` }),
  )));
}

function scheduleSave(q, text) {
  S.dirty[q] = true;
  $(`#qs${q}`).textContent = "Печатаю…";
  clearTimeout(S.saveTimers[q]);
  S.saveTimers[q] = setTimeout(() => saveAnswer(q, text), SAVE_DELAY_MS);
}

async function saveAnswer(q, text) {
  clearTimeout(S.saveTimers[q]);
  try {
    await api("PUT", `/api/answers/${q}`, { text });
    S.dirty[q] = false;
    $(`#qs${q}`).textContent = `Сохранено в ${timeOf(Date.now() / 1000)}`;
  } catch (err) {
    $(`#qs${q}`).textContent = err.message;
  }
}

function renderAnswers(answers, locked, submittedAt) {
  S.locked = locked;
  for (const ta of document.querySelectorAll("#questions textarea")) {
    const q = ta.dataset.q;
    const server = answers[q] || "";
    if (document.activeElement !== ta && !S.dirty[q] && ta.value !== server) ta.value = server;
    ta.disabled = locked || Boolean(VIEW_TEAM);
  }
  $("#submit").hidden = locked || Boolean(VIEW_TEAM);
  $("#submit-state").replaceChildren(submittedAt
    ? el("span.sent", null, `Бланк отправлен · ${timeOf(submittedAt)}`)
    : locked ? el("span", null, "Приём ответов закрыт ведущим.") : "");
}

$("#submit").addEventListener("click", async () => {
  const btn = $("#submit");
  if (!S.submitArmed) {
    S.submitArmed = true;
    btn.classList.add("armed");
    btn.textContent = "Точно отправить? Нажмите ещё раз";
    setTimeout(() => { S.submitArmed = false; btn.classList.remove("armed"); btn.textContent = "Отправить бланк"; }, ARM_MS);
    return;
  }
  S.submitArmed = false;
  btn.disabled = true;
  try {
    for (const ta of document.querySelectorAll("#questions textarea")) {
      if (S.dirty[ta.dataset.q]) await saveAnswer(ta.dataset.q, ta.value);
    }
    await api("POST", "/api/answers/submit");
    toast("Бланк отправлен. Ждите итогов от ведущего.");
    poll();
  } catch (err) {
    toast(err.message);
  } finally {
    btn.disabled = false;
    btn.classList.remove("armed");
    btn.textContent = "Отправить бланк";
  }
});

// ---------- итоги ----------

function renderResults(st) {
  const has = Boolean(st.results || st.debrief);
  const tab = $('.tab[data-tab="results"]');
  if (has && tab.hidden) {
    tab.hidden = false;
    tab.append(el("span.dot"));
    if (!S.firstLoad) toast("Ведущий опубликовал итоги.");
  }
  if (!has) { tab.hidden = true; return; }
  const box = $("#results");
  box.replaceChildren();
  if (st.results) {
    box.append(el("h2", null, "Итоги"), el("table.board", null,
      el("thead", null, el("tr", null, el("th", null, "#"), el("th", null, "Агентство"),
        el("th.num", null, "Баллы"), el("th.num", null, "Поездки"), el("th.num", null, "Гонорар"))),
      el("tbody", null, st.results.map((r) => el(`tr${r.me ? ".me" : ""}`, null,
        el("td.place", null, r.place), el("td", null, r.name), el("td.num", null, r.points),
        el("td.num", null, r.trips), el("td.num", null, `$${r.fee}`)))),
    ));
    if (st.scores && S.caseInfo) {
      box.append(el("p", null, "Ваши баллы по вопросам: ",
        S.caseInfo.questions.map((q) => `${q.n}) ${st.scores[q.n] ?? "—"}/${q.max}`).join(" · ")));
    }
  }
  const db = $("#debrief");
  db.replaceChildren();
  if (st.debrief) {
    db.append(el("h2", null, "Что произошло на самом деле"), ...paragraphs(st.debrief.text),
      el("h2", { style: "margin-top:24px" }, "Эталонные ответы"),
      el("ol.ref", null, st.debrief.rubric.map((r) => el("li", null, el("b", null, `${r.n}.`), r.answer))));
  }
}

// ---------- вкладки ----------

function switchTab(name) {
  for (const t of document.querySelectorAll(".tab")) {
    t.setAttribute("aria-selected", String(t.dataset.tab === name));
    if (t.dataset.tab === name) t.querySelector(".dot")?.remove();
  }
  for (const p of document.querySelectorAll("[data-panel]")) p.hidden = p.dataset.panel !== name;
  if (name === "book") openBook().catch((err) => { $("#book-count").textContent = err.message; });
}
for (const t of document.querySelectorAll(".tab")) t.addEventListener("click", () => switchTab(t.dataset.tab));

init();
