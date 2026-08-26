from __future__ import annotations

import os
from calendar import monthrange
from datetime import datetime, time, timedelta, timezone
import math
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

from etherscan_client import (
    EtherscanClient,
    EtherscanError,
    TOKENS,
    ZERO_ADDRESS,
    parse_log,
    topic_address,
)
from market_data import MarketDataError, coingecko_price_usd


st.set_page_config(page_title="Ethereum Mint & Burn Tracker", page_icon="⟠", layout="wide")

TREASURY_ADDRESS = "0x5754284f345afc66a98fbB0a0Afe71e0F007B949"
TRUE_ACTIVITY = "True Mint/Redeem"
SYNTHETIC_ACTIVITY = "Synthetic Mint/Redeem"


def api_key() -> str | None:
    """Read local environment first, then Streamlit Community Cloud secrets."""
    value = os.getenv("ETHERSCAN_API_KEY")
    if value:
        return value
    try:
        return st.secrets.get("ETHERSCAN_API_KEY")
    except (FileNotFoundError, KeyError):
        return None


@st.cache_data(ttl=21_600, show_spinner=False)
def fetch_events(
    key: str,
    symbols: tuple[str, ...],
    start_iso: str,
    end_iso: str,
    activity_mode: str,
) -> tuple[
    pd.DataFrame,
    pd.DataFrame,
    list[str],
    dict[str, float],
    dict[str, datetime],
    dict[str, float | None],
]:
    client = EtherscanClient(key)
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)
    from_block = client.block_at(int(start.timestamp()), "after")
    to_block = client.block_at(int(end.timestamp()), "before")
    records: list[dict] = []
    supply_records: list[dict] = []
    warnings: list[str] = []
    opening_supplies: dict[str, float] = {}
    launch_times: dict[str, datetime] = {}
    prices: dict[str, float | None] = {}

    for symbol in symbols:
        token = TOKENS[symbol]
        creation_block, launch_time = client.contract_creation(token)
        launch_times[symbol] = launch_time
        query_from_block = max(from_block, creation_block)
        if from_block <= creation_block:
            opening_supplies[symbol] = 0.0
        else:
            opening_supplies[symbol] = float(
                client.total_supply_at(token, from_block - 1)
            )
        if symbol in {"USDT", "USAT"}:
            prices[symbol] = 1.0
        else:
            try:
                prices[symbol] = coingecko_price_usd(token)
            except (MarketDataError, requests.RequestException) as exc:
                prices[symbol] = None
                warnings.append(f"Current {symbol} price is unavailable from CoinGecko: {exc}")

        if query_from_block > to_block:
            continue
        participant = (
            TREASURY_ADDRESS if activity_mode == SYNTHETIC_ACTIVITY else ZERO_ADDRESS
        )
        for event_type in ("Mint", "Burn"):
            logs, capped = client.event_logs(
                token,
                event_type,
                query_from_block,
                to_block,
                participant_address=participant,
            )
            if activity_mode == SYNTHETIC_ACTIVITY:
                # True issuance/redemption involving the zero address changes
                # treasury inventory and total supply equally, so it is not a
                # synthetic client mint/redeem.
                counterparty_index = 2 if event_type == "Mint" else 1
                logs = [
                    log
                    for log in logs
                    if topic_address(log["topics"][counterparty_index]).lower()
                    != ZERO_ADDRESS.lower()
                ]
            parsed_logs = [parse_log(log, token, event_type) for log in logs]
            valid_records = [record for record in parsed_logs if record["amount"] > 0]
            records.extend(valid_records)
            if activity_mode == TRUE_ACTIVITY:
                supply_records.extend(valid_records)
            if capped:
                warnings.append(
                    f"{symbol} {event_type.lower()} results reached the 10,000-event safety cap. "
                    "Choose a shorter date range for a complete result."
                )

        # Synthetic activity changes the activity views only. Outstanding
        # supply always follows canonical zero-address mint/burn events.
        if activity_mode == SYNTHETIC_ACTIVITY:
            for event_type in ("Mint", "Burn"):
                logs, capped = client.event_logs(
                    token,
                    event_type,
                    query_from_block,
                    to_block,
                    participant_address=ZERO_ADDRESS,
                )
                parsed_logs = [parse_log(log, token, event_type) for log in logs]
                supply_records.extend(
                    record for record in parsed_logs if record["amount"] > 0
                )
                if capped:
                    warnings.append(
                        f"{symbol} true {event_type.lower()} results reached the 10,000-event "
                        "safety cap. Choose a shorter date range for a complete supply series."
                    )

    columns = ["timestamp", "token", "type", "amount", "from", "to", "tx_hash", "block", "log_index"]
    frame = pd.DataFrame(records, columns=columns)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    if not frame.empty:
        frame = frame.drop_duplicates(["tx_hash", "log_index"]).sort_values("timestamp", ascending=False)
    supply_frame = pd.DataFrame(supply_records, columns=columns)
    supply_frame["timestamp"] = pd.to_datetime(supply_frame["timestamp"], utc=True)
    if not supply_frame.empty:
        supply_frame = supply_frame.drop_duplicates(["tx_hash", "log_index"]).sort_values(
            "timestamp", ascending=False
        )
    return frame, supply_frame, warnings, opening_supplies, launch_times, prices


def compact_number(value: float) -> str:
    absolute = abs(value)
    for divisor, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if absolute >= divisor:
            return f"{value / divisor:,.2f}{suffix}"
    return f"{value:,.2f}"


def financial_ticks(values: pd.Series) -> tuple[list[float], list[str]]:
    """Use finance-friendly T/B/M/K labels instead of SI's T/G/M/k."""
    if values.empty:
        return [], []
    low, high = float(values.min()), float(values.max())
    # Zoom around observed changes instead of letting an area chart force the
    # axis to zero. The small value-relative floor keeps a flat series legible.
    padding = max((high - low) * 0.15, abs(high) * 0.0001, 1.0)
    low, high = max(0.0, low - padding), high + padding
    ticks = [low + (high - low) * index / 3 for index in range(4)]
    step = ticks[1] - ticks[0]
    absolute = max(abs(low), abs(high))
    divisor, suffix = 1.0, ""
    for candidate, label in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if absolute >= candidate:
            divisor, suffix = candidate, label
            break
    scaled_step = abs(step / divisor)
    decimals = 2 if scaled_step == 0 else min(6, max(2, math.ceil(-math.log10(scaled_step)) + 1))
    labels = [f"{value / divisor:,.{decimals}f}{suffix}" for value in ticks]
    return ticks, labels


def previous_month(day):
    year = day.year if day.month > 1 else day.year - 1
    month = day.month - 1 if day.month > 1 else 12
    return day.replace(year=year, month=month, day=min(day.day, monthrange(year, month)[1]))


def select_asset(symbol: str) -> None:
    st.session_state.selected_asset = symbol


timezone_labels = {
    "America/New_York": "Eastern Time (EST/EDT)",
    "America/Chicago": "Central Time (CST/CDT)",
    "America/Denver": "Mountain Time (MST/MDT)",
    "America/Los_Angeles": "Pacific Time (PST/PDT)",
    "UTC": "UTC",
    "Europe/London": "London",
    "Asia/Singapore": "Singapore",
    "Asia/Hong_Kong": "Hong Kong",
    "Asia/Tokyo": "Tokyo",
}
header_column, timezone_column = st.columns([4, 1.35], vertical_alignment="center")
header_column.title("Ethereum Mint & Burn Tracker")
header_column.caption(
    "Canonical ERC-20 supply events for USDT, USAT, XAUT, and PAXG on Ethereum mainnet"
)
timezone_column.selectbox(
    "Local timezone",
    list(timezone_labels),
    index=0,
    format_func=timezone_labels.get,
    key="local_timezone",
)

# Asset navigation lives at the top so switching dashboards is one click.
if "selected_asset" not in st.session_state:
    st.session_state.selected_asset = "USDT"

asset_columns = st.columns(len(TOKENS))
for column, symbol in zip(asset_columns, TOKENS):
    column.button(
        symbol,
        key=f"nav_{symbol}",
        type="primary" if st.session_state.selected_asset == symbol else "secondary",
        width="stretch",
        on_click=select_asset,
        args=(symbol,),
    )

selected = [st.session_state.selected_asset]
if "activity_mode" not in st.session_state:
    st.session_state.activity_mode = TRUE_ACTIVITY
if selected[0] in {"USDT", "XAUT"}:
    st.radio(
        "Activity definition",
        options=[TRUE_ACTIVITY, SYNTHETIC_ACTIVITY],
        key="activity_mode",
        horizontal=True,
        width="content",
        label_visibility="collapsed",
    )
effective_activity_mode = (
    st.session_state.activity_mode
    if selected[0] in {"USDT", "XAUT"}
    else TRUE_ACTIVITY
)
today = datetime.now(timezone.utc).date()
if "date_filter" not in st.session_state:
    st.session_state.date_filter = (previous_month(today), today)
date_column, refresh_column = st.columns([5, 1], vertical_alignment="bottom")
date_range = date_column.date_input(
    "UTC date range", max_value=today, key="date_filter"
)
if refresh_column.button("Refresh now", width="stretch"):
    st.cache_data.clear()
    st.rerun()
if isinstance(date_range, (tuple, list)) and len(date_range) == 2:
    visible_days = (date_range[1] - date_range[0]).days + 1
    st.caption(f"Currently showing past {visible_days:,} days of data.")
st.caption("Data is cached for 6 hours. Use Refresh now to manually repull from Etherscan.")

key = api_key()
if not key:
    st.error("No Etherscan API key found.")
    st.code('ETHERSCAN_API_KEY="your_key_here"', language="toml")
    st.info(
        "For local use, set the environment variable or add the line above to "
        "`.streamlit/secrets.toml`. On Streamlit Community Cloud, add it in App settings → Secrets."
    )
    st.stop()

if not isinstance(date_range, (tuple, list)) or len(date_range) != 2:
    st.info("Choose both a start and end date.")
    st.stop()

start_date, end_date = date_range
if start_date > end_date:
    st.error("The start date must not be after the end date.")
    st.stop()
start_dt = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
# Include the entire selected end date without relying on microsecond precision.
requested_end = datetime.combine(
    end_date + timedelta(days=1), time.min, tzinfo=timezone.utc
) - timedelta(seconds=1)
# During the current UTC day, its nominal end is still in the future. Etherscan
# rejects future timestamps, so leave a small buffer for clock/indexer skew.
safe_chain_time = datetime.now(timezone.utc) - timedelta(minutes=1)
end_dt = min(requested_end, safe_chain_time)

try:
    with st.spinner("Loading Ethereum events from Etherscan…"):
        events, supply_events, warnings, opening_supplies, launch_times, prices = fetch_events(
            key,
            tuple(selected),
            start_dt.isoformat(),
            end_dt.isoformat(),
            effective_activity_mode,
        )
except (EtherscanError, ValueError) as exc:
    st.error(f"Etherscan could not return the requested data: {exc}")
    st.stop()
except Exception as exc:
    st.error(f"Could not reach Etherscan: {exc}")
    st.stop()

for warning in warnings:
    st.warning(warning)

mints = events.loc[events["type"] == "Mint", "amount"].sum()
burns = events.loc[events["type"] == "Burn", "amount"].sum()
active_symbol = selected[0]
chart_data = events.copy()
chart_data["date"] = chart_data["timestamp"].dt.date
chart_data["signed_amount"] = chart_data["amount"].where(chart_data["type"] == "Mint", -chart_data["amount"])
supply_activity = supply_events.copy()
supply_activity["date"] = supply_activity["timestamp"].dt.date
supply_activity["signed_amount"] = supply_activity["amount"].where(
    supply_activity["type"] == "Mint", -supply_activity["amount"]
)
series_start = max(start_date, launch_times[active_symbol].date())
daily_net = (
    supply_activity.groupby("date", as_index=True)["signed_amount"].sum()
    .reindex(
        pd.date_range(series_start, end_date, freq="D").date
        if series_start <= end_date
        else [],
        fill_value=0,
    )
)
supply = pd.DataFrame(
    {
        "date": pd.to_datetime(daily_net.index),
        "supply": opening_supplies[active_symbol] + daily_net.cumsum().to_numpy(),
    }
)
# Keep daily values for calculations, but avoid crowding the charts with
# unchanged observations. Retain only range boundaries and true supply changes.
if supply.empty:
    supply_plot = supply.copy()
else:
    supply_plot_mask = daily_net.ne(0).to_numpy()
    supply_plot_mask[0] = True
    supply_plot_mask[-1] = True
    supply_plot = supply.loc[supply_plot_mask].copy()

assets_outstanding = float(supply["supply"].iloc[-1]) if not supply.empty else 0.0
metric_cols = st.columns(5)
metric_cols[0].metric(
    f"Net {active_symbol} outstanding",
    compact_number(assets_outstanding),
    help=f"As of {end_date} UTC",
)
metric_cols[1].metric("Minted", compact_number(mints))
metric_cols[2].metric("Burned", compact_number(burns))
metric_cols[3].metric("Net issuance", compact_number(mints - burns))
metric_cols[4].metric("Events", f"{len(events):,}")

st.subheader("Activity over time")
if chart_data.empty:
    st.info("No canonical mint or burn events were found for these filters.")
else:
    daily = chart_data.groupby(["date", "token", "type"], as_index=False)["signed_amount"].sum()
    fig = px.bar(
        daily,
        x="date",
        y="signed_amount",
        color="type",
        color_discrete_map={"Mint": "#16a085", "Burn": "#d64545"},
        labels={"signed_amount": "Token units (burns shown negative)", "date": "UTC date"},
        barmode="relative",
    )
    fig.add_hline(y=0, line_color="#7f8c8d", line_width=1)
    fig.update_layout(legend_title_text="Token / event", hovermode="x unified")
    st.plotly_chart(fig, width="stretch")

st.subheader(f"Net {active_symbol} outstanding")
if supply.empty:
    st.info(f"{active_symbol} had not launched by the end of the selected range.")
else:
    supply_fig = px.area(
        supply_plot,
        x="date",
        y="supply",
        labels={"date": "UTC date", "supply": f"{active_symbol} outstanding"},
    )
    supply_fig.update_traces(
        line_color="#2f80ed",
        fillcolor="rgba(47, 128, 237, 0.18)",
        mode="lines+markers",
        marker={"size": 7},
    )
    supply_ticks, supply_tick_labels = financial_ticks(supply_plot["supply"])
    supply_fig.update_yaxes(
        tickmode="array",
        tickvals=supply_ticks,
        ticktext=supply_tick_labels,
        range=[supply_ticks[0], supply_ticks[-1]],
    )
    supply_fig.update_layout(hovermode="x unified", showlegend=False)
    st.plotly_chart(supply_fig, width="stretch")
    st.caption(
        "Supply starts from the token contract's totalSupply immediately before the selected range, "
        "then applies canonical mints and burns through each UTC day. The series begins no earlier "
        "than the contract's on-chain deployment. Charts show the range boundaries and dates when "
        "supply changed."
    )

st.subheader("Market capitalization")
price = prices[active_symbol]
if supply.empty:
    st.info(f"{active_symbol} had not launched by the end of the selected range.")
elif price is None:
    st.info(
        f"CoinGecko did not return a usable current USD price for {active_symbol}, so market "
        "capitalization is unavailable."
    )
else:
    market_cap = supply_plot.assign(market_cap=supply_plot["supply"] * price)
    market_fig = px.area(
        market_cap,
        x="date",
        y="market_cap",
        labels={"date": "UTC date", "market_cap": "Market cap (USD)"},
    )
    market_fig.update_traces(
        line_color="#16a085",
        fillcolor="rgba(22, 160, 133, 0.18)",
        mode="lines+markers",
        marker={"size": 7},
    )
    cap_ticks, cap_tick_labels = financial_ticks(market_cap["market_cap"])
    market_fig.update_yaxes(
        tickmode="array",
        tickvals=cap_ticks,
        ticktext=cap_tick_labels,
        range=[cap_ticks[0], cap_ticks[-1]],
    )
    market_fig.update_layout(hovermode="x unified", showlegend=False)
    st.plotly_chart(market_fig, width="stretch")
    if active_symbol in {"USDT", "USAT"}:
        st.caption("Market capitalization assumes a $1.00 stablecoin price.")
    else:
        st.caption(
            f"Market capitalization values the full supply series at CoinGecko's current "
            f"{active_symbol} price of ${price:,.2f}; it is not a historical price series."
        )

st.subheader("Token summary")
summary = (
    events.pivot_table(index="token", columns="type", values="amount", aggfunc="sum", fill_value=0)
    .reindex(selected)
    .fillna(0)
)
for column in ("Mint", "Burn"):
    if column not in summary:
        summary[column] = 0.0
summary["Net"] = summary["Mint"] - summary["Burn"]
summary["Events"] = events.groupby("token").size().reindex(summary.index, fill_value=0)
st.dataframe(
    summary[["Mint", "Burn", "Net", "Events"]].style.format(
        {"Mint": "{:,.4f}", "Burn": "{:,.4f}", "Net": "{:,.4f}", "Events": "{:,.0f}"}
    ),
    width="stretch",
)

st.subheader("Event details")
display = events.copy()
local_zone = ZoneInfo(st.session_state.local_timezone)
local_label = timezone_labels[st.session_state.local_timezone]
display["local_timestamp"] = display["timestamp"].dt.tz_convert(local_zone).dt.strftime("%Y-%m-%d %H:%M:%S %Z")
display["timestamp"] = display["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S UTC")
display["transaction"] = display["tx_hash"].map(lambda h: f"https://etherscan.io/tx/{h}")
display = display[["timestamp", "local_timestamp", "token", "type", "amount", "from", "to", "block", "transaction"]]
st.dataframe(
    display,
    width="stretch",
    hide_index=True,
    column_config={
        "timestamp": "UTC timestamp",
        "local_timestamp": f"Local timestamp ({local_label})",
        "amount": st.column_config.NumberColumn(format="localized"),
        "transaction": st.column_config.LinkColumn("Etherscan", display_text="View transaction"),
    },
)
st.download_button(
    "Download filtered events (CSV)",
    display.to_csv(index=False).encode("utf-8"),
    file_name=f"ethereum_mint_burn_{start_date}_{end_date}.csv",
    mime="text/csv",
)

with st.expander("Methodology and contract addresses"):
    st.markdown(
        "A **mint** is a standard ERC-20 `Transfer` whose `from` address is the zero address. "
        "A **burn** is a `Transfer` whose `to` address is the zero address. Transfers to other "
        "inaccessible or issuer-controlled addresses are not classified as burns. Amounts are token "
        "units, not USD values."
    )
    for token in TOKENS.values():
        st.markdown(f"- **{token.symbol}** — [`{token.address}`](https://etherscan.io/token/{token.address})")
