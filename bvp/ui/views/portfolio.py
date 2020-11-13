from datetime import timedelta
from typing import Dict

from flask import request, session
from flask_security import roles_accepted
from flask_security.core import current_user
import pandas as pd
import numpy as np
from bokeh.embed import components
import bokeh.palettes as palettes

from bvp.data.models.assets import Power
from bvp.data.models.markets import Price
from bvp.data.services.resources import Resource, get_assets
from bvp.data.queries.utils import simplify_index
from bvp.utils import time_utils
from bvp.utils.bvp_inflection import capitalize
import bvp.ui.utils.plotting_utils as plotting
from bvp.ui.views import bvp_ui
from bvp.ui.utils.view_utils import render_bvp_template


@bvp_ui.route("/portfolio", methods=["GET", "POST"])
@roles_accepted("admin", "Prosumer")
def portfolio_view():  # noqa: C901
    """Portfolio view.
    By default, this page shows live results (production, consumption and market data) from the user's portfolio.
    Time windows for which the platform has identified upcoming balancing opportunities are highlighted.
    The page can also be used to navigate historical results.
    """

    time_utils.set_time_range_for_session()
    start = session.get("start_time")
    end = session.get("end_time")
    resolution = session.get("resolution")

    # Get plot perspective
    perspectives = ["production", "consumption"]
    default_stack_side = "production"  # todo: move to user config setting
    show_stacked = request.values.get("show_stacked", default_stack_side)
    perspectives.remove(show_stacked)
    show_summed: str = perspectives[0]
    plot_label = f"Stacked {show_stacked} vs aggregated {show_summed}"

    # Set up a resource name for each asset type
    assets = get_assets(order_by_asset_attribute="display_name", order_direction="asc")
    represented_asset_types = {
        asset_type.plural_name: asset_type
        for asset_type in [asset.asset_type for asset in assets]
    }

    # Load structure (and set up resources)
    resource_dict = {}
    markets = []
    for resource_name in represented_asset_types.keys():
        resource = Resource(resource_name)
        if len(resource.assets) == 0:
            continue
        resource_dict[resource_name] = resource
        markets.extend(set(asset.market for asset in resource.assets))
    markets = set(markets)

    # Load power data (separate demand and supply, and group data per resource)
    supply_resources_df_dict = {}  # power >= 0, production/supply >= 0
    demand_resources_df_dict = {}  # power <= 0, consumption/demand >=0 !!!
    production_per_asset = {}
    consumption_per_asset = {}
    for resource_name, resource in resource_dict.items():
        resource.get_sensor_data(
            sensor_type=Power,
            start=start,
            end=end,
            resolution=resolution,
            sum_multiple=False,
        )  # The resource caches the results
        if (resource.aggregate_demand.values != 0).any():
            demand_resources_df_dict[resource_name] = simplify_index(
                resource.aggregate_demand
            )
        if (resource.aggregate_supply.values != 0).any():
            supply_resources_df_dict[resource_name] = simplify_index(
                resource.aggregate_supply
            )
        production_per_asset = {**production_per_asset, **resource.total_supply}
        consumption_per_asset = {**consumption_per_asset, **resource.total_demand}
    production_per_asset_type = {
        k: v.total_aggregate_supply for k, v in resource_dict.items()
    }
    consumption_per_asset_type = {
        k: v.total_aggregate_demand for k, v in resource_dict.items()
    }

    # Pick a perspective for summing and for stacking
    sum_dict = (
        demand_resources_df_dict.values()
        if show_summed == "consumption"
        else supply_resources_df_dict.values()
    )
    power_sum_df = (
        pd.concat(sum_dict, axis=1).sum(axis=1).to_frame(name="event_value")
        if sum_dict
        else pd.DataFrame()
    )
    stack_dict = (
        rename_each_value_column(supply_resources_df_dict).values()
        if show_summed == "consumption"
        else rename_each_value_column(demand_resources_df_dict).values()
    )
    df_stacked_data = pd.concat(stack_dict, axis=1) if stack_dict else pd.DataFrame()

    # Flexibility numbers are mocked for now
    curtailment_per_asset = {a.name: 0 for a in assets}
    shifting_per_asset = {a.name: 0 for a in assets}
    profit_loss_flexibility_per_asset = {a.name: 0 for a in assets}
    curtailment_per_asset_type = {k: 0 for k in represented_asset_types.keys()}
    shifting_per_asset_type = {k: 0 for k in represented_asset_types.keys()}
    profit_loss_flexibility_per_asset_type = {
        k: 0 for k in represented_asset_types.keys()
    }
    shifting_per_asset["48_r"] = 1.1
    profit_loss_flexibility_per_asset["48_r"] = 76000
    shifting_per_asset_type["one-way EVSE"] = shifting_per_asset["48_r"]
    profit_loss_flexibility_per_asset_type[
        "one-way EVSE"
    ] = profit_loss_flexibility_per_asset["48_r"]
    curtailment_per_asset["hw-onshore"] = 1.3
    profit_loss_flexibility_per_asset["hw-onshore"] = 84000
    curtailment_per_asset_type["wind turbines"] = curtailment_per_asset["hw-onshore"]
    profit_loss_flexibility_per_asset_type[
        "wind turbines"
    ] = profit_loss_flexibility_per_asset["hw-onshore"]

    # Load price data
    price_bdf_dict = {}
    for resource_name, resource in resource_dict.items():
        price_bdf_dict = resource.get_sensor_data(
            sensor_type=Price,
            sensor_key_attribute="market.name",
            start=start,
            end=end,
            resolution=resolution,
            sum_multiple=False,
            prior_data=price_bdf_dict,
            clear_cached_data=False,
        )
    average_price_dict = {k: v["event_value"].mean() for k, v in price_bdf_dict.items()}

    # Uncomment if needed
    # revenue_per_asset_type = {k: v.aggregate_revenue for k, v in resource_dict.items()}
    # cost_per_asset_type = {k: v.aggregate_cost for k, v in resource_dict.items()}
    # profit_per_asset_type = {k: v.aggregate_profit_or_loss for k, v in resource_dict.items()}

    # Create summed plot
    power_sum_df = data_or_zeroes(power_sum_df, start, end, resolution)

    this_hour = time_utils.get_most_recent_hour()
    next4am = [
        dt
        for dt in [this_hour + timedelta(hours=i) for i in range(1, 25)]
        if dt.hour == 4
    ][0]
    x_range = plotting.make_range(
        pd.date_range(start, end, freq=resolution, closed="left")
    )
    fig_profile = plotting.create_graph(
        power_sum_df,
        unit="MW",
        title=plot_label,
        x_range=x_range,
        x_label="Time (resolution of %s)"
        % time_utils.freq_label_to_human_readable_label(resolution),
        y_label="Power (in MW)",
        legend_location="top_right",
        legend_labels=(capitalize(show_summed), None, None),
        show_y_floats=True,
        non_negative_only=True,
    )

    # TODO: show when user has (possible) actions in order book for a time slot
    if current_user.is_authenticated and (
        current_user.has_role("admin")
        or "wind" in current_user.email
        or "charging" in current_user.email
    ):
        plotting.highlight(
            fig_profile, next4am, next4am + timedelta(hours=1), redirect_to="/control"
        )

    fig_profile.plot_height = 450
    fig_profile.plot_width = 900

    # Create stacked plot
    df_stacked_data = data_or_zeroes(df_stacked_data, start, end, resolution)
    df_stacked_areas = stack_df(df_stacked_data)

    num_areas = df_stacked_areas.shape[1]
    if num_areas <= 2:
        colors = ["#99d594", "#dddd9d"]
    else:
        colors = palettes.brewer["Spectral"][num_areas]

    df_stacked_data = time_utils.tz_index_naively(df_stacked_data)
    x_points = np.hstack((df_stacked_data.index[::-1], df_stacked_data.index))

    fig_profile.grid.minor_grid_line_color = "#eeeeee"

    for a, area in enumerate(df_stacked_areas):
        fig_profile.patch(
            x_points,
            df_stacked_areas[area].values,
            color=colors[a],
            alpha=0.8,
            line_color=None,
            legend=df_stacked_data.columns[a],
            level="underlay",
        )

    # actions
    df_actions = pd.DataFrame(index=power_sum_df.index, columns=["event_value"]).fillna(
        0
    )
    if next4am in df_actions.index:
        if current_user.is_authenticated:
            if current_user.has_role("admin"):
                df_actions.loc[next4am] = -2.4  # mock two actions
            elif "wind" in current_user.email:
                df_actions.loc[next4am] = -1.3  # mock one action
            elif "charging" in current_user.email:
                df_actions.loc[next4am] = -1.1  # mock one action
    next2am = [
        dt
        for dt in [this_hour + timedelta(hours=i) for i in range(1, 25)]
        if dt.hour == 2
    ][0]
    if next2am in df_actions.index:
        if next2am < next4am and (
            current_user.is_authenticated
            and (
                current_user.has_role("admin")
                or "wind" in current_user.email
                or "charging" in current_user.email
            )
        ):
            # mock the shift "payback" (actually occurs earlier in our mock example)
            df_actions.loc[next2am] = 1.1
    next9am = [
        dt
        for dt in [this_hour + timedelta(hours=i) for i in range(1, 25)]
        if dt.hour == 9
    ][0]
    if next9am in df_actions.index:
        # mock some other ordered actions that are not in an opportunity hour anymore
        df_actions.loc[next9am] = 3.5

    fig_actions = plotting.create_graph(
        df_actions,
        unit="MW",
        title="Ordered balancing actions",
        x_range=x_range,
        y_label="Power (in MW)",
    )
    if current_user.is_authenticated and (
        current_user.has_role("admin")
        or "wind" in current_user.email
        or "charging" in current_user.email
    ):
        plotting.highlight(
            fig_actions, next4am, next4am + timedelta(hours=1), redirect_to="/control"
        )

    fig_actions.plot_height = 150
    fig_actions.plot_width = fig_profile.plot_width
    fig_actions.xaxis.visible = False

    portfolio_plots_script, portfolio_plots_divs = components(
        (fig_profile, fig_actions)
    )
    next24hours = [
        (time_utils.get_most_recent_hour() + timedelta(hours=i)).strftime("%I:00 %p")
        for i in range(1, 26)
    ]

    return render_bvp_template(
        "views/portfolio.html",
        assets=assets,
        average_prices=average_price_dict,
        asset_types=represented_asset_types,
        markets=markets,
        production_per_asset=production_per_asset,
        consumption_per_asset=consumption_per_asset,
        curtailment_per_asset=curtailment_per_asset,
        shifting_per_asset=shifting_per_asset,
        profit_loss_flexibility_per_asset=profit_loss_flexibility_per_asset,
        production_per_asset_type=production_per_asset_type,
        consumption_per_asset_type=consumption_per_asset_type,
        curtailment_per_asset_type=curtailment_per_asset_type,
        shifting_per_asset_type=shifting_per_asset_type,
        profit_loss_flexibility_per_asset_type=profit_loss_flexibility_per_asset_type,
        sum_production=sum(production_per_asset_type.values()),
        sum_consumption=sum(consumption_per_asset_type.values()),
        sum_curtailment=sum(curtailment_per_asset_type.values()),
        sum_shifting=sum(shifting_per_asset_type.values()),
        sum_profit_loss_flexibility=sum(
            profit_loss_flexibility_per_asset_type.values()
        ),
        portfolio_plots_script=portfolio_plots_script,
        portfolio_plots_divs=portfolio_plots_divs,
        next24hours=next24hours,
        alt_stacking=show_summed,
    )


def data_or_zeroes(df: pd.DataFrame, start, end, resolution) -> pd.DataFrame:
    """Making really sure we have the structure to let the plots not fail"""
    if df is None or df.empty:
        return pd.DataFrame(
            index=pd.date_range(
                start=start,
                end=end,
                freq=resolution,
                tz=time_utils.get_timezone(),
                closed="left",
            ),
            columns=["event_value"],
        ).fillna(0)
    else:
        return df


def stack_df(df: pd.DataFrame) -> pd.DataFrame:
    """Stack columns of df cumulatively, include bottom"""
    df_top = df.cumsum(axis=1)
    df_bottom = df_top.shift(axis=1).fillna(0)[::-1]
    df_stack = pd.concat([df_bottom, df_top], ignore_index=True)
    return df_stack


def rename_each_value_column(
    df_dict: Dict[str, pd.DataFrame]
) -> Dict[str, pd.DataFrame]:
    return {
        df_name: df.rename(columns={"event_value": capitalize(df_name)})
        for df_name, df in df_dict.items()
    }
