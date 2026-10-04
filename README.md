# Shop Billing

One shared billing application for the Windows desktop, shop server, and phone browsers. The desktop is a native window using the same authenticated API and UI as browsers. Old `app/` and nested `Billing_App/` launch paths forward to this implementation.

## Windows: one command

Install **Python 3.11 (64-bit)** and **Git for Windows** first. In **Git Bash**:

```bash
git clone https://github.com/Ravi9shaw/Billing_App.git && cd Billing_App && bash setupapp.sh
```

For an existing checkout: `bash setupapp.sh`, or double-click `setupapp.bat` in File Explorer. The setup asks for company name, admin password and optional Meta WhatsApp credentials, then optional company/invoice details. Blank optional fields stay optional. It installs into `.venv`, checks errors, and can be rerun after an interrupted dependency install. It **never deletes or resets the shop database**.

- `startapp.bat`: desktop window. It starts the local server if necessary.
- `startserver.bat`: standalone server for the shop host.
- `build_windows.bat`: builds `dist\ClothShopBilling.exe`. Use Windows to build a Windows EXE.
- A copied EXE has a first-run company/password setup dialog. The backend is bundled explicitly, including its dependencies, web UI and Qt WebEngine support. Keep enough free temporary disk space for the onefile extraction.

Closing a desktop window **leaves the server running** so phone tills are not disconnected. For maintenance, stop the standalone server with Ctrl+C; for a detached Windows server, close its matching `ClothShopBilling.exe --run-server` / Python server process in Task Manager. Stop server processes before restoring data or upgrading. Start `startserver.bat` through Windows Task Scheduler at login if the host should start automatically; the host must remain awake.

## Multiple users / computers

Keep one host computer and one local database file. Other desktops set `BILLING_SERVER_URL=http://<shop-host-ip>:5000` in their user-local `.env`; browsers open that same URL. Remote desktop clients do not open SQLite. Do not put the database on SMB/OneDrive/network shares or run old application versions against it.

Allow the selected port on the host's **private** Windows firewall profile. Keep the service on the shop's trusted network. For access beyond that network, deploy HTTPS with a reverse proxy and set `BILLING_COOKIE_SECURE=true`; this release does not install certificates or expose the host to the internet.

SQLite uses WAL, full synchronous durability, bounded write waits and short transactions. Concurrent checkout and payments are serialized at the database write boundary; reads use consistent snapshots. This serves a single shop host with connected tills, rather than multiple independent database hosts. Larger multi-host deployments need a database migration and measured capacity planning.

## Employee and admin

Every new session starts with the employee/admin choice. Employees can select catalog items/quantities, add optional customer information, complete bills, print and share them. They cannot change catalog prices, discounts, taxes, inventory, records or admin reports through the API.

Admin login uses the configured password (stored as a salted scrypt hash). On New Bill, **Switch to admin** requests that password; **Switch to employee** removes admin access. Admin manages catalog, customers, tax modes, discounts, sales edits/voids, payments, expenses, messages and backups. Sessions expire after eight hours and the browser preserves a draft on connection/session errors. Sign out on shared devices. Role selection is not individual employee identity tracking.

## Accurate billing

- GST added, GST included, or without GST; interstate IGST is an admin choice. Configure the default mode/rate for your own catalog.
- Totals use Decimal and per-line cent rounding on the server. Client-supplied totals are not authoritative.
- Price/catalog versions and stock edit preconditions prevent stale edits overwriting intervening changes.
- Checkout and payment retries reuse request keys. Invoice sequence numbers remain allocated when a bill is voided.
- Stock deductions are recorded explicitly, so a sale at zero recorded stock can be reversed without inventing stock.
- Saved invoices contain customer/company/tax snapshots. Receipts use actual payment totals and outstanding balances.
- Void and edit history is retained in the activity log, including original payment and line records.

The reference invoice layout is implemented in `billing/invoice.py`: company heading, customer and transport details, item/HSN/quantity/rate/amount table, totals/tax, amount in words, bank details and signatures. Browser printing and PDF generation support multiple pages. Logo artwork from the sample was not copied; sample billing/bank values are not embedded.

## Existing production data and recovery

Default live database: `%USERPROFILE%\.cloth_shop_billing\cloth_shop.db` (Linux: `~/.cloth_shop_billing/cloth_shop.db`). Configuration is `.env` beside it. Logs, backups and campaign uploads are in that same writable data directory. `.env.example` documents environment overrides; environment values win over the file. Secrets are never returned by the API or included in an EXE.

Before a legacy schema migration, the app creates and integrity-checks a SQLite backup. Migrations preserve optional values and historical amounts. The old repeated “mark everything paid” migration is removed. Only a database that never had a payments table receives the one-time pre-payment-era conversion.

Some old data cannot be inferred reliably. **Backup & activity → Review records** lists the previous buggy migration payments and bill lines whose actual stock deduction is unknown. An admin must use actual records to correct these, with a required explanation and retained audit history. Unknown historical stock prevents automated voiding/editing until reconciled. No records are silently assumed to be unpaid or assigned a guessed stock correction.

Automatic online backups run after changes, at most once per configured interval (60 seconds by default), plus graceful shutdown. The latest 30 automatic backups are retained by default; manual, pre-migration and pre-restore copies are retained separately. Backup failure appears in the admin status screen. Live committed transactions remain in the SQLite database/WAL between backups. Copy backups **and your `.env` / uploads** to a separate device for hardware-failure recovery; a same-disk copy alone cannot survive a failed drive.

```bash
# Use .venv\Scripts\python.exe on Windows; .venv/bin/python on Linux.
python -m billing.maintenance backup
python -m billing.maintenance check
# STOP the host server first. Restore refuses while its process lock is held.
python -m billing.maintenance restore "path/to/verified-backup.db" --confirm
```

Restore verifies integrity, makes a safety copy of the current database, restores through SQLite's backup API, and applies supported versioned migrations. The application process lock coordinates supported server/restore commands; external SQLite tools or old versions must not write concurrently. Test upgrades against a copy of your real database before replacing the shop installation.

## WhatsApp

No messages are sent during setup. Without API credentials, each receipt offers Download PDF and manual Share PDF (native share where available, otherwise download and attach in WhatsApp).

The included automated provider is **Meta WhatsApp Cloud API**; it is disabled by default. A token alone is not sufficient. Configure `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, the currently supported `WHATSAPP_API_VERSION`, language and an approved invoice template. The invoice template must have a **document header** and **one body text parameter** (invoice number). Campaign templates must have an **image header** and **one body text parameter** (your update text). Create/approve these in your Meta account before sending. See [Meta's message API reference](https://developers.facebook.com/docs/whatsapp/cloud-api/reference/messages).

Customers must have a valid international phone number and recorded WhatsApp permission. Admin can withdraw permission on the customer profile. Admin uploads a PNG/JPG, selects an approved template, reviews recipient count, and queues an offer/new-stock update to opted-in customers. The persistent outbox resumes queued work after a restart and rechecks consent before sending.

“Accepted” is provider acceptance, **not delivery confirmation**. Rejected/uncertain sends remain visible. Ambiguous requests are not automatically retried because the provider may already have received them. Delivery webhooks and other provider adapters are not implemented. There is no silent fallback to another provider or automatic send to customers without permission.

## Development and verification

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m billing --setup
.venv/bin/python -m billing --run-server
```

Tests use temporary synthetic databases: legacy migrations, restart durability, optional fields, concurrent checkout/payments, idempotency, stock reversal, permissions, stale edits, backups and invoice generation. Validate native Windows onefile launch, printing, firewall and display scaling on the target shop machine before rollout. The Linux build smoke is not a substitute for Windows acceptance.

### Receiving UPI payments

Setup (including the EXE first-run dialog) asks for an optional **receiving UPI ID**, for example `yourshop@bank`. This is saved as `UPI_ID` in the user-local `.env`. Use the actual UPI address linked to the receiving account; `COMPANY_PHONE` is only a contact number and `BANK_ACCOUNT` is printed bank information.

New invoices save this destination with the company snapshot. When a saved invoice has an outstanding balance, its printable HTML and PDF include a UPI QR for that balance. Fully paid invoices and invoices without a valid configured UPI ID have no payment QR. To collect after bill creation, enter the amount already received in **Paid now** (zero if unpaid), complete the bill, and open/print its QR. After confirming receipt in your bank/payment app, an admin records the payment under **Sales & payments**. Scanning does not automatically mark a bill paid. Existing printed copies cannot update after subsequent payments, so use a fresh receipt for the current balance.

The payment URI uses the UPI address, payee name, INR amount and invoice note described in [Google Pay's UPI intent reference](https://developers.google.com/pay/india/api/android/in-app-payments). Account validity and acceptance must be checked in the recipient's payment app before shop use. No live payment is made by setup or by the automated tests.

## Reports, customer balances and date filters

Balances shows all named-customer totals, with server-side name/phone search, outstanding-only filtering and pagination. Each customer profile has recorded purchase visits (one saved invoice counts as one visit), contact details, balance, sales history and open wishlist requests. Billing search fills the selected customer's saved name, phone and address without overwriting the profile.

Sales, Expenses and Overview share calendar controls: all time, today, last 7/30 days, a chosen month, last month, the last three **complete** calendar months, a chosen year and custom inclusive dates. Month/year controls include the full calendar boundaries, including leap years. Search supports invoice/customer names and phones; expenses also filter by category. CSV links export **every matching row**, regardless of pagination, using the same applied filters as the screen. Selecting September exports September only. Backup & activity's complete database export remains a separately labelled full-data backup export.

Overview shows bill count, pieces, invoiced sales, payments collected, current outstanding and expenses. Daily bars include zero-sales days for periods up to 94 days; longer periods use monthly bars. Click a bar to inspect its sales. Category quantities and item rankings by quantity/net sales, best customers and common open wishlist demand are available below. Item/category net sales use the stored discounted taxable line value (excluding GST); legacy tax details may be incomplete. New sales snapshot the category name; older missing snapshots use the current category. Common wishlist demand groups exact descriptions ignoring case/surrounding spaces; it does not infer that differently worded requests mean the same item. Wishlist demand is current, independent of the selected sales dates.

## Local Wi-Fi server and desktop graphics

The desktop automatically starts its local backend unless `BILLING_SERVER_URL` points to another host. Use **Local server / connect device** at the top of the app for the running server URL and connection QR. Other devices must be on the same reachable Wi-Fi/network and the host's configured private-network firewall port must be allowed. `startserver.bat` or `python -m billing --run-server` starts the backend without the desktop and prints the address. `BILLING_PUBLIC_URL` can specify a fixed host/reverse-proxy address; the app cannot configure the router/firewall for you. The connection QR opens the shop; it is separate from the invoice payment QR.

Desktop software mode sets `QT_QUICK_BACKEND=software`, `QSG_RHI_BACKEND=opengl`, `QT_OPENGL=software` and Chromium `--disable-gpu --disable-features=Vulkan` before importing Qt. Software mode overrides inherited Vulkan/Qt backend choices and preserves unrelated Chromium flags. Set `BILLING_SOFTWARE_RENDERING=false` to manage these rendering settings yourself. Vulkan error `-9` means an incompatible graphics driver/runtime; `GPUInfo not initialized` is a graphics-probe warning. These messages do not diagnose the database or an HTTP “Not Found” response. If the embedded view still fails on a particular machine, use **Open in browser** in the native toolbar. See [Qt WebEngine debugging](https://doc.qt.io/qt-6/qtwebengine-debugging.html) and [Qt Quick rendering adaptations](https://doc.qt.io/qt-6/qtquick-visualcanvas-adaptations.html).

Setup also asks for `UPI_PHONE` as an optional reference. It cannot look up a receiving bank account from that number offline; copy the actual `UPI_ID` from your payment app for the interoperable payment URI. A payment QR is shown only for an unpaid saved balance and never confirms payment automatically. Meta's official WhatsApp Cloud API remains optional (`WHATSAPP_PROVIDER=disabled` until configured).


## Admin configuration and server recovery

Sign in as **Admin → Configuration** to reenter company/invoice/bank information, receiving UPI ID, GST defaults, optional Meta credentials, server access/port and backup settings. Blank optional fields stay optional. A blank token keeps the existing secret; use the separate checkbox to remove it. Password changes require the current admin password and sign existing sessions out after restart. Fields explicitly set by the operating-system environment are labelled and locked; edit those environment values at the host instead.

**Save & restart backend** saves the user-local configuration atomically, makes a verified database backup, finishes active requests and starts a fresh backend process. Existing invoice snapshots retain their original company/payment information. The database location is shown read-only to help identify the active data folder. Configuration editing and restarting are admin-only on both desktop and web. Managed restart requires the supplied launcher (`python -m billing --run-server`, `startserver.bat` or the desktop), not an externally launched Uvicorn instance.

The desktop has native **Start / reconnect server**, **Restart local server**, **Open in browser** and **Open logs** controls even when its web page is unavailable. Start/reconnect rereads the local configuration and starts an offline local host. Restart requires the admin password. After a port change, use Start/reconnect on the desktop or the new address offered in the browser. Other tills must use that updated address too.

### Updating from the previous release

Older detached hosts can keep old Python routes while serving updated web files, causing customer profiles, Overview and connection controls to return “Not Found”. This release checks the API version, available capabilities, running build and local database identity before loading the desktop UI; the browser also rejects an incompatible backend.

**For the first upgrade from API version 2, stop the old host once**: Ctrl+C in its server terminal, or end the matching billing `--run-server` process in Task Manager. Closing only the desktop window leaves that host running. Then update this checkout and run `python app/main.py` with the installed environment, or `startapp.bat`. Do not delete `.cloth_shop_billing` or create a replacement database. Subsequent managed hosts can restart through the native toolbar or **Local server / connect device → Restart backend**. The controls report startup failures and the log location; they do not terminate unrelated processes automatically.
