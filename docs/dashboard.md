# Dashboard

A Streamlit app with five pages:

| Page | What it shows |
| --- | --- |
| **Timetable** | Pick two stations and a time; see the next trains with type, destination and tags for AC, 15-car and ladies' specials |
| **Delay patterns** | Heatmaps of mean delay by hour and weekday, and by station along the route; a table of every train's typical and bad-day delay; one train's delay along its route |
| **Crowd reports** | The reports logged through the Telegram bot, by level and by hour |
| **Recommender** | An interactive `/next`: the recommended train, each train's predicted arrival and crowding, and the explanation |
| **Model metrics** | The delay model's accuracy against the baselines, range calibration and feature importance |

## Run it locally

```bash
uv run sitt-dashboard
```

It opens at http://localhost:8501. (`uv run streamlit run src/sitt/dashboard/app.py` is the
same thing.)

## What data it uses

The app decides this once at startup and says which in the sidebar.

**Timetable**

1. The real database (`SITT_DB_PATH`, default `data/sitt.duckdb`) if it holds a timetable.
2. Otherwise it builds one in a cache folder from Central Railway's official PDFs: four
   downloads, each checked against the hash this project knows.
3. If that fails, an invented sample timetable (13 made-up trains). The pages then carry a
   red **SAMPLE TIMETABLE** banner.

**Observations** (the Delay patterns page, and past delays in the Recommender)

1. The real database's observations, if it has any.
2. Otherwise `data/synthetic.duckdb`, if you have generated it for the same timetable
   (`python -m sitt.synth`).
3. Otherwise three weeks of synthetic observations generated on the spot.

In cases 2 and 3 a yellow **SYNTHETIC DATA** banner is shown wherever those numbers appear.
The delays are then invented and say nothing about real trains
([`synthetic-data.md`](synthetic-data.md)). Collect real observations
([`collector.md`](collector.md)), load them into the real database, and the banner goes away
with no code changes.

**Crowd reports** always come from the real database. They are never synthetic.

**Model metrics** come from `models/delay/metadata.json` if you have trained a model.
Otherwise the page shows [`model-results.md`](model-results.md) from the repository.

Nothing the dashboard builds for itself goes into `data/`. It uses a `sitt-dashboard` folder
in the system's temporary directory, or `SITT_DASHBOARD_CACHE` if set. The app only reads the
real database.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SITT_DB_PATH` | `data/sitt.duckdb` | The real database |
| `SITT_MODEL_DIR` | `models/delay` | Where the trained delay model is |
| `SITT_DASHBOARD_CACHE` | a temp folder | Where demo data is built |
| `SITT_DASHBOARD_DOWNLOAD` | `1` | Set to `0` to never download the PDFs |

## Deploy to Streamlit Community Cloud

No secrets are needed. The app builds everything from the repository.

1. Push the repository to GitHub (it is public already).
2. At https://share.streamlit.io choose **Create app → Deploy a public app from GitHub**.
3. Repository `vinay443/Should-I-Take-This-Train-`, branch `main`, main file path
   `streamlit_app.py`.
4. Under **Advanced settings**, choose Python 3.12. Leave the secrets box empty.
5. Deploy.

The first visit after a deploy or a restart takes about a minute while the app downloads and
converts the timetable PDFs and generates synthetic observations. After that it is quick.

What the deployed app will and won't have:

- **Timetable:** the real one, if Central Railway's site answers Streamlit's servers. If not,
  the sample timetable with its red banner. This was not tested from Streamlit's servers.
- **Observations:** always synthetic, with the banner. The collected data lives on your
  machine or the `data` branch, not in the app.
- **Crowd reports:** none. They are in your local database.
- **Recommender:** timetable times and the rule-of-thumb crowding. There is no trained model
  in the repository (`models/` is gitignored), so it never shows model predictions there.
- **Model metrics:** the results page from the repository.

Three files at the repository root exist for Community Cloud:

- [`streamlit_app.py`](../streamlit_app.py): the entry point it looks for.
- [`requirements.txt`](../requirements.txt): the packages to install, exported from `uv.lock`.
  Regenerate it whenever dependencies change:

  ```bash
  uv export --no-dev --no-hashes --no-emit-project --format requirements-txt -o requirements.txt
  ```

- [`packages.txt`](../packages.txt): one system library that LightGBM needs.

## Tests

```bash
uv run pytest tests/dashboard
```

The tests cover the data layer and run every page headlessly with Streamlit's `AppTest`,
with downloads switched off.
