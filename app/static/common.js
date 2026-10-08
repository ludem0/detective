"use strict";

const $ = (sel, root = document) => root.querySelector(sel);

async function api(method, url, body) {
  const opts = { method, headers: { "X-Requested-With": "fetch" }, credentials: "same-origin" };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  let data = null;
  try { data = await res.json(); } catch (_) { /* пустой ответ */ }
  if (!res.ok) {
    const detail = data && data.detail;
    const message = typeof detail === "string" ? detail : "Ошибка запроса. Попробуйте ещё раз.";
    const err = new Error(message);
    err.status = res.status;
    throw err;
  }
  return data;
}

/** Создать элемент: el("p.note", {title: "x"}, "текст", child). Текст всегда как textContent. */
function el(tag, attrs, ...children) {
  const [name, ...classes] = tag.split(".");
  const node = document.createElement(name || "div");
  if (classes.length) node.className = classes.join(" ");
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else if (k === "dataset") Object.assign(node.dataset, v);
      else node.setAttribute(k, v === true ? "" : v);
    }
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function paragraphs(text) {
  return text.split(/\n\s*\n/).map((p) => p.trim()).filter(Boolean)
    .map((p) => el(p.startsWith("(") ? "p.note" : "p", null, p));
}

function clockText(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function timeOf(ts) {
  return new Date(ts * 1000).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

let toastTimer = null;
function toast(text) {
  const t = $("#toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 4500);
}

const lightbox = {
  open(src, title) {
    $("#lightbox-img").src = src;
    $("#lightbox-img").alt = title;
    $("#lightbox-title").textContent = title;
    $("#lightbox-open").href = src;
    $("#lightbox-stage").classList.remove("zoomed");
    $("#lightbox").hidden = false;
    $("#lightbox-close").focus();
  },
  close() {
    $("#lightbox").hidden = true;
    $("#lightbox-img").removeAttribute("src");
  },
};

document.addEventListener("DOMContentLoaded", () => {
  if (!$("#lightbox")) return;
  $("#lightbox-close").addEventListener("click", lightbox.close);
  $("#lightbox-img").addEventListener("click", () => $("#lightbox-stage").classList.toggle("zoomed"));
  $("#lightbox-stage").addEventListener("click", (e) => { if (e.target.id === "lightbox-stage") lightbox.close(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#lightbox").hidden) lightbox.close(); });
});
