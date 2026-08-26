# Ethereum Mint & Burn Tracker

A Streamlit dashboard for canonical ERC-20 mint and burn activity for USDT, USAT, XAUT, and PAXG on Ethereum mainnet. It uses Etherscan API V2 event logs and classifies `Transfer` events from/to the zero address.

For USDT and XAUT, the dashboard can alternatively classify transfers out of Tether's treasury as synthetic mints and transfers into the treasury as synthetic redemptions. Current XAUT and PAXG prices come from CoinGecko's keyless public API.

## Run locally

1. Create and activate a virtual environment.
2. Install dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

3. Provide your API key using either option:

   ```powershell
   $env:ETHERSCAN_API_KEY = "your_key_here"
   ```

   Or create `.streamlit/secrets.toml` (gitignored):

   ```toml
   ETHERSCAN_API_KEY = "your_key_here"
   ```

4. Start the app:

   ```powershell
   streamlit run app.py
   ```

## Deploy on Streamlit Community Cloud

Deploy this repository with `app.py` as the entry point. In **App settings → Secrets**, add:

```toml
ETHERSCAN_API_KEY = "your_key_here"
```

The app checks the local environment variable first, then Streamlit secrets. API responses are cached for six hours and can be manually refreshed from the dashboard. Each token/event query paginates up to 10,000 logs; the UI warns when that safety cap is reached so the date range can be shortened.

## Classification

- Mint: `Transfer(address(0), recipient, amount)`
- Burn: `Transfer(sender, address(0), amount)`

Transfers to dead, inaccessible, treasury, or issuer-controlled addresses are intentionally not inferred to be burns.
