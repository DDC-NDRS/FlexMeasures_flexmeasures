from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from flexmeasures.data.models.reporting import Reporter
from flexmeasures.data.models.time_series import Sensor
from flexmeasures.data.schemas.reporting.aggregation import (
    AggregatorConfigSchema,
    AggregatorParametersSchema,
)

from flexmeasures.utils.time_utils import server_now


class AggregatorReporter(Reporter):
    """This reporter applies an aggregation function to multiple sensors"""

    __version__ = "1"
    __author__ = "Seita"

    _config_schema = AggregatorConfigSchema()
    _parameters_schema = AggregatorParametersSchema()

    weights: dict
    method: str

    def _compute_report(
        self,
        start: datetime,
        end: datetime,
        input: list[dict[str, Any]],
        output: list[dict[str, Any]],
        resolution: timedelta | None = None,
        belief_time: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """
        This method merges all the BeliefDataFrames into a single one, dropping
        all indexes but event_start, and applies an aggregation function over the
        columns.
        """

        method: str = self._config.get("method")
        weights: dict = self._config.get("weights", {})

        dataframes = []

        if belief_time is None:
            belief_time = server_now()

        for input_description in input:
            sensor: Sensor = input_description.pop("sensor")
            # if name is not in belief_search_config, using the Sensor id instead
            column_name = input_description.pop("name", f"sensor_{sensor.id}")

            source = input_description.pop(
                "source", input_description.pop("sources", None)
            )
            if source is not None and not isinstance(source, list):
                source = [source]

            df = sensor.search_beliefs(
                event_starts_after=start,
                event_ends_before=end,
                resolution=resolution,
                beliefs_before=belief_time,
                source=source,
                one_deterministic_belief_per_event=True,
                **input_description,
            )

            # Check for multi-sourced events (i.e. multiple sources for a single event)
            if len(df.lineage.events) != len(df):
                duplicate_events = df[
                    df.index.get_level_values("event_start").duplicated()
                ]
                raise ValueError(
                    f"{len(duplicate_events)} event(s) are duplicate. First duplicate: {duplicate_events[0]}. Consider using (more) source filters."
                )

            # Check for multiple sources within the entire frame (excluding different versions of the same source)
            unique_sources = df.lineage.sources
            properties = [
                "name",
                "type",
                "model",
            ]  # properties to identify different versions of the same source
            if (
                len(unique_sources) > 1
                and not all(
                    getattr(source, prop) == getattr(unique_sources[0], prop)
                    for prop in properties
                    for source in unique_sources
                )
                and (source is None or len(source) == 0)
            ):
                raise ValueError(
                    "Missing attribute source or sources. The fields `source` or `sources` is required when having multiple sources within the time window."
                )

            df = df.droplevel([1, 2, 3])

            # apply weight
            if column_name in weights:
                df *= weights[column_name]

            dataframes.append(df)

        output_df = pd.concat(dataframes, axis=1)

        # apply aggregation method
        output_df = output_df.aggregate(method, axis=1)

        # convert BeliefsSeries into a BeliefsDataFrame
        output_df = output_df.to_frame("event_value")
        output_df["belief_time"] = belief_time
        output_df["cumulative_probability"] = 0.5
        output_df["source"] = self.data_source
        output_df.sensor = output[0]["sensor"]
        output_df.event_resolution = output[0]["sensor"].event_resolution

        output_df = output_df.set_index(
            ["belief_time", "source", "cumulative_probability"], append=True
        )

        return [
            {
                "name": "aggregate",
                "column": "event_value",
                "sensor": output[0]["sensor"],
                "data": output_df,
            }
        ]
