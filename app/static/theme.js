/* «Стенограмма»: тема оформления — auto | dark | light.
   Подключать в <head> обычным <script src> (без defer/async), до стилей страницы — тогда нет вспышки не той темы.
   Тема по умолчанию: атрибут <html data-default-theme="dark">, иначе "dark".
   Выбор хранится в localStorage["theme"]; "auto" следует за системной темой и меняется вместе с ней.
   Переключатели: любые элементы с атрибутом data-theme-set="auto|dark|light" (см. .theme-switch в stenogramma.css).
   Из кода: stTheme.get() / stTheme.set("light"); событие document "themechange". */
(function () {
  var KEY = "theme";
  var root = document.documentElement;
  var def = root.getAttribute("data-default-theme") || "dark";
  var mq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function pref() {
    try { return localStorage.getItem(KEY) || def; } catch (e) { return def; }
  }
  function apply(p) {
    if (p !== "auto" && p !== "dark" && p !== "light") p = def;
    // на время смены темы отключаем плавные переходы — тема меняется мгновенно и целиком
    root.classList.add("st-no-anim");
    root.setAttribute("data-theme-pref", p);
    root.setAttribute("data-theme", p === "auto" ? (mq && mq.matches ? "dark" : "light") : p);
    setTimeout(function () { root.classList.remove("st-no-anim"); }, 60);
  }

  apply(pref());
  if (mq && mq.addEventListener) {
    mq.addEventListener("change", function () { if (pref() === "auto") apply("auto"); });
  }

  window.stTheme = {
    get: pref,
    set: function (p) {
      try { localStorage.setItem(KEY, p); } catch (e) {}
      apply(p);
      document.dispatchEvent(new CustomEvent("themechange", { detail: p }));
    }
  };

  document.addEventListener("click", function (e) {
    var el = e.target && e.target.closest ? e.target.closest("[data-theme-set]") : null;
    if (el) window.stTheme.set(el.getAttribute("data-theme-set"));
  });
})();
