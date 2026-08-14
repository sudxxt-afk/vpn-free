# Zaza VPN

Zaza VPN is a control plane and Telegram bot for free personal VPN subscriptions. It automatically processes public GitHub configuration files, health-checks parsed endpoints and exposes only active, highly rated configurations through device-scoped subscription URLs.

The bot's primary flow is a personal Zaza VPN connection page (`/connect?token=…`). It copies the subscription link and guides the user through importing it into **HAPP**, the primary client. HAPP receives the Wi-Fi and LTE auto-connect entries first, followed by the selected servers.

It is **not** a VPN data-plane and does not deploy VPN servers. When sources are third-party public configurations, a copied imported configuration cannot be revoked at its server; device slots protect only the subscription URLs.

## Start

1. Copy `.env.example` to `.env` and replace every secret.
2. Set `TELEGRAM_BOT_TOKEN`, `WEB_APP_BASE_URL`, and add the bot as an administrator to every required channel.
3. Run `docker compose up --build`.
4. Open `http://localhost:5173`; use `INITIAL_ADMIN_LOGIN` and `INITIAL_ADMIN_PASSWORD`.

For production, `WEB_APP_BASE_URL` must be the public **HTTPS** address of the frontend. It is embedded in the bot QR code and button. A device token is a credential: do not put it in analytics, logs, or third-party widgets.

## User connection flow

1. In Telegram, the user taps **«Получить VPN»**.
2. The bot sends a small QR code and **«Открыть Zaza VPN»**.
3. The Zaza page copies the device-scoped subscription and sends the user to the HAPP download page.
4. In HAPP the user chooses **«+» → «Импорт по ссылке»**, pastes the copied value, and connects.

HAPP is the primary client. Its published instructions describe importing a copied subscription link, so the project uses that reliable flow rather than inventing an unverified `happ://` deep link.

The first source must be a public GitHub `blob` or `raw.githubusercontent.com` URL to a text file containing supported URI configuration lines.

## Supported URI protocols

VLESS, Shadowsocks, Trojan, VMess, Hysteria2 and TUIC.

## Schedules

- GitHub source refresh: every 40 minutes.
- Active-node health checks: every 2 minutes by default. A node is published only after two independent successful Xray-backed HTTP checks; speed affects ranking, not availability.
- Telegram membership revalidation: every 12 hours.

## Telegram operator panel

The web owner manages administrators in the **Administrators** section. Bind an existing bot user's Telegram ID or `@username`, choose the `owner` or `admin` role, and enable support access. The user must have started the bot before a username can be resolved.

Bound administrators open the operator panel with `/admin`. It includes support tickets, user lookup and blocking, pool/source status, manual source refreshes, and durable segmented broadcasts. Broadcasts accept Telegram HTML formatting, either text or one photo, and up to six configurable URL buttons. Drafts survive bot restarts in Redis, repeated confirmation is idempotent, and a draft can be sent to administrators as a safe test before the worker delivers it from the PostgreSQL-backed queue with live progress counters.

The web analytics section shows a strict `/start → VPN issued → landing opened → HAPP import started → subscription used` funnel and 14 daily acquisition cohorts with exact-day D0, D1, D3, and D7 retention.

## Voluntary donations

The bot menu and `/donate` command open voluntary project support. Telegram Stars are available with preset and custom amounts. A donation is recorded only after Telegram sends `successful_payment`; repeated delivery of the same payment event is idempotent. `/terms` explains the voluntary nature of support and `/paysupport` opens a support ticket for payment questions. Donations never unlock VPN features.

TON support is optional. Set `TON_DONATION_ADDRESS` to a public TON wallet address to enable the button and the TON Connect page. `TONCENTER_BASE_URL` defaults to TON Center API v2; `TONCENTER_API_KEY` is optional but recommended for production limits. The backend confirms an inbound transaction by its unique comment, full expected amount, freshness, and transaction hash before recording it. Never store or provide a wallet seed phrase or private key to this service.

The web analytics page reports donation-section opens, unique supporters, Stars totals, and verified TON totals.
