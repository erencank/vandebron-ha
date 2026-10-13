# Vandebron Home Assistant Energy Importer

Automated tool to fetch previous-day energy and gas consumption from Vandebron and inject it directly into the Home Assistant Energy Dashboard using Home Assistant's Long-Term Statistics (LTS) WebSocket API (`recorder/import_statistics`).

---

## Features
- **Keycloak OIDC Refresh:** Exchanges your refresh token for a short-lived access token, and automatically updates `config.json` if Keycloak rotates the refresh token.
- **Hourly Aggregation:** Automatically groups 15-minute intervals into 1-hour slots matching Home Assistant Energy Dashboard's native bucket format.
- **Monotonic Cumulative Sums:** Calculates continuous cumulative sums so Home Assistant can accurately compute hourly consumption bars without creating false spikes.
- **Dry-Run Mode:** Inspect the generated Home Assistant WebSocket payloads before sending anything to your live Home Assistant instance.
- **Managed with `uv`:** Ultra-fast, isolated Python dependency management.

---

## 1. Quick Start with `uv`

If you haven't installed `uv` yet:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Navigate to this directory:
```bash
cd /home/ek/dotfiles/scripts/vandebron-ha
```

Run tests to verify the setup:
```bash
uv run python -m unittest discover tests
```

---

## 2. Configuration

Copy the example environment configuration:
```bash
cp .env.example .env
```

Edit `.env` with your values following the .env.example file.


*(Note: `.env` is listed in `.gitignore` and will never be committed to git).*

### How to get your Vandebron `refresh_token`
1. Log in to [mijn.vandebron.nl](https://mijn.vandebron.nl).
2. Open Developer Tools in your browser (**F12** or **Ctrl+Shift+I**).
3. Go to the **Storage** tab (or **Application** in Chrome).
4. Click **Local Storage** $\rightarrow$ `https://mijn.vandebron.nl`.
5. Look for the key `oidc.user:...` or search for `refresh_token`.
6. Copy the value and paste it into `.env`.

### How to get your Home Assistant Long-Lived Access Token
1. In Home Assistant, click your user profile (bottom left).
2. Go to the **Security** tab.
3. Scroll to the bottom to **Long-Lived Access Tokens**.
4. Click **Create Token**, give it a name (e.g. `Vandebron Importer`), and paste it into `.env`.

---

## 3. Running the Sync

### Test with Dry-Run
```bash
uv run python src/sync.py --dry-run
```
This fetches yesterday's data from Vandebron and prints the exact WebSocket payload that will be sent to Home Assistant without modifying your database.

### Run Live Sync
```bash
uv run python src/sync.py
```

### Backfill a Specific Date
```bash
uv run python src/sync.py --date 2026-10-08
```

---

## 4. Configuring Home Assistant Energy Dashboard

Once you have run the sync at least once:
1. In Home Assistant, navigate to **Settings** $\rightarrow$ **Dashboards** $\rightarrow$ **Energy**.
2. **For Electricity:**
   - Under *Electricity Grid*, click **Add Consumption** (or edit existing `sensor.vandebron_electricity_consumption`).
   - Under *Cost tracking*, select **Use an entity tracking the total costs**.
   - Select `sensor.vandebron_electricity_cost`.
   *(Note: Do not select "Use a static price" because imported statistics have no live entity state, which causes HA to display €0.00).*
3. **For Gas:**
   - Under *Gas Consumption*, click **Add Gas Source** (or edit existing `sensor.vandebron_gas_consumption`).
   - Under *Cost tracking*, select **Use an entity tracking the total costs**.
   - Select `sensor.vandebron_gas_cost`.
4. Go to your **Energy** dashboard and navigate the date picker to see your consumption bars and monetary cost graphs!

