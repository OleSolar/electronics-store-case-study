# %% 0. Imports and Snowflake session
import warnings

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import statsmodels.formula.api as smf
from snowflake.snowpark.context import get_active_session
from statsmodels.tsa.holtwinters import ExponentialSmoothing
from statsmodels.tsa.statespace.sarimax import SARIMAX

warnings.filterwarnings("ignore", category=UserWarning)
session = get_active_session()


# %% 1. Snowsight set-up: aligned with the current Snowsight SQL
# SALES_ANALYSIS is used because the annual summary is too aggregated for
# monthly forecasting and interrupted time-series regression.

SOURCE_TABLE = "GLOBAL_ELECTRONICS_STORE.MY_SCHEMA.SALES_ANALYSIS"
DATE_COLUMN = "ORDER_DATE"
REVENUE_COLUMN = "REVENUE_USD"

OUTPUT_DATABASE = "GLOBAL_ELECTRONICS_STORE"
OUTPUT_SCHEMA = "MY_SCHEMA"

COVID_START = pd.Timestamp("2020-03-01")
# Use complete calendar months for the shareholder analysis. 
ANALYSIS_END = pd.Timestamp("2020-12-01")
MAX_POST_MONTHS = 12

# These tables are written to OUTPUT_DATABASE and OUTPUT_SCHEMA.

FORECAST_OUTPUT_TABLE = "COVID_FORECAST_RESULTS"
ITS_OUTPUT_TABLE = "COVID_ITS_RESULTS"
MODEL_COMPARISON_TABLE = "COVID_MODEL_COMPARISON"
ROLLING_COMPARISON_TABLE = "COVID_ROLLING_BACKTEST"
COEFFICIENT_OUTPUT_TABLE = "COVID_REGRESSION_COEFFICIENTS"
GROWTH_OUTPUT_TABLE = "COVID_GROWTH_SARIMA_RESULTS"
GROWTH_MODEL_NAME = "Growth-aware SARIMA"
SIMPLE_MODEL_NAME = "Simple log trend + month effects"


# %% 2. Load and validate the monthly revenue series

query = f"""
SELECT
    DATE_TRUNC('MONTH', {DATE_COLUMN}) AS MONTH_START,
    SUM({REVENUE_COLUMN})              AS REVENUE
FROM {SOURCE_TABLE}
GROUP BY 1
ORDER BY 1
"""

monthly = session.sql(query).to_pandas()
monthly.columns = monthly.columns.str.upper()
monthly["MONTH_START"] = pd.to_datetime(monthly["MONTH_START"])
monthly["REVENUE"] = pd.to_numeric(monthly["REVENUE"], errors="coerce")
monthly = monthly.sort_values("MONTH_START").set_index("MONTH_START").asfreq("MS")

if ANALYSIS_END is not None:
    monthly = monthly.loc[monthly.index <= ANALYSIS_END].copy()

missing_months = monthly.index[monthly["REVENUE"].isna()]
if len(missing_months):
    missing_text = ", ".join(d.strftime("%Y-%m") for d in missing_months)
    raise ValueError(
        "The time series has missing months: " + missing_text
        + ". Confirm whether they should be zero before fitting the models."
    )

if (monthly["REVENUE"] < 0).any():
    raise ValueError("Negative monthly revenue was found. Review returns/refunds first.")

pre_covid = monthly.loc[monthly.index < COVID_START, "REVENUE"]
post_covid = monthly.loc[monthly.index >= COVID_START, "REVENUE"].iloc[:MAX_POST_MONTHS]

if len(pre_covid) < 36:
    raise ValueError("At least 36 complete pre-COVID months are recommended.")
if len(post_covid) == 0:
    raise ValueError("No observations were found on or after COVID_START.")

print(
    f"Loaded {len(monthly)} months: "
    f"{monthly.index.min():%Y-%m} to {monthly.index.max():%Y-%m}."
)
print(f"Pre-COVID months: {len(pre_covid)}; post-COVID months analysed: {len(post_covid)}")


# %% 3. Forecasting functions and pre-COVID backtest
def simple_design_matrix(months, start_index):
    """Intercept, elapsed years, and 11 fixed month-of-year indicators."""
    elapsed_years = np.arange(start_index, start_index + len(months)) / 12.0
    columns = [np.ones(len(months)), elapsed_years]
    columns.extend((months.month == month).astype(float) for month in range(2, 13))
    return np.column_stack(columns)


def simple_growth_forecast(train, steps):
    """Fit log revenue to a trend and month effects; return USD mean forecasts."""
    if len(train) < 24 or (train <= 0).any():
        raise ValueError("Simple growth model requires 24 positive training months.")
    design = simple_design_matrix(train.index, 0)
    coefficients, _, rank, _ = np.linalg.lstsq(
        design, np.log(train.to_numpy()), rcond=None
    )
    if rank != design.shape[1]:
        raise ValueError("Simple growth trend and month effects cannot be estimated.")

    residuals = np.log(train.to_numpy()) - design @ coefficients
    smearing_factor = np.mean(np.exp(residuals))
    future_months = pd.date_range(
        train.index[-1] + pd.offsets.MonthBegin(1), periods=steps, freq="MS"
    )
    forecast = np.exp(
        simple_design_matrix(future_months, len(train)) @ coefficients
    ) * smearing_factor
    if not np.isfinite(forecast).all():
        raise ValueError("Simple growth model produced non-finite USD forecasts.")
    return forecast, float(np.expm1(coefficients[1]))


def fit_growth_sarima(train):
    """Estimate an annual log-revenue trend with seasonal ARIMA errors."""
    if (train <= 0).any():
        raise ValueError("Growth-aware SARIMA requires positive monthly revenue for log().")

    # One unit of TREND_YEARS is 12 months. The coefficient is therefore an
    # annual log-growth estimate based on every month passed to this function.
    trend_years = np.arange(len(train), dtype=float) / 12.0
    train_exog = pd.DataFrame({"TREND_YEARS": trend_years}, index=train.index)
    fitted = SARIMAX(
        np.log(train),
        exog=train_exog,
        order=(1, 0, 1),
        seasonal_order=(1, 0, 0, 12),
        trend="c",
        enforce_stationarity=False,
        enforce_invertibility=False,
    ).fit(disp=False, maxiter=200)

    if not fitted.mle_retvals.get("converged", True):
        raise RuntimeError("Growth-aware SARIMA did not converge; do not use its forecast.")
    return fitted


def forecast_growth_sarima(train, steps):
    """Return USD mean forecast, USD prediction bounds, and fitted model."""
    fitted = fit_growth_sarima(train)
    future_months = pd.date_range(
        train.index[-1] + pd.offsets.MonthBegin(1), periods=steps, freq="MS"
    )
    future_trend_years = np.arange(len(train), len(train) + steps, dtype=float) / 12.0
    future_exog = pd.DataFrame(
        {"TREND_YEARS": future_trend_years}, index=future_months
    )
    prediction = fitted.get_forecast(steps=steps, exog=future_exog)

    log_mean = np.asarray(prediction.predicted_mean, dtype=float)
    log_variance = np.maximum(
        np.asarray(prediction.var_pred_mean, dtype=float), 0.0
    )
    log_bounds = np.asarray(prediction.conf_int(alpha=0.05), dtype=float)

    # Under the model's log-normal forecast distribution, exp(log_mean) is
    # the median. Adding half the forecast variance yields the USD mean.
    usd_mean = np.exp(log_mean + 0.5 * log_variance)
    usd_bounds = np.exp(log_bounds)
    if not np.isfinite(usd_mean).all() or not np.isfinite(usd_bounds).all():
        raise RuntimeError("Growth-aware SARIMA produced non-finite USD values.")
    return usd_mean, usd_bounds[:, 0], usd_bounds[:, 1], fitted


def model_forecast(model_name, train, steps):
    """Fit one model and return a non-negative revenue forecast."""
    if model_name == "Seasonal naive":
        if len(train) < 12:
            raise ValueError("Seasonal naive forecasting requires at least 12 months.")
        values = np.tile(train.iloc[-12:].to_numpy(), int(np.ceil(steps / 12)))[:steps]

    elif model_name == "Holt-Winters":
        fitted = ExponentialSmoothing(
            train,
            trend="add",
            damped_trend=True,
            seasonal="add",
            seasonal_periods=12,
            initialization_method="estimated",
        ).fit(optimized=True, use_brute=True)
        values = np.asarray(fitted.forecast(steps))

    elif model_name == "SARIMA":
        fitted = SARIMAX(
            train,
            order=(1, 1, 1),
            seasonal_order=(1, 1, 0, 12),
            enforce_stationarity=False,
            enforce_invertibility=False,
        ).fit(disp=False)
        values = np.asarray(fitted.get_forecast(steps=steps).predicted_mean)

    elif model_name == GROWTH_MODEL_NAME:
        values, _, _, _ = forecast_growth_sarima(train, steps)

    elif model_name == SIMPLE_MODEL_NAME:
        values, _ = simple_growth_forecast(train, steps)

    else:
        raise ValueError(f"Unknown model: {model_name}")

    return np.maximum(values.astype(float), 0.0)


def accuracy(actual, predicted):
    error = np.asarray(actual, dtype=float) - np.asarray(predicted, dtype=float)
    mae = np.mean(np.abs(error))
    rmse = np.sqrt(np.mean(error**2))
    wmape = np.sum(np.abs(error)) / np.sum(np.abs(actual))
    return mae, rmse, wmape


# Reserve the final 12 pre-COVID months for an honest historical test.

BACKTEST_MONTHS = 12
backtest_train = pre_covid.iloc[:-BACKTEST_MONTHS]
backtest_actual = pre_covid.iloc[-BACKTEST_MONTHS:]

comparison_rows = []
backtest_predictions = {}

for model_name in [
    "Seasonal naive", "Holt-Winters", "SARIMA", SIMPLE_MODEL_NAME,
    GROWTH_MODEL_NAME,
]:
    try:
        prediction = model_forecast(model_name, backtest_train, BACKTEST_MONTHS)
        mae, rmse, wmape = accuracy(backtest_actual, prediction)
        comparison_rows.append(
            {
                "MODEL": model_name,
                "MAE": mae,
                "RMSE": rmse,
                "WMAPE": wmape,
                "STATUS": "Successful",
            }
        )
        backtest_predictions[model_name] = prediction
    except Exception as exc:
        comparison_rows.append(
            {
                "MODEL": model_name,
                "MAE": np.nan,
                "RMSE": np.nan,
                "WMAPE": np.nan,
                "STATUS": f"Failed: {str(exc)[:160]}",
            }
        )

model_comparison = pd.DataFrame(comparison_rows)
successful_models = model_comparison.loc[model_comparison["STATUS"] == "Successful"]
if successful_models.empty:
    raise RuntimeError("All forecasting models failed. Review the model status messages.")

# Keep the best of the original three as a fallback. The simple growth model
# earns primary status only if it also passes repeated pre-COVID tests.

original_models = successful_models.loc[
    successful_models["MODEL"].isin(["Seasonal naive", "Holt-Winters", "SARIMA"])
]
if original_models.empty:
    raise RuntimeError("All original forecasting models failed.")
baseline_model = original_models.sort_values("WMAPE").iloc[0]["MODEL"]

# Each origin forecasts the next 12 months using only earlier observations.
# Windows partly overlap; pooled scores are descriptive, not independent tests.
rolling_folds = [
    ("2018 Jan-Dec", 24),
    ("2018 Jul-2019 Jun", 30),
    ("2019 Jan-Dec", 36),
    ("2019 Mar-2020 Feb", 38),
]
rolling_rows = []
for period, train_months in rolling_folds:
    train = pre_covid.iloc[:train_months]
    actual = pre_covid.iloc[train_months : train_months + 12]
    if len(actual) != 12:
        raise ValueError(f"Incomplete 12-month pre-COVID holdout: {period}")
    for model_name in ["Seasonal naive", SIMPLE_MODEL_NAME]:
        prediction = model_forecast(model_name, train, 12)
        mae, rmse, wmape = accuracy(actual, prediction)
        rolling_rows.append(
            {
                "PERIOD": period,
                "MODEL": model_name,
                "MAE": mae,
                "RMSE": rmse,
                "WMAPE": wmape,
                "ABS_ERROR_USD": float(np.sum(np.abs(actual.to_numpy() - prediction))),
                "ACTUAL_USD": float(actual.sum()),
            }
        )

rolling_comparison = pd.DataFrame(rolling_rows)
pooled_totals = rolling_comparison.groupby("MODEL")[["ABS_ERROR_USD", "ACTUAL_USD"]].sum()
pooled_wmape = pooled_totals["ABS_ERROR_USD"] / pooled_totals["ACTUAL_USD"]
rolling_wide = rolling_comparison.pivot(index="PERIOD", columns="MODEL", values="WMAPE")
fold_wins = int((rolling_wide[SIMPLE_MODEL_NAME] < rolling_wide["Seasonal naive"]).sum())
latest_fold_better = bool(
    rolling_wide.loc["2019 Mar-2020 Feb", SIMPLE_MODEL_NAME]
    < rolling_wide.loc["2019 Mar-2020 Feb", "Seasonal naive"]
)

simple_counterfactual, simple_annual_trend = simple_growth_forecast(
    pre_covid, len(post_covid)
)
simple_plausible = bool(
    -0.25 <= simple_annual_trend <= 1.00
    and simple_counterfactual.min() > 0
    and simple_counterfactual.max() <= 2 * pre_covid.max()
)
simple_holdout = model_comparison.loc[model_comparison["MODEL"] == SIMPLE_MODEL_NAME].iloc[0]
simple_supported = bool(
    simple_holdout["STATUS"] == "Successful"
    and simple_plausible
    and pooled_wmape[SIMPLE_MODEL_NAME] < pooled_wmape["Seasonal naive"]
    and fold_wins > len(rolling_folds) / 2
    and latest_fold_better
    and float(simple_holdout["WMAPE"]) < float(original_models["WMAPE"].min())
)
best_model = SIMPLE_MODEL_NAME if simple_supported else baseline_model
best_backtest_rmse = float(
    successful_models.loc[successful_models["MODEL"] == best_model, "RMSE"].iloc[0]
)
if best_model == SIMPLE_MODEL_NAME:
    # A single recent holdout would make the display range too narrow. Use
    # the larger RMSE from the repeated tests, still only as an error guide.
    simple_rolling_rmse = float(np.sqrt(np.mean(
        rolling_comparison.loc[rolling_comparison["MODEL"] == SIMPLE_MODEL_NAME, "RMSE"]
        .to_numpy() ** 2
    )))
    best_backtest_rmse = max(best_backtest_rmse, simple_rolling_rmse)

print(model_comparison.sort_values("WMAPE", na_position="last").to_string(index=False))
print("Pre-COVID rolling-origin backtests (12-month forecasts):")
print(rolling_comparison[["PERIOD", "MODEL", "MAE", "RMSE", "WMAPE"]].to_string(index=False))
print(
    f"Pooled WMAPE, overlapping windows: simple {pooled_wmape[SIMPLE_MODEL_NAME]:.1%}; "
    f"seasonal naive {pooled_wmape['Seasonal naive']:.1%}"
)
print(f"Simple model won {fold_wins} of {len(rolling_folds)} 12-month windows")
print(f"Simple model fitted annual pre-COVID trend: {simple_annual_trend:.1%}")
print(f"Simple model plausibility checks passed: {simple_plausible}")
print(f"Primary model selected: {best_model}")

# Report the most recent two pre-COVID months as context, not a selection veto.

jan_feb_actual = pre_covid.iloc[48:50]
jan_feb_train = pre_covid.iloc[:48]
jan_feb_simple_wmape = accuracy(
    jan_feb_actual, model_forecast(SIMPLE_MODEL_NAME, jan_feb_train, 2)
)[2]
jan_feb_naive_wmape = accuracy(
    jan_feb_actual, model_forecast("Seasonal naive", jan_feb_train, 2)
)[2]
print(
    f"Jan-Feb 2020 cautionary WMAPE: simple {jan_feb_simple_wmape:.1%}; "
    f"seasonal naive {jan_feb_naive_wmape:.1%}"
)
if simple_supported and jan_feb_simple_wmape > jan_feb_naive_wmape:
    print("Retain seasonal naive as a conservative scenario: recent growth was slower.")

growth_backtest = model_comparison.loc[model_comparison["MODEL"] == GROWTH_MODEL_NAME].iloc[0]
if growth_backtest["STATUS"] != "Successful":
    raise RuntimeError(
        "Growth-aware SARIMA backtest failed: " + str(growth_backtest["STATUS"])
    )
print(f"Growth-aware SARIMA sensitivity backtest WMAPE: {growth_backtest['WMAPE']:.1%}")
if float(growth_backtest["WMAPE"]) >= float(simple_holdout["WMAPE"]):
    print(
        "Growth-aware SARIMA did not beat the simple-growth benchmark on "
        "the pre-COVID holdout. Keep it as a sensitivity scenario."
    )
else:
    print(
        "Growth-aware SARIMA did better on this one holdout, but is a sensitivity "
        "scenario until repeated tests and trend checks support it."
    )


# %% 4. Forecast the no-COVID counterfactual from March 2020

counterfactual = model_forecast(best_model, pre_covid, len(post_covid))
seasonal_comparison = model_forecast("Seasonal naive", pre_covid, len(post_covid))

# Approximate interval based on the selected model's pre-COVID backtest error.
# It is an uncertainty guide, not an exact model-based confidence interval.
lower_95 = np.maximum(counterfactual - 1.96 * best_backtest_rmse, 0.0)
upper_95 = counterfactual + 1.96 * best_backtest_rmse

forecast_results = pd.DataFrame(
    {
        "MONTH_START": post_covid.index,
        "ACTUAL_REVENUE": post_covid.to_numpy(dtype=float),
        "FORECAST_REVENUE_NO_COVID": counterfactual,
        "SEASONAL_NAIVE_COMPARISON": seasonal_comparison,
        "LOWER_95_APPROX": lower_95,
        "UPPER_95_APPROX": upper_95,
        "ESTIMATED_REVENUE_IMPACT": post_covid.to_numpy(dtype=float) - counterfactual,
        "MODEL": best_model,
        "SIMPLE_ANNUAL_TREND_RATE": simple_annual_trend,
        "BACKTEST_RMSE_USED": best_backtest_rmse,
    }
)
forecast_results["ESTIMATED_IMPACT_PCT"] = np.where(
    forecast_results["FORECAST_REVENUE_NO_COVID"] != 0,
    forecast_results["ESTIMATED_REVENUE_IMPACT"]
    / forecast_results["FORECAST_REVENUE_NO_COVID"],
    np.nan,
)

forecast_total = forecast_results["FORECAST_REVENUE_NO_COVID"].sum()
actual_total = forecast_results["ACTUAL_REVENUE"].sum()
total_impact = actual_total - forecast_total

print(f"Actual post-interruption revenue: ${actual_total:,.0f}")
print(f"Primary pre-COVID forecast benchmark ({best_model}): ${forecast_total:,.0f}")
print(f"Seasonal-naive conservative comparison: ${seasonal_comparison.sum():,.0f}")
print(f"Estimated gap against primary benchmark: ${total_impact:,.0f} ({total_impact / forecast_total:.1%})")


# %% 4a. Growth-aware SARIMA sensitivity forecast
# This fit uses all 50 months from January 2016 through February 2020.

growth_forecast, growth_lower_95, growth_upper_95, growth_fit = (
    forecast_growth_sarima(pre_covid, len(post_covid))
)
annual_log_trend = float(growth_fit.params["TREND_YEARS"])
estimated_annual_growth = np.expm1(annual_log_trend)
growth_sarima_presentation_ready = bool(
    -0.25 <= estimated_annual_growth <= 1.00
    and float(growth_backtest["WMAPE"]) < float(simple_holdout["WMAPE"])
)

growth_results = pd.DataFrame(
    {
        "MONTH_START": post_covid.index,
        "ACTUAL_REVENUE_USD": post_covid.to_numpy(dtype=float),
        "GROWTH_SARIMA_FORECAST_USD": growth_forecast,
        "GROWTH_SARIMA_LOWER_95_USD": growth_lower_95,
        "GROWTH_SARIMA_UPPER_95_USD": growth_upper_95,
        "PRIMARY_FORECAST_USD": counterfactual,
        "PRIMARY_MODEL": best_model,
        "GROWTH_MODEL": GROWTH_MODEL_NAME,
        "ESTIMATED_ANNUAL_TREND_RATE": estimated_annual_growth,
        "BACKTEST_WMAPE": float(growth_backtest["WMAPE"]),
        "PRESENTATION_READY": growth_sarima_presentation_ready,
    }
)
growth_results["GAP_VS_GROWTH_SARIMA_USD"] = (
    growth_results["ACTUAL_REVENUE_USD"]
    - growth_results["GROWTH_SARIMA_FORECAST_USD"]
)

growth_total = growth_results["GROWTH_SARIMA_FORECAST_USD"].sum()
growth_gap = actual_total - growth_total
print(f"Growth-aware SARIMA estimated annual pre-COVID trend: {estimated_annual_growth:.1%}")
print(f"Growth-aware SARIMA March-Dec forecast: ${growth_total:,.0f}")
print(f"Actual revenue vs growth-aware forecast: ${growth_gap:,.0f} ({growth_gap / growth_total:.1%})")
print(
    "Sensitivity model only. Its explicit growth trend assumes the historical "
    "log-revenue trend would have continued after February 2020."
)
if not growth_sarima_presentation_ready:
    print("Do not use growth-aware SARIMA in shareholder materials: its trend or backtest is not credible.")


# %% 5. Interrupted time-series regression
# Model specification: revenue = baseline + pre-trend + immediate level change
# + post-COVID trend change + calendar-month effects

its_data = monthly.reset_index().copy()
its_data["TIME"] = np.arange(len(its_data), dtype=int)
interruption_index = int(its_data.index[its_data["MONTH_START"] >= COVID_START][0])
its_data["COVID_PERIOD"] = (its_data["MONTH_START"] >= COVID_START).astype(int)
its_data["MONTHS_AFTER_COVID"] = np.maximum(its_data["TIME"] - interruption_index, 0)
its_data["MONTH_NUMBER"] = its_data["MONTH_START"].dt.month.astype("category")

its_model = smf.ols(
    "REVENUE ~ TIME + COVID_PERIOD + MONTHS_AFTER_COVID + C(MONTH_NUMBER)",
    data=its_data,
).fit(cov_type="HAC", cov_kwds={"maxlags": 12})

print(its_model.summary())

confidence_intervals = its_model.conf_int()
coefficient_results = pd.DataFrame(
    {
        "TERM": its_model.params.index,
        "ESTIMATE": its_model.params.values,
        "ROBUST_STD_ERROR": its_model.bse.values,
        "P_VALUE": its_model.pvalues.values,
        "LOWER_95": confidence_intervals[0].values,
        "UPPER_95": confidence_intervals[1].values,
    }
)

level_change = float(its_model.params["COVID_PERIOD"])
slope_change = float(its_model.params["MONTHS_AFTER_COVID"])
pre_trend = float(its_model.params["TIME"])
post_trend = pre_trend + slope_change

print(f"Adjusted immediate change in March 2020: ${level_change:,.0f}")
print(f"Pre-COVID monthly trend: ${pre_trend:,.0f}")
print(f"Change in monthly trend after March 2020: ${slope_change:,.0f}")
print(f"Estimated post-COVID monthly trend: ${post_trend:,.0f}")


# %% 6. Create the regression counterfactual series

counterfactual_design = its_data.copy()
post_mask = counterfactual_design["MONTH_START"] >= COVID_START
counterfactual_design.loc[post_mask, "COVID_PERIOD"] = 0
counterfactual_design.loc[post_mask, "MONTHS_AFTER_COVID"] = 0

its_data["ITS_FITTED_REVENUE"] = its_model.predict(its_data)
its_data["ITS_COUNTERFACTUAL_REVENUE"] = its_model.predict(counterfactual_design)
its_data["ITS_ESTIMATED_IMPACT"] = np.where(
    its_data["MONTH_START"] >= COVID_START,
    its_data["REVENUE"] - its_data["ITS_COUNTERFACTUAL_REVENUE"],
    np.nan,
)

its_results = its_data[
    [
        "MONTH_START",
        "REVENUE",
        "ITS_FITTED_REVENUE",
        "ITS_COUNTERFACTUAL_REVENUE",
        "ITS_ESTIMATED_IMPACT",
        "COVID_PERIOD",
    ]
].copy()


# %% 7. Create presentation charts

fig, ax = plt.subplots(figsize=(12, 6))
ax.plot(monthly.index, monthly["REVENUE"], color="#234E70", linewidth=2, label="Actual revenue")
ax.plot(
    forecast_results["MONTH_START"],
    forecast_results["FORECAST_REVENUE_NO_COVID"],
    color="#E07A5F",
    linewidth=2,
    linestyle="--",
    label=f"Pre-COVID benchmark ({best_model})",
)
if best_model != "Seasonal naive":
    ax.plot(
        forecast_results["MONTH_START"],
        forecast_results["SEASONAL_NAIVE_COMPARISON"],
        color="#66717D",
        linewidth=1.8,
        linestyle=":",
        label="Conservative comparison (seasonal naive)",
    )
ax.fill_between(
    forecast_results["MONTH_START"],
    forecast_results["LOWER_95_APPROX"],
    forecast_results["UPPER_95_APPROX"],
    color="#E07A5F",
    alpha=0.15,
    label="Approximate 95% range",
)
ax.axvline(COVID_START, color="#B22222", linestyle=":", linewidth=2, label="March 2020")
ax.set_title("Monthly Revenue: Actual vs Pre-COVID Counterfactual")
ax.set_ylabel("Revenue (USD)")
ax.grid(axis="y", alpha=0.2)
ax.legend(frameon=False)
plt.tight_layout()
plt.show()

fig, ax = plt.subplots(figsize=(12, 6))
ax.plot(
    monthly.index,
    monthly["REVENUE"],
    color="#234E70",
    linewidth=2,
    label="Actual revenue",
)
ax.plot(
    growth_results["MONTH_START"],
    growth_results["GROWTH_SARIMA_FORECAST_USD"],
    color="#B35C44",
    linewidth=2,
    linestyle="--",
    label="Growth-aware SARIMA forecast",
)
ax.plot(
    growth_results["MONTH_START"],
    growth_results["PRIMARY_FORECAST_USD"],
    color="#66717D",
    linewidth=1.8,
    linestyle=":",
    label=f"Primary benchmark ({best_model})",
)
ax.fill_between(
    growth_results["MONTH_START"],
    growth_results["GROWTH_SARIMA_LOWER_95_USD"],
    growth_results["GROWTH_SARIMA_UPPER_95_USD"],
    color="#B35C44",
    alpha=0.14,
    label="Growth-aware SARIMA 95% prediction interval",
)
ax.axvline(COVID_START, color="#B22222", linestyle=":", linewidth=2)
ax.set_title("Diagnostic Only: Growth-Aware SARIMA Sensitivity")
ax.set_ylabel("Revenue (USD)")
ax.grid(axis="y", alpha=0.2)
ax.legend(frameon=False)
plt.tight_layout()
plt.show()

fig, ax = plt.subplots(figsize=(12, 6))
ax.plot(its_results["MONTH_START"], its_results["REVENUE"], color="#234E70", label="Actual")
ax.plot(
    its_results["MONTH_START"],
    its_results["ITS_FITTED_REVENUE"],
    color="#3D9970",
    linewidth=2,
    label="ITS fitted",
)
ax.plot(
    its_results.loc[post_mask, "MONTH_START"],
    its_results.loc[post_mask, "ITS_COUNTERFACTUAL_REVENUE"],
    color="#E07A5F",
    linewidth=2,
    linestyle="--",
    label="ITS no-interruption counterfactual",
)
ax.axvline(COVID_START, color="#B22222", linestyle=":", linewidth=2)
ax.set_title("Interrupted Time-Series Regression")
ax.set_ylabel("Revenue (USD)")
ax.grid(axis="y", alpha=0.2)
ax.legend(frameon=False)
plt.tight_layout()
plt.show()


# %% 8. Save clean outputs to Snowflake for Tableau

def save_pandas_table(dataframe, table_name):
    output = dataframe.copy()
    output.columns = output.columns.str.upper()
    session.write_pandas(
        output,
        table_name=table_name,
        database=OUTPUT_DATABASE,
        schema=OUTPUT_SCHEMA,
        auto_create_table=True,
        overwrite=True,
        quote_identifiers=False,
    )
    print(f"Saved {len(output):,} rows to {table_name}")


save_pandas_table(forecast_results, FORECAST_OUTPUT_TABLE)
save_pandas_table(growth_results, GROWTH_OUTPUT_TABLE)
save_pandas_table(its_results, ITS_OUTPUT_TABLE)
save_pandas_table(model_comparison, MODEL_COMPARISON_TABLE)
save_pandas_table(rolling_comparison, ROLLING_COMPARISON_TABLE)
save_pandas_table(coefficient_results, COEFFICIENT_OUTPUT_TABLE)
