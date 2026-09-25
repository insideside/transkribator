// Транскрибатор — десктоп-виджет для Übersicht (https://tracesof.net/uebersicht/).
// Шапка: индикатор + название + переключатель тем + тумблер вкл/выкл сервера.
// Тело: текущая обработка (название, этап, прогресс, очередь) и кнопка «Открыть».
// Установка: bash macos-widget/install.sh (подставит путь к папке приложения)

import { run } from "uebersicht";

// ── пути/порт (при необходимости поправьте) ──
const PROJ = "__PROJECT_DIR__"; // подставляет macos-widget/install.sh
const PY = PROJ + "/.venv/bin/python";
const LOG = PROJ + "/data/logs/server.out";
const PORT = 8770;
const LOCAL = "http://127.0.0.1:" + PORT;

export const refreshFrequency = 2000;

export const command = `
S=$(/usr/bin/curl -s --max-time 1 ${LOCAL}/api/health)
if [ -n "$S" ]; then /usr/bin/printf '{"running":true,"health":%s}' "$S"; else /usr/bin/printf '{"running":false}'; fi
`;

const startApp = () =>
  run(`/bin/mkdir -p "${PROJ}/data/logs" && cd "${PROJ}" && /usr/bin/nohup "${PY}" run.py --no-browser >> "${LOG}" 2>&1 &`);
// SIGTERM: сервер корректно завершится; прерванная задача вернётся в очередь при следующем запуске
const stopApp = () =>
  run(`/usr/sbin/lsof -ti tcp:${PORT} -sTCP:LISTEN | /usr/bin/xargs kill 2>/dev/null`);
const openApp = () => run(`/usr/bin/open ${LOCAL}`);

// ── темы ──
const THEMES = ["auto", "dark", "light", "transparent"];
const readTheme = () => {
  try { return localStorage.getItem("trWidgetTheme") || "auto"; } catch (e) { return "auto"; }
};
const applyTheme = (t) => {
  try { localStorage.setItem("trWidgetTheme", t); } catch (e) {}
  const el = document.getElementById("tr-widget-root");
  if (el) el.className = "tr-root theme-" + t;
};
const cycleTheme = () => {
  const cur = readTheme();
  applyTheme(THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length]);
};

const STAGES = { decode: "Чтение файла", transcribe: "Распознавание", diarize: "Разделение по говорящим", done: "Завершение" };
const I_EXT = "M14 3v2h3.59l-9.83 9.83 1.41 1.41L19 6.41V10h2V3zM19 19H5V5h7V3H5a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7h-2z";

const queuedText = (n) => {
  const m10 = n % 10, m100 = n % 100;
  const w = m10 === 1 && m100 !== 11 ? "файл" : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? "файла" : "файлов";
  return `ещё ${n} ${w} в очереди`;
};

export const render = ({ output }) => {
  let data = {};
  try { data = JSON.parse(output); } catch (e) {}
  const running = !!data.running;
  const cur = data.health && data.health.current;
  const queued = (data.health && data.health.queued) || 0;
  const theme = readTheme();
  const pct = cur ? Math.round((cur.progress || 0) * 100) : 0;
  return (
    <div id="tr-widget-root" className={"tr-root theme-" + theme}>
      <div className="tr-header">
        <span className={"tr-dot " + (running ? (cur ? "busy" : "on") : "off")} />
        <span className="tr-name" title="Открыть транскрибатор"
          onClick={() => running ? openApp() : startApp()}>
          Транскрибатор
        </span>
        <span className="tr-theme" title={"Тема: " + theme} onClick={cycleTheme} />
        <div className={"tr-toggle " + (running ? "on" : "off")}
          title={running ? (cur ? "Выключить (текущая обработка продолжится после следующего запуска)" : "Выключить") : "Включить"}
          onClick={() => (running ? stopApp() : startApp())}>
          <span className="tr-knob" />
        </div>
      </div>

      {running && cur ? (
        <div className="tr-job">
          <div className="tr-job-title" title={cur.title}>{cur.title}</div>
          <div className="tr-job-meta">
            <span>{STAGES[cur.stage] || "Обработка"}</span>
            <span>{pct}%</span>
          </div>
          <div className="tr-bar"><div style={{ width: pct + "%" }} /></div>
          {queued > 0 ? <div className="tr-queue">{queuedText(queued)}</div> : null}
        </div>
      ) : null}

      <div className="tr-body">
        {running ? (
          <button className="tr-open" title="Открыть транскрибатор в браузере" onClick={() => openApp()}>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d={I_EXT} /></svg>
            <span>{cur ? "Открыть" : queued ? "Открыть · " + queuedText(queued) : "Открыть транскрибатор"}</span>
          </button>
        ) : (
          <div className="tr-off">Выключено — нажмите тумблер</div>
        )}
      </div>
    </div>
  );
};

export const className = `
  top: 350px; left: 40px;
  font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
  -webkit-font-smoothing: antialiased;

  .tr-root {
    --bg: rgba(23, 26, 33, 0.92);
    --fg: #e6e8ee;
    --muted: rgba(230, 232, 238, 0.55);
    --accent: #e5533d;
    --border: rgba(255, 255, 255, 0.10);
    --btn: rgba(255, 255, 255, 0.10);
    --btn-hover: rgba(255, 255, 255, 0.16);

    width: 270px; box-sizing: border-box;
    padding: 14px 16px 16px; border-radius: 16px;
    background: var(--bg); color: var(--fg);
    border: 1px solid var(--border);
    box-shadow: 0 8px 30px rgba(0,0,0,0.35);
    -webkit-backdrop-filter: blur(20px); backdrop-filter: blur(20px);
    user-select: none;
  }
  .tr-root.theme-light {
    --bg: rgba(250,250,252,0.95); --fg:#1c1c20; --muted: rgba(28,28,32,0.5); --accent: #b3261e;
    --border: rgba(0,0,0,0.08); --btn: rgba(0,0,0,0.06); --btn-hover: rgba(0,0,0,0.12);
  }
  .tr-root.theme-transparent {
    --bg: rgba(0,0,0,0.18); --fg:#fff; --muted: rgba(255,255,255,0.7);
    --border: rgba(255,255,255,0.18); --btn: rgba(255,255,255,0.14); --btn-hover: rgba(255,255,255,0.25);
    text-shadow: 0 1px 3px rgba(0,0,0,0.5); box-shadow: none;
  }
  @media (prefers-color-scheme: light) {
    .tr-root.theme-auto {
      --bg: rgba(250,250,252,0.95); --fg:#1c1c20; --muted: rgba(28,28,32,0.5); --accent: #b3261e;
      --border: rgba(0,0,0,0.08); --btn: rgba(0,0,0,0.06); --btn-hover: rgba(0,0,0,0.12);
    }
  }

  .tr-header { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }

  .tr-dot { width: 8px; height: 8px; border-radius: 50%; flex: 0 0 auto; }
  .tr-dot.on   { background: #34c759; box-shadow: 0 0 8px rgba(52,199,89,0.8); }
  .tr-dot.busy { background: var(--accent); box-shadow: 0 0 8px var(--accent); animation: tr-pulse 1.4s ease-in-out infinite; }
  .tr-dot.off  { background: var(--muted); }
  @keyframes tr-pulse { 50% { opacity: 0.35; } }

  .tr-name { font-size: 13px; font-weight: 600; letter-spacing: 0.2px;
    flex: 1 1 auto; min-width: 0; cursor: pointer; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .tr-name:hover { color: var(--accent); }

  .tr-theme { width: 14px; height: 14px; border-radius: 50%; flex: 0 0 auto; cursor: pointer;
    background: conic-gradient(var(--accent), var(--fg), var(--muted), var(--accent));
    border: 1px solid var(--border); opacity: 0.85; }
  .tr-theme:hover { opacity: 1; transform: scale(1.1); }

  .tr-toggle { width: 40px; height: 24px; border-radius: 13px; flex: 0 0 auto;
    cursor: pointer; position: relative; background: var(--btn);
    border: 1px solid var(--border); transition: background 0.2s; }
  .tr-toggle.on { background: var(--accent); border-color: transparent; }
  .tr-knob { position: absolute; top: 2px; left: 2px; width: 18px; height: 18px;
    border-radius: 50%; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.3);
    transition: left 0.2s; }
  .tr-toggle.on .tr-knob { left: 19px; }

  .tr-job { margin-bottom: 12px; }
  .tr-job-title { font-size: 13px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .tr-job-meta { display: flex; justify-content: space-between; gap: 8px; font-size: 12px; color: var(--muted); margin: 3px 0 6px; }
  .tr-job-meta span:first-child { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
  .tr-bar { height: 4px; border-radius: 2px; background: var(--btn); overflow: hidden; }
  .tr-bar > div { height: 100%; background: var(--accent); transition: width 0.6s; }
  .tr-queue { font-size: 12px; color: var(--muted); margin-top: 6px; }

  .tr-body { display: flex; }
  .tr-open {
    display: inline-flex; align-items: center; justify-content: center; gap: 8px;
    flex: 1 1 auto; min-width: 0; height: 40px; padding: 0 14px;
    border: none; cursor: pointer; color: var(--fg);
    background: var(--btn); border-radius: 10px;
    font-size: 13px; font-weight: 500;
    transition: background 0.15s, transform 0.1s;
  }
  .tr-open span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .tr-open svg { flex: 0 0 auto; }
  .tr-open:hover { background: var(--btn-hover); }
  .tr-open:active { transform: scale(0.98); }
  .tr-off { flex: 1 1 auto; height: 40px; display: flex; align-items: center;
    justify-content: center; color: var(--muted); font-size: 12px; }
`;
