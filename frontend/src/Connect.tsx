import { useEffect, useMemo, useState } from "react";
import { API } from "./api";
import heroImage from "./assets/zaza-hero.png";
import "./connect.css";

type Platform = "android" | "ios" | "desktop" | "other";

const platformNames: Record<Exclude<Platform, "other">, string> = {
  android: "Android",
  ios: "iPhone / iPad",
  desktop: "Windows / macOS / Linux",
};

function detectPlatform(): Platform {
  const agent = navigator.userAgent.toLowerCase();
  if (/android/.test(agent)) return "android";
  if (/iphone|ipad|ipod/.test(agent)) return "ios";
  if (/windows|macintosh|linux/.test(agent)) return "desktop";
  return "other";
}

async function copyText(value: string) {
  try {
    await navigator.clipboard.writeText(value);
  } catch {
    const input = document.createElement("textarea");
    input.value = value;
    input.setAttribute("readonly", "");
    input.style.cssText = "position:fixed;opacity:0";
    document.body.appendChild(input);
    input.select();
    document.execCommand("copy");
    input.remove();
  }
}

export function Connect() {
  const [copied, setCopied] = useState(false);
  const [platform, setPlatform] = useState<Platform>(detectPlatform);
  const token = new URLSearchParams(window.location.search).get("token") || "";
  // VITE_API_URL is deliberately relative (/api) in production so browser API
  // calls stay same-origin. HAPP, however, receives no browser origin, so its
  // subscription URL must be absolute.
  const apiBase = useMemo(() => new URL(API, window.location.origin).toString().replace(/\/$/, ""), []);
  const subscription = token ? `${apiBase}/s/${encodeURIComponent(token)}` : "";
  // HAPP's standard deeplink accepts the subscription URL itself after /add/.
  // Passing a Base64 string makes current HAPP builds treat it as an invalid URL.
  const happLink = useMemo(() => subscription ? `happ://add/${subscription}` : "", [subscription]);
  const platformLabel = platform === "other" ? "вашего устройства" : platformNames[platform];

  const track = (eventType: "site_visit" | "happ_launch") => {
    if (!token) return;
    void fetch(`${apiBase}/events/landing`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token, event_type: eventType }),
    }).catch(() => undefined);
  };

  useEffect(() => { track("site_visit"); }, [token, apiBase]);

  const copy = async () => {
    if (!subscription) return;
    await copyText(subscription);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2200);
  };

  const launchHapp = () => {
    if (!happLink) return;
    track("happ_launch");
    void copy();
    window.location.href = happLink;
  };

  if (!token) return <main className="zaza-page zaza-error-page"><section><span className="zaza-mini-logo">Z</span><h1>Ссылка неполная</h1><p>Вернитесь в Telegram-бот и ещё раз нажмите «Получить VPN».</p><a href="https://t.me/zazaaVPN_bot">Открыть Zaza VPN в Telegram</a></section></main>;

  return <main className="zaza-page">
    <section className="zaza-hero-art" style={{ backgroundImage: `url(${heroImage})` }}>
      <div className="zaza-topline"><div className="zaza-wordmark">ZAZA <span>VPN</span></div><div className="zaza-pulse"><i /> ЛИЧНАЯ ПОДПИСКА ГОТОВА</div></div>
      <div className="zaza-hero-copy"><p className="zaza-tag">// FREE ACCESS · NO BORING SETUP</p><h1>СВОБОДНЫЙ<br /><em>ИНТЕРНЕТ.</em></h1><p>Одна личная подписка. В ней — полный список серверов Zaza VPN, который обновляется прямо внутри HAPP.</p><div className="zaza-strike"><span>01</span> ТВОЙ ПРОПУСК В СЕТЬ</div></div>
    </section>

    <section className="zaza-import-card">
      <div className="zaza-import-title"><span>⚡</span><div><p>Шаг 1 из 1</p><h2>ИМПОРТИРУЙ В HAPP</h2></div></div>
      <div className="zaza-os-block"><span>ТВОЁ УСТРОЙСТВО</span><div className="zaza-os-switch">{(["android", "ios", "desktop"] as Exclude<Platform, "other">[]).map(item => <button key={item} className={platform === item ? "active" : ""} onClick={() => setPlatform(item)}>{item === "android" ? "🤖 Android" : item === "ios" ? "🍏 iPhone" : "💻 Компьютер"}</button>)}</div><small>Выбрано: {platformLabel}</small></div>
      <div className="zaza-import-actions"><button className="zaza-open-happ" onClick={launchHapp}>🚀 ОТКРЫТЬ HAPP И ДОБАВИТЬ</button><button className="zaza-copy-link" onClick={copy}>{copied ? "✓ ССЫЛКА СКОПИРОВАНА" : "▣ СКОПИРОВАТЬ ССЫЛКУ"}</button></div>
      <p className="zaza-explainer">Нажатие откроет HAPP и передаст туда твою личную подписку. Если HAPP ещё не установлен — сначала скачай его, затем вернись сюда и нажми кнопку снова.</p>
      <a className="zaza-download" href="https://www.happ.uno/" target="_blank" rel="noreferrer">↓ СКАЧАТЬ HAPP ДЛЯ {platformLabel.toUpperCase()}</a>
    </section>

    <section className="zaza-what-inside"><p className="zaza-tag">// ЧТО ДАЛЬШЕ</p><h2>ЗАШЁЛ.<br />ВКЛЮЧИЛ. ПОГНАЛ.</h2><div className="zaza-feature-grid"><article><span>✹</span><h3>ОДНА ССЫЛКА</h3><p>Добавь её в HAPP один раз — и всё нужное будет внутри приложения.</p></article><article><span>↻</span><h3>ВСЕГДА СВЕЖО</h3><p>Приложение само подтянет актуальные настройки, когда они появятся.</p></article><article><span>⚡</span><h3>ТВОЙ ВЫБОР</h3><p>Открой HAPP, выбери подходящий сервер и включай подключение.</p></article></div></section>
    <footer className="zaza-footer"><div className="zaza-wordmark">ZAZA <span>VPN</span></div><p>Не пересылай личную ссылку другим людям.</p></footer>
  </main>;
}
