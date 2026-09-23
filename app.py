import streamlit as st
import requests
import hashlib
import time
import threading
import pandas as pd

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


# ============================================================
# PAGE
# ============================================================

st.set_page_config(
    page_title="FoxESS Data Checker",
    page_icon="⚡",
    layout="centered"
)


# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://www.foxesscloud.com"
HISTORY_PATH = "/op/v0/device/history/query"

TZ_NAME = "Australia/Sydney"
TZ = ZoneInfo(TZ_NAME)

VARIABLE_REQUEST_DELAY = 0.8
MAX_DAYS = 120


# ============================================================
# API KEY
# ============================================================

try:
    API_KEY = st.secrets["FOXESS_API_KEY"]
except Exception:
    st.error(
        "FOXESS_API_KEY is not configured. "
        "Add it to .streamlit/secrets.toml locally, "
        "or to Streamlit Cloud Secrets after deployment."
    )
    st.stop()


# ============================================================
# VERIFIED HISTORY VARIABLES
# ============================================================

AC_SOLAR_VARIABLE = "meterPower2"
DC_SOLAR_VARIABLE = "generation"

LOAD_VARIABLE = "loadsPower"
GRID_IMPORT_VARIABLE = "gridConsumptionPower"
GRID_EXPORT_VARIABLE = "feedinPower"
BATTERY_CHARGE_VARIABLE = "batChargePower"
BATTERY_DISCHARGE_VARIABLE = "batDischargePower"


# ============================================================
# SHARED API LOCK
# ============================================================

@st.cache_resource
def get_fox_lock():
    return threading.Lock()


FOX_LOCK = get_fox_lock()


# ============================================================
# AUTH
# ============================================================

def create_headers():
    timestamp = str(int(time.time() * 1000))

    signature_text = (
        f"{HISTORY_PATH}\r\n"
        f"{API_KEY}\r\n"
        f"{timestamp}"
    )

    signature = hashlib.md5(
        signature_text.encode("utf-8")
    ).hexdigest()

    return {
        "token": API_KEY,
        "timestamp": timestamp,
        "signature": signature,
        "lang": "en",
        "timezone": TZ_NAME,
        "Content-Type": "application/json"
    }


# ============================================================
# FOX REQUEST: ONE DAY + ONE VARIABLE
# ============================================================

def fox_history_request(sn, day_value, variable):
    start_dt = datetime.combine(
        day_value,
        datetime.min.time()
    ).replace(tzinfo=TZ)

    end_dt = (
        start_dt
        + timedelta(days=1)
        - timedelta(seconds=1)
    )

    payload = {
        "sn": sn,
        "variables": [variable],
        "begin": int(start_dt.timestamp() * 1000),
        "end": int(end_dt.timestamp() * 1000)
    }

    response = requests.post(
        BASE_URL + HISTORY_PATH,
        params={"lang": "en"},
        headers=create_headers(),
        json=payload,
        timeout=30
    )

    response.raise_for_status()
    data = response.json()

    if data.get("errno") != 0:
        raise RuntimeError(
            f"Fox API {data.get('errno')}: {data.get('msg')}"
        )

    return data


# ============================================================
# RETRY
# ============================================================

def request_with_retry(sn, day_value, variable):
    last_error = None
    waits = [0, 5, 10, 20, 30]

    for wait in waits:
        if wait:
            time.sleep(wait)

        try:
            with FOX_LOCK:
                result = fox_history_request(
                    sn,
                    day_value,
                    variable
                )
                time.sleep(VARIABLE_REQUEST_DELAY)

            return result

        except Exception as e:
            last_error = e

    raise last_error


# ============================================================
# PARSE HISTORY
# ============================================================

def extract_points(response_json, requested_variable):
    points = []
    result = response_json.get("result", [])

    if not isinstance(result, list):
        return points

    for result_block in result:
        if not isinstance(result_block, dict):
            continue

        for block in result_block.get("datas", []):
            if block.get("variable") != requested_variable:
                continue

            for point in block.get("data", []):
                try:
                    raw_time = str(point["time"])
                    clean_time = " ".join(
                        raw_time.split(" ")[:2]
                    )

                    dt = datetime.strptime(
                        clean_time,
                        "%Y-%m-%d %H:%M:%S"
                    )

                    value = float(point["value"])
                    points.append((dt, value))

                except Exception:
                    continue

    points.sort(key=lambda x: x[0])
    return points


# ============================================================
# CACHE
# ============================================================

@st.cache_data(ttl=86400, show_spinner=False)
def get_cached_points(sn, day_iso, variable):
    day_value = datetime.strptime(
        day_iso,
        "%Y-%m-%d"
    ).date()

    response = request_with_retry(
        sn,
        day_value,
        variable
    )

    points = extract_points(
        response,
        variable
    )

    if not points:
        raise RuntimeError(
            f"No data returned for {variable}"
        )

    return points


def get_points(sn, day_value, variable):
    today_sydney = datetime.now(TZ).date()

    # Today's data is still changing, so don't cache it.
    if day_value == today_sydney:
        response = request_with_retry(
            sn,
            day_value,
            variable
        )

        points = extract_points(
            response,
            variable
        )

        if not points:
            raise RuntimeError(
                f"No data returned for {variable}"
            )

        return points

    return get_cached_points(
        sn,
        day_value.isoformat(),
        variable
    )


# ============================================================
# POWER INTEGRATION: kW -> kWh
# ============================================================

def integrate_power(points):
    if len(points) < 2:
        return 0.0

    energy = 0.0

    for i in range(1, len(points)):
        t1, p1 = points[i - 1]
        t2, p2 = points[i]

        p1 = max(float(p1), 0)
        p2 = max(float(p2), 0)

        hours = (
            t2 - t1
        ).total_seconds() / 3600

        if hours <= 0:
            continue

        # Ignore telemetry gaps > 15 min.
        if hours > 0.25:
            continue

        energy += (
            (p1 + p2) / 2
        ) * hours

    return energy


# ============================================================
# DC SOLAR: cumulative kWh delta
# ============================================================

def cumulative_energy_delta(points):
    if len(points) < 2:
        return 0.0

    total = 0.0

    for i in range(1, len(points)):
        previous = float(points[i - 1][1])
        current = float(points[i][1])

        delta = current - previous

        if delta >= 0:
            total += delta

        # Possible counter reset.
        elif (
            current >= 0
            and previous > 0
            and current < previous * 0.5
        ):
            total += current

    return total


# ============================================================
# DATE RANGE
# ============================================================

def make_dates(start_date, end_date):
    dates = []
    current = start_date

    while current <= end_date:
        dates.append(current)
        current += timedelta(days=1)

    return dates


# ============================================================
# VARIABLE MAP
# ============================================================

def get_variable_map(system_type):
    solar_variable = (
        AC_SOLAR_VARIABLE
        if system_type == "AC Coupled"
        else DC_SOLAR_VARIABLE
    )

    return {
        "solar": solar_variable,
        "load": LOAD_VARIABLE,
        "grid_import": GRID_IMPORT_VARIABLE,
        "grid_export": GRID_EXPORT_VARIABLE,
        "battery_charge": BATTERY_CHARGE_VARIABLE,
        "battery_discharge": BATTERY_DISCHARGE_VARIABLE
    }


# ============================================================
# CALCULATE ONE METRIC
# ============================================================

def calculate_metric(metric, points, system_type):
    if metric == "solar" and system_type == "DC Coupled":
        return cumulative_energy_delta(points)

    return integrate_power(points)


# ============================================================
# FULL PERIOD QUERY
# ============================================================

def get_period_data(
    sn,
    start_date,
    end_date,
    system_type,
    progress_bar,
    status_box
):
    dates = make_dates(start_date, end_date)
    variables = get_variable_map(system_type)

    metric_labels = {
        "solar": "Solar Generation",
        "load": "Household Load",
        "grid_import": "Grid Import",
        "grid_export": "Grid Export",
        "battery_charge": "Battery Charge",
        "battery_discharge": "Battery Discharge"
    }

    totals = {key: 0.0 for key in variables}
    metric_days = {key: 0 for key in variables}

    daily_rows = []
    failed_rows = []

    total_steps = len(dates) * len(variables)
    completed_steps = 0

    for day_number, day_value in enumerate(
        dates,
        start=1
    ):
        row = {"Date": day_value.isoformat()}
        full_day_ok = True

        for metric, variable in variables.items():
            label = metric_labels[metric]

            status_box.info(
                f"{day_value.strftime('%d %b %Y')} · "
                f"{label} · "
                f"Day {day_number}/{len(dates)}"
            )

            try:
                points = get_points(
                    sn,
                    day_value,
                    variable
                )

                value = calculate_metric(
                    metric,
                    points,
                    system_type
                )

                totals[metric] += value
                metric_days[metric] += 1

                row[f"{label} (kWh)"] = round(
                    value,
                    3
                )

            except Exception as e:
                full_day_ok = False
                row[f"{label} (kWh)"] = None

                failed_rows.append({
                    "Date": day_value.isoformat(),
                    "Metric": label,
                    "Variable": variable,
                    "Error": str(e)
                })

            completed_steps += 1

            progress_bar.progress(
                completed_steps / total_steps
            )

        row["Status"] = (
            "OK"
            if full_day_ok
            else "PARTIAL"
        )

        daily_rows.append(row)

    return (
        totals,
        metric_days,
        pd.DataFrame(daily_rows),
        pd.DataFrame(failed_rows),
        len(dates)
    )


# ============================================================
# HELPERS
# ============================================================

def safe_average(total, days):
    if days <= 0:
        return None

    return total / days


def format_kwh(value):
    return f"{value:,.2f} kWh"


# ============================================================
# CSS
# ============================================================

st.markdown(
    """
    <style>
    .block-container {
        max-width: 850px;
        padding-top: 2rem;
        padding-bottom: 3rem;
    }

    .fox-subtitle {
        color: #777;
        margin-top: -10px;
        margin-bottom: 24px;
        font-size: 0.9rem;
    }
    </style>
    """,
    unsafe_allow_html=True
)


# ============================================================
# HEADER
# ============================================================

st.title("⚡ FoxESS Data Checker")

st.markdown(
    """
    <div class="fox-subtitle">
        History-based energy & bill diagnostic
    </div>
    """,
    unsafe_allow_html=True
)


# ============================================================
# INPUT FORM
# ============================================================

today = datetime.now(TZ).date()
default_start = today - timedelta(days=7)

with st.form("fox_query_form"):
    system_type = st.selectbox(
        "System Type",
        [
            "AC Coupled",
            "DC Coupled"
        ]
    )

    sn = st.text_input(
        "Inverter SN",
        placeholder="e.g. 60HD15305C7M184"
    )

    date_col1, date_col2 = st.columns(2)

    with date_col1:
        start_date = st.date_input(
            "Start Date",
            value=default_start
        )

    with date_col2:
        end_date = st.date_input(
            "End Date",
            value=today
        )

    bill_usage = st.number_input(
        "Retailer General Usage (kWh) — optional",
        min_value=0.0,
        value=0.0,
        step=1.0,
        format="%.3f"
    )

    submitted = st.form_submit_button(
        "Check Data",
        type="primary",
        use_container_width=True
    )


# ============================================================
# RUN
# ============================================================

if submitted:
    sn = sn.strip()

    if not sn:
        st.error("Please enter the inverter SN.")
        st.stop()

    if start_date > end_date:
        st.error(
            "Start Date cannot be after End Date."
        )
        st.stop()

    number_of_days = (
        end_date - start_date
    ).days + 1

    if number_of_days > MAX_DAYS:
        st.error(
            f"This version supports up to {MAX_DAYS} days "
            f"per query. Please split the period."
        )
        st.stop()

    progress_bar = st.progress(0)
    status_box = st.empty()

    try:
        (
            totals,
            metric_days,
            daily_df,
            failed_df,
            requested_days
        ) = get_period_data(
            sn,
            start_date,
            end_date,
            system_type,
            progress_bar,
            status_box
        )

    except Exception as e:
        status_box.empty()
        st.error(f"Query failed: {e}")
        st.stop()

    progress_bar.progress(1.0)
    status_box.success("Query complete")

    # ========================================================
    # ENERGY SUMMARY
    # ========================================================

    st.subheader("Energy Summary")

    c1, c2 = st.columns(2)

    with c1:
        with st.container(border=True):
            solar_days = metric_days["solar"]
            solar_avg = safe_average(
                totals["solar"],
                solar_days
            )

            st.metric(
                "Solar Generation",
                format_kwh(totals["solar"])
            )

            if solar_avg is not None:
                st.caption(
                    f"{solar_avg:,.2f} kWh/day"
                )

            if system_type == "AC Coupled":
                st.caption(
                    "Source: GEN Load / Meter 2"
                )
            else:
                st.caption(
                    "Source: Fox cumulative generation"
                )

            if solar_days != requested_days:
                st.warning(
                    f"{solar_days}/{requested_days} days"
                )

    with c2:
        with st.container(border=True):
            load_days = metric_days["load"]
            load_avg = safe_average(
                totals["load"],
                load_days
            )

            st.metric(
                "Household Load",
                format_kwh(totals["load"])
            )

            if load_avg is not None:
                st.caption(
                    f"{load_avg:,.2f} kWh/day"
                )

            if load_days != requested_days:
                st.warning(
                    f"{load_days}/{requested_days} days"
                )

    c3, c4 = st.columns(2)

    with c3:
        with st.container(border=True):
            import_days = metric_days["grid_import"]
            import_avg = safe_average(
                totals["grid_import"],
                import_days
            )

            st.metric(
                "Grid Import",
                format_kwh(
                    totals["grid_import"]
                )
            )

            if import_avg is not None:
                st.caption(
                    f"{import_avg:,.2f} kWh/day"
                )

            if import_days != requested_days:
                st.warning(
                    f"{import_days}/{requested_days} days"
                )

    with c4:
        with st.container(border=True):
            export_days = metric_days["grid_export"]

            st.metric(
                "Grid Export",
                format_kwh(
                    totals["grid_export"]
                )
            )

            if export_days != requested_days:
                st.warning(
                    f"{export_days}/{requested_days} days"
                )

    c5, c6 = st.columns(2)

    with c5:
        with st.container(border=True):
            charge_days = metric_days[
                "battery_charge"
            ]

            st.metric(
                "Battery Charge",
                format_kwh(
                    totals["battery_charge"]
                )
            )

            if charge_days != requested_days:
                st.warning(
                    f"{charge_days}/{requested_days} days"
                )

    with c6:
        with st.container(border=True):
            discharge_days = metric_days[
                "battery_discharge"
            ]

            st.metric(
                "Battery Discharge",
                format_kwh(
                    totals["battery_discharge"]
                )
            )

            if discharge_days != requested_days:
                st.warning(
                    f"{discharge_days}/{requested_days} days"
                )

    # ========================================================
    # BILL COMPARISON
    # ========================================================

    if bill_usage > 0:
        st.subheader("Bill Comparison")

        with st.container(border=True):
            grid_import_complete = (
                metric_days["grid_import"]
                == requested_days
            )

            if grid_import_complete:
                difference = abs(
                    totals["grid_import"]
                    - bill_usage
                )

                difference_pct = (
                    difference
                    / bill_usage
                    * 100
                )

                left, right = st.columns(2)

                with left:
                    st.metric(
                        "Fox Grid Import",
                        format_kwh(
                            totals["grid_import"]
                        )
                    )

                with right:
                    st.metric(
                        "Retailer Usage",
                        format_kwh(
                            bill_usage
                        )
                    )

                st.divider()

                st.metric(
                    "Difference",
                    (
                        f"{difference:,.2f} kWh "
                        f"({difference_pct:.2f}%)"
                    )
                )

                if difference_pct <= 2:
                    st.success(
                        "✓ Data closely aligned"
                    )

                elif difference_pct <= 5:
                    st.warning(
                        "△ Small difference detected"
                    )

                else:
                    st.error(
                        "⚠ Further review recommended"
                    )

            else:
                st.warning(
                    "Bill comparison is unavailable because "
                    "Grid Import history is incomplete."
                )

    # ========================================================
    # DATA STATUS
    # ========================================================

    st.subheader("Data Status")

    status_df = pd.DataFrame({
        "Metric": [
            "Solar Generation",
            "Household Load",
            "Grid Import",
            "Grid Export",
            "Battery Charge",
            "Battery Discharge"
        ],
        "Days Retrieved": [
            metric_days["solar"],
            metric_days["load"],
            metric_days["grid_import"],
            metric_days["grid_export"],
            metric_days["battery_charge"],
            metric_days["battery_discharge"]
        ],
        "Days Requested": [
            requested_days
        ] * 6
    })

    all_complete = all(
        value == requested_days
        for value in metric_days.values()
    )

    if all_complete:
        st.success(
            f"✓ All History data retrieved · "
            f"{requested_days}/{requested_days} days"
        )
    else:
        st.warning(
            "Some History data could not be retrieved. "
            "Review the status table or failed-query CSV."
        )

    st.dataframe(
        status_df,
        hide_index=True,
        use_container_width=True
    )

    st.info(
        "History values are intended for technical diagnostic "
        "comparison. Small differences from the FoxESS portal "
        "may occur due to telemetry interval and aggregation."
    )

    # ========================================================
    # EXPORTS
    # ========================================================

    st.subheader("Export")

    labels = {
        "solar": "Solar Generation",
        "load": "Household Load",
        "grid_import": "Grid Import",
        "grid_export": "Grid Export",
        "battery_charge": "Battery Charge",
        "battery_discharge": "Battery Discharge"
    }

    summary_rows = []

    for key, label in labels.items():
        summary_rows.append({
            "System Type": system_type,
            "Metric": label,
            "Value": round(totals[key], 2),
            "Unit": "kWh",
            "Days Retrieved": metric_days[key],
            "Days Requested": requested_days
        })

    if bill_usage > 0:
        summary_rows.append({
            "System Type": system_type,
            "Metric": "Retailer General Usage",
            "Value": bill_usage,
            "Unit": "kWh",
            "Days Retrieved": "",
            "Days Requested": requested_days
        })

    summary_df = pd.DataFrame(
        summary_rows
    )

    summary_csv = summary_df.to_csv(
        index=False
    ).encode("utf-8")

    daily_csv = daily_df.to_csv(
        index=False
    ).encode("utf-8")

    download_col1, download_col2 = st.columns(2)

    with download_col1:
        st.download_button(
            "Download Summary CSV",
            data=summary_csv,
            file_name=f"FoxESS_{sn}_Summary.csv",
            mime="text/csv",
            use_container_width=True
        )

    with download_col2:
        st.download_button(
            "Download Daily CSV",
            data=daily_csv,
            file_name=f"FoxESS_{sn}_Daily.csv",
            mime="text/csv",
            use_container_width=True
        )

    if not failed_df.empty:
        failed_csv = failed_df.to_csv(
            index=False
        ).encode("utf-8")

        st.download_button(
            "Download Failed Queries CSV",
            data=failed_csv,
            file_name=f"FoxESS_{sn}_Failed.csv",
            mime="text/csv"
        )
