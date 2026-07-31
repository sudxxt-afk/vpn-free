import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { Activity, ArrowUpRight, Bell, Check, ChevronRight, CircleAlert, Gauge, Link2, LogOut, Menu, Plus, Radio, RefreshCw, Server, Settings2, ShieldCheck, Users, X } from "lucide-react";
import { request } from "./api";
import { Connect } from "./Connect";

type Dashboard = { active_nodes: number; quarantined_nodes: number; average_ping: number | null; active_users: number; sources_with_errors: number; required_channels: number };
type Metric = { active_nodes: number; quarantined_nodes: number; average_ping_ms: number | null; check_success_rate: number | null; created_at: string };
type Source = { id: string; name: string; github_url: string; is_enabled: boolean; last_success_at: string | null; last_error: string | null; content_hash: string | null };
type Node = { id: string; protocol: string; host: string; port: number; state: "candidate" | "active" | "degraded" | "quarantined" | "removed"; score: number; avg_latency_ms: number | null; success_checks: number; failed_checks: number; source_id: string; region: string; region_emoji: string; network_profile: "mobile" | "wifi"; network_label: string; network_emoji: string; profile_priority: number };
type Channel = { id: string; chat_id: number; title: string; username: string | null; is_active: boolean };
type PoolPolicy = { vless_reality_limit: number; vless_ws_limit: number; vless_other_limit: number; hysteria2_limit: number; tuic_limit: number; trojan_limit: number; shadowsocks_limit: number; vmess_limit: number; updated_at: string | null };

const nav = [
  { key: "overview", label: "Обзор", icon: Gauge },
  { key: "sources", label: "Источники", icon: Link2 },
  { key: "nodes", label: "Ноды", icon: Server },
  { key: "policy", label: "Подписка", icon: Settings2 },
  { key: "channels", label: "Каналы", icon: Radio },
  { key: "users", label: "Пользователи", icon: Users },
];

function formatDate(value: string | null) {
  return value ? new Intl.DateTimeFormat("ru-RU", { dateStyle: "short", timeStyle: "short" }).format(new Date(value)) : "ещё не запускался";
}

function Login({ onSuccess }: { onSuccess: () => void }) {
  const [login, setLogin] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setError("");
    try { await request("/auth/login", { method: "POST", body: JSON.stringify({ login, password }) }); onSuccess(); }
    catch (err) { setError(err instanceof Error ? err.message : "Ошибка входа"); }
  };
  return <main className="login-shell"><section className="login-card" aria-labelledby="login-title">
    <div className="brand-lockup"><span className="brand-mark">V</span><span>VECTOR</span></div>
    <p className="eyebrow">CONTROL PLANE</p><h1 id="login-title">Доступ к сети</h1><p className="muted">Управляйте источниками, качеством и условиями выдачи VPN-подписок.</p>
    <form onSubmit={submit} className="stack" aria-label="Вход в админ-панель">
      <label>Логин<input autoComplete="username" value={login} onChange={(e) => setLogin(e.target.value)} /></label>
      <label>Пароль<input type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} /></label>
      {error && <p role="alert" className="form-error"><CircleAlert size={16} />{error}</p>}
      <button className="primary" type="submit">Войти <ArrowUpRight size={17} /></button>
    </form>
    <p className="login-foot">Логин первого администратора задаётся в переменных окружения.</p>
  </section></main>;
}

export function App() {
  if (window.location.pathname.startsWith("/connect")) return <Connect />;
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [page, setPage] = useState("overview");
  const [compact, setCompact] = useState(false);
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [metrics, setMetrics] = useState<Metric[]>([]);
  const [sources, setSources] = useState<Source[]>([]);
  const [nodes, setNodes] = useState<Node[]>([]);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [policy, setPolicy] = useState<PoolPolicy | null>(null);
  const [notice, setNotice] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [summary, sourceList, nodeList, channelList, metricList, policyValue] = await Promise.all([
        request<Dashboard>("/admin/dashboard"), request<Source[]>("/admin/sources"), request<Node[]>("/admin/nodes"), request<Channel[]>("/admin/channels"), request<Metric[]>("/admin/metrics"), request<PoolPolicy>("/admin/pool-policy"),
      ]);
      setDashboard(summary); setSources(sourceList); setNodes(nodeList); setChannels(channelList); setMetrics(metricList); setPolicy(policyValue); setAuthenticated(true);
    } catch { setAuthenticated(false); }
  }, []);
  useEffect(() => { refresh(); }, [refresh]);
  const notify = (message: string) => { setNotice(message); window.setTimeout(() => setNotice(""), 3500); };
  const logout = async () => { await request("/auth/logout", { method: "POST" }); setAuthenticated(false); };
  const title = nav.find((item) => item.key === page)?.label || "Обзор";
  const issueCount = useMemo(() => sources.filter((source) => source.last_error).length, [sources]);

  if (authenticated === null) return <div className="splash"><span className="brand-mark">V</span></div>;
  if (!authenticated) return <Login onSuccess={refresh} />;
  return <div className={compact ? "app compact" : "app"}>
    <aside className="sidebar"><div className="brand-lockup"><span className="brand-mark">V</span><span className="brand-name">VECTOR</span></div>
      <nav aria-label="Основная навигация">{nav.map(({ key, label, icon: Icon }) => <button key={key} className={page === key ? "nav-item active" : "nav-item"} onClick={() => setPage(key)}><Icon size={19}/><span>{label}</span>{key === "sources" && issueCount > 0 && <b>{issueCount}</b>}</button>)}</nav>
      <div className="sidebar-bottom"><div className="system-state"><span className="pulse"/> Система работает</div><button className="nav-item" onClick={logout}><LogOut size={19}/><span>Выйти</span></button></div>
    </aside>
    <main className="content"><header><div className="headline"><button className="icon-button mobile-menu" aria-label="Открыть меню" onClick={() => setCompact(!compact)}><Menu size={20}/></button><div><p className="eyebrow">CONTROL / {page.toUpperCase()}</p><h1>{title}</h1></div></div><div className="header-actions"><button className="icon-button" aria-label="Обновить данные" onClick={() => { refresh(); notify("Данные обновлены"); }}><RefreshCw size={19}/></button><button className="icon-button" aria-label="Уведомления"><Bell size={19}/><i/></button><div className="avatar">A</div></div></header>
      {notice && <div className="toast" role="status"><Check size={17}/>{notice}</div>}
      {page === "overview" && <Overview dashboard={dashboard} metrics={metrics} sources={sources} nodes={nodes} setPage={setPage} />}
      {page === "sources" && <Sources sources={sources} onChanged={refresh} notify={notify} />}
      {page === "nodes" && <Nodes nodes={nodes} />}
      {page === "policy" && policy && <SubscriptionPolicy policy={policy} onChanged={refresh} notify={notify} />}
      {page === "channels" && <Channels channels={channels} onChanged={refresh} notify={notify} />}
      {page === "users" && <UsersPage notify={notify} />}
    </main>
  </div>;
}

function Overview({ dashboard, metrics, sources, nodes, setPage }: { dashboard: Dashboard | null; metrics: Metric[]; sources: Source[]; nodes: Node[]; setPage: (page: string) => void }) {
  const cards = [
    ["Активные ноды", dashboard?.active_nodes ?? 0, "прошли порог качества", Server, "mint"],
    ["Средний ping", dashboard?.average_ping ? `${dashboard.average_ping} мс` : "—", "по активному пулу", Activity, "blue"],
    ["Пользователи", dashboard?.active_users ?? 0, "доступ не заблокирован", Users, "violet"],
    ["В карантине", dashboard?.quarantined_nodes ?? 0, "не выдаются в подписках", ShieldCheck, "amber"],
  ] as const;
  return <section className="page-stack"><div className="hero"><div><span className="live-badge"><span className="pulse"/> LIVE NETWORK</span><h2>Пул под контролем</h2><p>Источники обновляются каждые 40 минут. Ноды оцениваются отдельно каждые 10 минут.</p></div><button className="primary" onClick={() => setPage("sources")}>Управлять источниками <ChevronRight size={17}/></button></div>
    <div className="metric-grid">{cards.map(([label, value, description, Icon, tone]) => <article className="metric-card" key={label}><div className={`metric-icon ${tone}`}><Icon size={20}/></div><p>{label}</p><strong>{value}</strong><span>{description}</span></article>)}</div>
    <div className="split-grid"><article className="panel health-panel"><div className="panel-head"><div><p className="eyebrow">QUALITY SIGNAL</p><h3>Качество сети</h3></div><span className="status-dot">{metrics.length ? "Снимки поступают" : "Ожидание данных"}</span></div><SignalChart metrics={metrics}/><div className="health-legend"><span><i className="dot mint"/>Активные: {dashboard?.active_nodes ?? 0}</span><span><i className="dot amber"/>Карантин: {dashboard?.quarantined_nodes ?? 0}</span></div></article>
      <article className="panel source-summary"><div className="panel-head"><div><p className="eyebrow">SOURCES</p><h3>Последняя активность</h3></div><button className="text-button" onClick={() => setPage("sources")}>Все источники</button></div>{sources.slice(0, 4).map((source) => <div className="source-row" key={source.id}><span className={source.last_error ? "source-status fail" : "source-status"}/><div><strong>{source.name}</strong><small>{source.last_error || `Обновлён: ${formatDate(source.last_success_at)}`}</small></div><span className={source.is_enabled ? "pill" : "pill off"}>{source.is_enabled ? "Активен" : "Выключен"}</span></div>)}{sources.length === 0 && <Empty text="Добавьте первый GitHub-источник — его конфиги будут обработаны автоматически." action="Добавить источник" onClick={() => setPage("sources")}/>}</article></div>
    <article className="panel"><div className="panel-head"><div><p className="eyebrow">TOP POOL</p><h3>Лучшие ноды</h3></div><button className="text-button" onClick={() => setPage("nodes")}>Открыть пул</button></div><NodeTable nodes={nodes.slice(0, 6)} /></article>
  </section>;
}

function SignalChart({ metrics }: { metrics: Metric[] }) {
  const usable = metrics.filter((item) => item.check_success_rate !== null);
  if (usable.length < 2) return <div className="signal-chart chart-empty" aria-label="График качества сети"><p>График появится после двух health-check запусков.</p></div>;
  const values = usable.map((item) => Math.round((item.check_success_rate || 0) * 100));
  const min = Math.min(...values); const max = Math.max(...values); const range = Math.max(max - min, 1);
  const points = values.map((value, index) => `${(index / Math.max(values.length - 1, 1)) * 600},${148 - ((value - min) / range) * 110}`).join(" ");
  return <div className="signal-chart" aria-label="График успешности проверок нод"><div className="chart-glow"/><svg viewBox="0 0 600 180" preserveAspectRatio="none"><polyline points={points} fill="none" stroke="currentColor" strokeWidth="3"/></svg><div className="chart-labels"><span>{values[0]}%</span><span>успешность health-check</span><span>{values.at(-1)}%</span></div></div>;
}

function Sources({ sources, onChanged, notify }: { sources: Source[]; onChanged: () => Promise<void>; notify: (message: string) => void }) {
  const [name, setName] = useState(""); const [url, setUrl] = useState(""); const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const add = async (event: FormEvent) => { event.preventDefault(); setBusy(true); setError(""); try { await request("/admin/sources", { method: "POST", body: JSON.stringify({ name, github_url: url }) }); setName(""); setUrl(""); await onChanged(); notify("Источник добавлен в очередь обработки"); } catch (err) { setError(err instanceof Error ? err.message : "Ошибка"); } finally { setBusy(false); } };
  const action = async (path: string, message: string) => { try { await request(path, { method: "POST" }); await onChanged(); notify(message); } catch (err) { notify(err instanceof Error ? err.message : "Ошибка операции"); } };
  return <section className="page-stack"><article className="panel add-source"><div><p className="eyebrow">PUBLIC GITHUB ONLY</p><h2>Добавить источник</h2><p>Поддерживаются ссылки GitHub `blob` и `raw` на конкретный файл. Ничего не публикуется вручную: обработка и отбор полностью автоматические.</p></div><form onSubmit={add}><label>Название<input required placeholder="Например, public pool" value={name} onChange={(e) => setName(e.target.value)} /></label><label>Ссылка на файл GitHub<input required type="url" placeholder="https://github.com/owner/repo/blob/main/configs.txt" value={url} onChange={(e) => setUrl(e.target.value)} /></label>{error && <p className="form-error"><CircleAlert size={16}/>{error}</p>}<button className="primary" disabled={busy}>{busy ? "Добавляем…" : <> <Plus size={17}/> Добавить источник</>}</button></form></article>
    <article className="panel"><div className="panel-head"><div><p className="eyebrow">AUTOMATION QUEUE</p><h3>Источники</h3></div><span className="muted">Обновление раз в 40 минут</span></div><div className="source-list">{sources.map((source) => <div className="source-card" key={source.id}><div className="source-card-title"><span className={source.last_error ? "source-status fail" : "source-status"}/><div><strong>{source.name}</strong><small>{source.github_url}</small></div><span className={source.is_enabled ? "pill" : "pill off"}>{source.is_enabled ? "Активен" : "Выключен"}</span></div><div className="source-meta"><span>Последний запуск: <b>{formatDate(source.last_success_at)}</b></span><span>Хеш: <b>{source.content_hash?.slice(0, 12) || "—"}</b></span>{source.last_error && <span className="error-text">Ошибка: {source.last_error}</span>}</div><div className="card-actions"><button onClick={() => action(`/admin/sources/${source.id}/refresh`, "Обработка источника запущена")}>Запустить сейчас</button><button onClick={async () => { try { await request(`/admin/sources/${source.id}/toggle`, { method: "PATCH" }); await onChanged(); notify(source.is_enabled ? "Источник выключен" : "Источник включён"); } catch (err) { notify(err instanceof Error ? err.message : "Ошибка"); } }}>{source.is_enabled ? "Выключить" : "Включить"}</button></div></div>)}{sources.length === 0 && <Empty text="Пока нет источников. Добавьте публичный GitHub-файл выше."/>}</div></article></section>;
}

function Nodes({ nodes }: { nodes: Node[] }) { return <section className="page-stack"><div className="page-intro"><div><p className="eyebrow">TRANSPORT-BASED SORTING</p><h2>Пул нод</h2><p>📡 Мобильный интернет: VLESS Reality и Trojan. 📶 Wi‑Fi: Hysteria2, TUIC, Shadowsocks, VMess и обычный VLESS.</p></div><div className="legend"><span><i className="dot mint"/>active</span><span><i className="dot amber"/>degraded</span><span><i className="dot red"/>quarantined</span></div></div><article className="panel"><NodeTable nodes={nodes} /></article></section>; }

function NodeTable({ nodes }: { nodes: Node[] }) { return <div className="table-wrap"><table><thead><tr><th>Регион и сеть</th><th>Протокол</th><th>Ping</th><th>Проверки</th><th>Рейтинг</th><th>Статус</th></tr></thead><tbody>{nodes.map((node) => <tr key={node.id}><td><strong>{node.region_emoji} {node.region}</strong><small>{node.network_emoji} {node.network_label} · {node.host}:{node.port}</small></td><td><span className="protocol">{node.protocol}</span></td><td>{node.avg_latency_ms ? `${Math.round(node.avg_latency_ms)} мс` : "—"}</td><td>{node.success_checks} / <span className="faded">{node.failed_checks}</span></td><td><div className="score"><span style={{ width: `${Math.min(node.score, 100)}%` }}/><b>{node.score}</b></div></td><td><span className={`state ${node.state}`}>{node.state}</span></td></tr>)}</tbody></table>{nodes.length === 0 && <Empty text="Нод ещё нет: добавьте источник и запустите обработку."/>}</div>; }

const policyFields: Array<[keyof Omit<PoolPolicy, "updated_at">, string, string]> = [
  ["vless_reality_limit", "VLESS Reality", "LTE: приоритетный анти-DPI пул"],
  ["vless_ws_limit", "VLESS WS", "WebSocket-транспорт"],
  ["vless_other_limit", "VLESS прочие", "gRPC, XHTTP, TCP/TLS и другие"],
  ["hysteria2_limit", "Hysteria2", "Высокоскоростной Wi‑Fi-пул"],
  ["tuic_limit", "TUIC", "QUIC / UDP"],
  ["trojan_limit", "Trojan", "TLS-пул"],
  ["shadowsocks_limit", "Shadowsocks", "SS и плагины"],
  ["vmess_limit", "VMess", "Legacy-пул"],
];

function SubscriptionPolicy({ policy, onChanged, notify }: { policy: PoolPolicy; onChanged: () => Promise<void>; notify: (message: string) => void }) {
  const [draft, setDraft] = useState(policy); const [busy, setBusy] = useState(false);
  useEffect(() => setDraft(policy), [policy]);
  const save = async (event: FormEvent) => { event.preventDefault(); setBusy(true); try { await request("/admin/pool-policy", { method: "PUT", body: JSON.stringify(Object.fromEntries(policyFields.map(([key]) => [key, draft[key]])) ) }); await onChanged(); notify("Лимиты пула сохранены"); } catch (err) { notify(err instanceof Error ? err.message : "Не удалось сохранить лимиты"); } finally { setBusy(false); } };
  return <section className="page-stack"><div className="page-intro"><div><p className="eyebrow">HAPP SUBSCRIPTION ORDER</p><h2>Автоподключение и лимиты</h2><p>Первые две строки подписки — реальные ноды с постоянно одинаковыми названиями. При обновлении система подставляет за ними лучший проверенный сервер.</p></div></div>
    <div className="auto-route-grid"><article className="auto-route wifi"><span className="auto-route-icon">📶</span><div><p>ПЕРВАЯ НОДА В HAPP</p><strong>Автоподключение Wi‑Fi</strong><small>лучший активный Wi‑Fi-сервер по рейтингу</small></div></article><article className="auto-route lte"><span className="auto-route-icon">📡</span><div><p>ВТОРАЯ НОДА В HAPP</p><strong>Автоподключение LTE</strong><small>лучший активный сервер для мобильной сети</small></div></article></div>
    <article className="panel"><div className="panel-head"><div><p className="eyebrow">PUBLISHED NODE LIMITS</p><h3>Сколько нод выдавать в одной подписке</h3></div><span className="muted">0 — не выдавать этот тип</span></div><form className="policy-grid" onSubmit={save}>{policyFields.map(([key, label, hint]) => <label key={key}><span>{label}<small>{hint}</small></span><input type="number" min="0" max="100" value={draft[key]} onChange={(event) => setDraft({ ...draft, [key]: Math.max(0, Math.min(100, Number(event.target.value))) })}/></label>)}<button className="primary" disabled={busy}>{busy ? "Сохраняем…" : "Сохранить лимиты"}</button></form></article></section>;
}

function Channels({ channels, onChanged, notify }: { channels: Channel[]; onChanged: () => Promise<void>; notify: (message: string) => void }) {
  const [chatId, setChatId] = useState(""); const [title, setTitle] = useState(""); const [username, setUsername] = useState(""); const [error, setError] = useState("");
  const add = async (event: FormEvent) => { event.preventDefault(); setError(""); try { await request("/admin/channels", { method: "POST", body: JSON.stringify({ chat_id: Number(chatId), title, username: username || null }) }); setChatId(""); setTitle(""); setUsername(""); await onChanged(); notify("Канал добавлен как обязательный"); } catch (err) { setError(err instanceof Error ? err.message : "Ошибка"); } };
  return <section className="page-stack"><article className="panel add-source"><div><p className="eyebrow">ACCESS GATE</p><h2>Обязательные каналы</h2><p>Если список пуст, VPN доступен сразу. Канал добавится только когда бот является в нём администратором.</p></div><form onSubmit={add} className="channel-form"><label>ID канала<input required inputMode="numeric" placeholder="-100…" value={chatId} onChange={(e) => setChatId(e.target.value)} /></label><label>Название<input required placeholder="Партнёрский канал" value={title} onChange={(e) => setTitle(e.target.value)} /></label><label>@username <input placeholder="optional" value={username} onChange={(e) => setUsername(e.target.value)} /></label>{error && <p className="form-error"><CircleAlert size={16}/>{error}</p>}<button className="primary"><Plus size={17}/>Добавить</button></form></article><article className="panel"><div className="panel-head"><div><p className="eyebrow">ACTIVE RULES</p><h3>Каналы в проверке</h3></div><span className="muted">Проверка каждые 12 часов</span></div><div className="channel-list">{channels.map((channel) => <div className="channel-row" key={channel.id}><div className="channel-icon"><Radio size={19}/></div><div><strong>{channel.title}</strong><small>{channel.username ? `@${channel.username.replace("@", "")}` : channel.chat_id}</small></div><span className={channel.is_active ? "pill" : "pill off"}>{channel.is_active ? "Требуется" : "Выключен"}</span><button className="ghost" onClick={async () => { try { await request(`/admin/channels/${channel.id}/toggle`, { method: "PATCH" }); await onChanged(); notify("Статус канала изменён"); } catch (err) { notify(err instanceof Error ? err.message : "Ошибка"); } }}>{channel.is_active ? <X size={18}/> : <Check size={18}/>}</button></div>)}{channels.length === 0 && <Empty text="Нет обязательных каналов — пользователи получают VPN без проверки подписки."/>}</div></article></section>;
}

type ManagedUser = { id: string; telegram_id: number; username: string | null; is_blocked: boolean; device_count: number; last_membership_check: string | null };
function UsersPage({ notify }: { notify: (message: string) => void }) {
  const [users, setUsers] = useState<ManagedUser[]>([]); const [loading, setLoading] = useState(true);
  const load = useCallback(async () => { setLoading(true); try { setUsers(await request<ManagedUser[]>("/admin/users")); } catch (err) { notify(err instanceof Error ? err.message : "Не удалось загрузить пользователей"); } finally { setLoading(false); } }, [notify]);
  useEffect(() => { load(); }, [load]);
  return <section className="page-stack"><div className="page-intro"><div><p className="eyebrow">ACCESS CONTROL</p><h2>Пользователи</h2><p>Блокировка отключает выдачу подписки. Импортированные внешние конфиги остаются вне контроля сервиса.</p></div><button className="primary" onClick={load}><RefreshCw size={16}/>Обновить</button></div><article className="panel"><div className="panel-head"><div><p className="eyebrow">TELEGRAM ACCOUNTS</p><h3>Доступ и ячейки устройств</h3></div><span className="muted">{users.length} записей</span></div><div className="table-wrap"><table><thead><tr><th>Пользователь</th><th>Telegram ID</th><th>Ячейки</th><th>Проверка каналов</th><th>Доступ</th><th/></tr></thead><tbody>{users.map((user) => <tr key={user.id}><td><strong>{user.username ? `@${user.username}` : "Без username"}</strong></td><td>{user.telegram_id}</td><td>{user.device_count} / 2</td><td>{formatDate(user.last_membership_check)}</td><td><span className={user.is_blocked ? "state quarantined" : "state active"}>{user.is_blocked ? "blocked" : "active"}</span></td><td><button className="ghost" aria-label={user.is_blocked ? "Разблокировать" : "Заблокировать"} onClick={async () => { try { await request(`/admin/users/${user.id}/block`, { method: "PATCH" }); await load(); notify(user.is_blocked ? "Пользователь разблокирован" : "Пользователь заблокирован"); } catch (err) { notify(err instanceof Error ? err.message : "Ошибка"); } }}>{user.is_blocked ? <Check size={17}/> : <X size={17}/>}</button></td></tr>)}</tbody></table>{!loading && users.length === 0 && <Empty text="Пользователи появятся после первого запуска Telegram-бота."/>}{loading && <div className="empty"><p>Загрузка…</p></div>}</div></article></section>;
}

function Empty({ text, action, onClick }: { text: string; action?: string; onClick?: () => void }) { return <div className="empty"><div className="empty-icon"><ShieldCheck size={22}/></div><p>{text}</p>{action && <button className="text-button" onClick={onClick}>{action}</button>}</div>; }
