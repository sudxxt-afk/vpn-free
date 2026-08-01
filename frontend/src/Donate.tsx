import { useEffect, useState } from "react";
import { beginCell } from "@ton/core";
import { TonConnectButton, TonConnectUIProvider, useTonConnectUI, useTonWallet } from "@tonconnect/ui-react";
import { request } from "./api";
import "./donate.css";

type DonationSession = {
  status: "pending" | "paid" | "expired";
  enabled: boolean;
  recipient: string | null;
  amount_nano: number | null;
  amount_ton: number | null;
  reference: string | null;
  tx_hash: string | null;
  presets: number[];
};

function commentPayload(value: string) {
  const bytes = beginCell().storeUint(0, 32).storeStringTail(value).endCell().toBoc();
  let binary = "";
  bytes.forEach((byte: number) => { binary += String.fromCharCode(byte); });
  return window.btoa(binary);
}

function TonDonation() {
  const token = new URLSearchParams(window.location.search).get("token") || "";
  const [tonConnect] = useTonConnectUI();
  const wallet = useTonWallet();
  const [session, setSession] = useState<DonationSession | null>(null);
  const [amount, setAmount] = useState("1");
  const [busy, setBusy] = useState(false);
  const [canRetry, setCanRetry] = useState(false);
  const [message, setMessage] = useState("Загрузка безопасной сессии…");
  const [error, setError] = useState("");

  useEffect(() => {
    if (!token) { setError("Ссылка доната неполная. Откройте её заново из Telegram-бота."); return; }
    request<DonationSession>(`/donations/${encodeURIComponent(token)}`)
      .then((value) => { setSession(value); setMessage(""); })
      .catch((reason) => { setError(reason instanceof Error ? reason.message : "Сессия не найдена"); setMessage(""); });
  }, [token]);

  const verify = async () => {
    const value = await request<DonationSession>(`/donations/${encodeURIComponent(token)}/verify`, { method: "POST" });
    setSession(value);
    return value.status === "paid";
  };

  const pay = async () => {
    if (!wallet || !session?.enabled) return;
    let transactionSubmitted = false;
    setBusy(true); setCanRetry(false); setError(""); setMessage("Готовим транзакцию…");
    try {
      const prepared = session.reference ? session : await request<DonationSession>(`/donations/${encodeURIComponent(token)}/prepare`, {
        method: "POST", body: JSON.stringify({ amount }),
      });
      if (!prepared.recipient || !prepared.amount_nano || !prepared.reference) throw new Error("Сессия TON настроена неправильно");
      setSession(prepared); setMessage("Подтвердите перевод в кошельке…");
      await tonConnect.sendTransaction({
        validUntil: Math.floor(Date.now() / 1000) + 300,
        network: "-239",
        from: wallet.account.address,
        messages: [{
          address: prepared.recipient,
          amount: String(prepared.amount_nano),
          payload: commentPayload(prepared.reference),
        }],
      });
      transactionSubmitted = true;
      setMessage("Транзакция отправлена. Ждём подтверждение сети…");
      for (let attempt = 0; attempt < 20; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 3000));
        if (await verify()) { setMessage(""); return; }
      }
      setMessage("Транзакция ещё обрабатывается. Бот пришлёт подтверждение автоматически.");
    } catch (reason) {
      const text = reason instanceof Error ? reason.message : "Не удалось отправить TON";
      setError(/reject|cancel/i.test(text) ? "Перевод отменён в кошельке." : text);
      setCanRetry(!transactionSubmitted);
      setMessage("");
    } finally { setBusy(false); }
  };

  if (session?.status === "paid") return <main className="donate-page"><section className="donate-card success">
    <span className="donate-logo">Z</span><p className="donate-kicker">TRANSACTION CONFIRMED</p><h1>Спасибо за поддержку!</h1>
    <p>Получено <strong>{session.amount_ton} TON</strong>. Донат пойдёт на работу серверов и развитие Zaza VPN.</p>
    <a className="donate-primary" href="https://t.me/zazaaVPN_bot">Вернуться в Telegram</a>
  </section></main>;

  return <main className="donate-page"><section className="donate-card">
    <div className="donate-head"><span className="donate-logo">Z</span><TonConnectButton /></div>
    <p className="donate-kicker">ZAZA VPN · VOLUNTARY SUPPORT</p><h1>Поддержать в TON</h1>
    <p className="donate-lead">Средства идут на серверы, проверку VPN-конфигураций и развитие проекта. Донат не открывает платные функции.</p>
    {!session?.enabled && !message && <div className="donate-notice">TON-донаты пока не настроены. Используйте Telegram Stars в боте.</div>}
    {session?.enabled && <>
      <div className="donate-presets">{session.presets.map((value) => <button key={value} className={amount === String(value) ? "active" : ""} onClick={() => setAmount(String(value))}>{value} TON</button>)}</div>
      <label className="donate-custom">Своя сумма TON<input inputMode="decimal" value={amount} disabled={Boolean(session.reference)} onChange={(event) => setAmount(event.target.value.replace(",", "."))} /></label>
      {!wallet && <div className="donate-notice">Сначала подключите TON-кошелёк кнопкой вверху. Сервис никогда не запрашивает seed-фразу.</div>}
      <button className="donate-primary" disabled={!wallet || busy || (Boolean(session.reference) && !canRetry)} onClick={pay}>{busy ? "Проверяем…" : canRetry ? "Повторить перевод" : session.reference ? "Транзакция отправлена" : `Отправить ${amount || "—"} TON`}</button>
      {session.reference && session.status === "pending" && <button className="donate-secondary" disabled={busy} onClick={async () => { setBusy(true); setError(""); try { await verify(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Ошибка проверки"); } finally { setBusy(false); } }}>Проверить транзакцию</button>}
    </>}
    {message && <p className="donate-status">{message}</p>}{error && <p className="donate-error">{error}</p>}
    <small>Перевод проходит напрямую между вашим кошельком и публичным кошельком проекта через TON Connect.</small>
  </section></main>;
}

export function DonatePage() {
  return <TonConnectUIProvider manifestUrl={`${window.location.origin}/tonconnect-manifest.json`}><TonDonation /></TonConnectUIProvider>;
}
