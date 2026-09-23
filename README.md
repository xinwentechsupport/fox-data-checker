# FoxESS Data Checker

A Streamlit-based internal diagnostic tool for FoxESS history data.

## 1. Local setup

### Windows PowerShell / Terminal

Open this folder and run:

```bash
pip install -r requirements.txt
```

Copy:

```text
.streamlit/secrets.toml.example
```

to:

```text
.streamlit/secrets.toml
```

Then replace the placeholder with the real FoxESS API key.

Run:

```bash
streamlit run app.py
```

The browser should open automatically at:

```text
http://localhost:8501
```

## 2. Important: do not run app.py as a normal Colab cell

This is a Streamlit web app, not a notebook script.

If you see:

```text
ModuleNotFoundError: No module named 'streamlit'
```

install the dependencies first:

```bash
pip install -r requirements.txt
```

For Google Colab specifically:

```python
!pip install streamlit requests pandas
```

However, Colab is only useful for testing. For colleagues, deploy the app as a private Streamlit web app.

## 3. Streamlit Cloud deployment

Upload these files to a private GitHub repository:

- app.py
- requirements.txt
- .gitignore
- README.md

Do NOT upload `.streamlit/secrets.toml`.

In the Streamlit deployment settings, add this Secret:

```toml
FOXESS_API_KEY = "YOUR_REAL_KEY"
```

Then share the private app URL with approved colleagues.

## 4. Calculation logic

### AC Coupled
- Solar Generation: `meterPower2` (kW history integrated to kWh)

### DC Coupled
- Solar Generation: `generation` (cumulative kWh delta)

### Other metrics
- Household Load: `loadsPower`
- Grid Import: `gridConsumptionPower`
- Grid Export: `feedinPower`
- Battery Charge: `batChargePower`
- Battery Discharge: `batDischargePower`

History is queried one day and one variable at a time to reduce Fox API errors such as 41930.
