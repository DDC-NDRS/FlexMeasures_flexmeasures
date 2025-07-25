from __future__ import annotations

import isodate
from datetime import datetime, timedelta

from flexmeasures.data.services.sensors import (
    serialize_sensor_status_data,
)

from werkzeug.exceptions import Unauthorized
from flask import current_app, url_for
from flask_classful import FlaskView, route
from flask_json import as_json
from flask_security import auth_required, current_user
from marshmallow import fields, ValidationError
import marshmallow.validate as validate
from rq.job import Job, NoSuchJobError
import timely_beliefs as tb
from webargs.flaskparser import use_args, use_kwargs
from sqlalchemy import delete, select, or_

from flexmeasures.api.common.responses import (
    request_processed,
    unrecognized_event,
    unknown_schedule,
    invalid_flex_config,
    fallback_schedule_redirect,
)
from flexmeasures.api.common.utils.validators import (
    optional_duration_accepted,
)
from flexmeasures.api.common.schemas.sensor_data import (
    GetSensorDataSchema,
    PostSensorDataSchema,
)
from flexmeasures.api.common.schemas.users import AccountIdField
from flexmeasures.api.common.utils.api_utils import save_and_enqueue
from flexmeasures.auth.policy import check_access
from flexmeasures.auth.decorators import permission_required_for_context
from flexmeasures.data import db
from flexmeasures.data.models.audit_log import AssetAuditLog
from flexmeasures.data.models.user import Account
from flexmeasures.data.models.generic_assets import GenericAsset
from flexmeasures.data.models.time_series import Sensor, TimedBelief
from flexmeasures.data.queries.utils import simplify_index
from flexmeasures.data.schemas.sensors import (
    SensorSchema,
    SensorIdField,
    SensorDataFileSchema,
)
from flexmeasures.data.schemas.times import AwareDateTimeField, PlanningDurationField
from flexmeasures.data.schemas.utils import path_and_files
from flexmeasures.data.schemas import AssetIdField
from flexmeasures.api.common.schemas.search import SearchFilterField
from flexmeasures.api.common.schemas.sensors import UnitField
from flexmeasures.data.services.sensors import get_sensor_stats
from flexmeasures.data.services.scheduling import (
    create_scheduling_job,
    get_data_source_for_job,
)
from flexmeasures.utils.time_utils import duration_isoformat
from flexmeasures.utils.flexmeasures_inflection import join_words_into_a_list


# Instantiate schemas outside of endpoint logic to minimize response time
get_sensor_schema = GetSensorDataSchema()
post_sensor_schema = PostSensorDataSchema()
sensors_schema = SensorSchema(many=True)
sensor_schema = SensorSchema()
partial_sensor_schema = SensorSchema(partial=True, exclude=["generic_asset_id"])


class SensorAPI(FlaskView):
    route_base = "/sensors"
    trailing_slash = False
    decorators = [auth_required()]

    @route("", methods=["GET"])
    @use_kwargs(
        {
            "account": AccountIdField(data_key="account_id", required=False),
            "asset": AssetIdField(data_key="asset_id", required=False),
            "include_consultancy_clients": fields.Boolean(
                required=False, load_default=False
            ),
            "include_public_assets": fields.Boolean(required=False, load_default=False),
            "page": fields.Int(required=False, validate=validate.Range(min=1)),
            "per_page": fields.Int(
                required=False, validate=validate.Range(min=1), load_default=10
            ),
            "filter": SearchFilterField(required=False),
            "unit": UnitField(required=False),
        },
        location="query",
    )
    @as_json
    def index(
        self,
        account: Account | None = None,
        asset: GenericAsset | None = None,
        include_consultancy_clients: bool = False,
        include_public_assets: bool = False,
        page: int | None = None,
        per_page: int | None = None,
        filter: list[str] | None = None,
        unit: str | None = None,
    ):
        """API endpoint to list all sensors of an account.

        .. :quickref: Sensor; Get list of sensors

        This endpoint returns all accessible sensors.
        By default, "accessible sensors" means all sensors in the same account as the current user (if they have read permission to the account).

        You can also specify an `account` (an ID parameter), if the user has read access to that account. In this case, all assets under the
        specified account will be retrieved, and the sensors associated with these assets will be returned.

        Alternatively, you can filter by asset hierarchy by providing the `asset` parameter (ID). When this is set, all sensors on the specified
        asset and its sub-assets are retrieved, provided the user has read access to the asset.

        NOTE: You can't set both account and asset at the same time, you can only have one set. The only exception is if the asset being specified is
        part of the account that was set, then we allow to see sensors under that asset but then ignore the account (account = None).

        Finally, you can use the `include_consultancy_clients` parameter to include sensors from accounts for which the current user account is a consultant.
        This is only possible if the user has the role of a consultant.

        Only admins can use this endpoint to fetch sensors from a different account (by using the `account_id` query parameter).

        The `filter` parameter allows you to search for sensors by name or account name.
        The `unit` parameter allows you to filter by unit.

        For the pagination of the sensor list, you can use the `page` and `per_page` query parameters, the `page` parameter is used to trigger
        pagination, and the `per_page` parameter is used to specify the number of records per page. The default value for `page` is 1 and for `per_page` is 10.

        **Example response**

        An example of one sensor being returned:

        .. sourcecode:: json

            {
                "data" : [
                    {
                        "entity_address": "ea1.2021-01.io.flexmeasures.company:fm1.42",
                        "event_resolution": PT15M,
                        "generic_asset_id": 1,
                        "name": "Gas demand",
                        "timezone": "Europe/Amsterdam",
                        "unit": "m\u00b3/h"
                        "id": 2
                    }
                ],
                "num-records" : 1,
                "filtered-records" : 1
            }

        If no pagination is requested, the response only consists of the list under the "data" key.

        :reqheader Authorization: The authentication token
        :reqheader Content-Type: application/json
        :resheader Content-Type: application/json
        :status 200: PROCESSED
        :status 400: INVALID_REQUEST
        :status 401: UNAUTHORIZED
        :status 403: INVALID_SENDER
        :status 422: UNPROCESSABLE_ENTITY
        """
        if account is None and asset is None:
            if current_user.is_anonymous:
                raise Unauthorized
            account = current_user.account

        if account is not None and asset is not None:
            if asset.account_id != account.id:
                return {
                    "message": "Please provide either an account or an asset ID, not both"
                }, 422
            else:
                account = None

        if asset is not None:
            check_access(asset, "read")

            asset_tree = (
                db.session.query(GenericAsset.id, GenericAsset.parent_asset_id)
                .filter(GenericAsset.id == asset.id)
                .cte(name="asset_tree", recursive=True)
            )

            recursive_part = db.session.query(
                GenericAsset.id, GenericAsset.parent_asset_id
            ).join(asset_tree, GenericAsset.parent_asset_id == asset_tree.c.id)

            asset_tree = asset_tree.union(recursive_part)

            child_assets = db.session.query(asset_tree).all()

            filter_statement = GenericAsset.id.in_(
                [asset.id] + [a.id for a in child_assets]
            )
        elif account is not None:
            check_access(account, "read")

            account_ids: list = [account.id]

            if include_consultancy_clients:
                if current_user.has_role("consultant"):
                    consultancy_accounts = (
                        db.session.query(Account)
                        .filter(Account.consultancy_account_id == account.id)
                        .all()
                    )
                    account_ids.extend([acc.id for acc in consultancy_accounts])

            filter_statement = GenericAsset.account_id.in_(account_ids)
        else:
            filter_statement = None

        if include_public_assets:
            filter_statement = or_(
                filter_statement,
                GenericAsset.account_id.is_(None),
            )

        sensor_query = (
            select(Sensor)
            .join(GenericAsset, Sensor.generic_asset_id == GenericAsset.id)
            .outerjoin(Account, GenericAsset.owner)
            .filter(filter_statement)
        )

        if filter is not None:
            sensor_query = sensor_query.filter(
                or_(
                    *(
                        or_(
                            Sensor.name.ilike(f"%{term}%"),
                            Account.name.ilike(f"%{term}%"),
                            GenericAsset.name.ilike(f"%{term}%"),
                        )
                        for term in filter
                    )
                )
            )

        if unit:
            sensor_query = sensor_query.filter(Sensor.unit == unit)

        sensors = (
            db.session.scalars(sensor_query).all()
            if page is None
            else db.paginate(sensor_query, per_page=per_page, page=page).items
        )

        sensors = [sensor for sensor in sensors if check_access(sensor, "read") is None]

        sensors_response = sensors_schema.dump(sensors)

        # Return appropriate response for paginated or non-paginated data
        if page is None:
            return sensors_response, 200
        else:
            num_records = len(db.session.execute(sensor_query).scalars().all())
            select_pagination = db.paginate(sensor_query, per_page=per_page, page=page)
            response = {
                "data": sensors_response,
                "num-records": num_records,
                "filtered-records": select_pagination.total,
            }
            return response, 200

    @route("<id>/data/upload", methods=["POST"])
    @path_and_files(SensorDataFileSchema)
    @permission_required_for_context(
        "create-children",
        ctx_arg_name="data",
        ctx_loader=lambda data: data[0].sensor if data else None,
        pass_ctx_to_loader=True,
    )
    def upload_data(
        self, data: list[tb.BeliefsDataFrame], filenames: list[str], **kwargs
    ):
        """
        Post sensor data to FlexMeasures by file upload.

        .. :quickref: Data; Upload sensor data by file

        ** Example request **

        .. code-block:: json

            {
                "data": [
                    {
                        "uploaded-files": "[\"file1.csv\", \"file2.csv\"]"
                    }
                ]
            }

        The file should have columns for a timestamp (event_start) and a value (event_value).
        The timestamp should be in ISO 8601 format.
        The value should be a numeric value.

        The unit has to be convertible to the sensor's unit.
        The resolution of the data has to match the sensor's required resolution, but
        FlexMeasures will attempt to upsample lower resolutions.
        The list of values may include null values.

        :reqheader Authorization: The authentication token
        :reqheader Content-Type: multipart/form-data
        :resheader Content-Type: application/json
        :status 200: PROCESSED
        :status 400: INVALID_REQUEST
        """
        sensor = data[0].sensor
        AssetAuditLog.add_record(
            sensor.generic_asset,
            f"Data from {join_words_into_a_list(filenames)} uploaded to sensor '{sensor.name}': {sensor.id}",
        )
        response, code = save_and_enqueue(data)
        return response, code

    @route("/data", methods=["POST"])
    @use_args(
        post_sensor_schema,
        location="json",
    )
    @permission_required_for_context(
        "create-children",
        ctx_arg_pos=1,
        ctx_loader=lambda bdf: bdf.sensor,
        pass_ctx_to_loader=True,
    )
    def post_data(self, bdf: tb.BeliefsDataFrame):
        """
        Post sensor data to FlexMeasures.

        .. :quickref: Data; Upload sensor data

        **Example request**

        .. code-block:: json

            {
                "sensor": "ea1.2021-01.io.flexmeasures:fm1.1",
                "values": [-11.28, -11.28, -11.28, -11.28],
                "start": "2021-06-07T00:00:00+02:00",
                "duration": "PT1H",
                "unit": "m³/h"
            }

        The above request posts four values for a duration of one hour, where the first
        event start is at the given start time, and subsequent events start in 15 minute intervals throughout the one hour duration.

        The sensor is the one with ID=1.
        The unit has to be convertible to the sensor's unit.
        The resolution of the data has to match the sensor's required resolution, but
        FlexMeasures will attempt to upsample lower resolutions.
        The list of values may include null values.

        :reqheader Authorization: The authentication token
        :reqheader Content-Type: application/json
        :resheader Content-Type: application/json
        :status 200: PROCESSED
        :status 400: INVALID_REQUEST
        :status 401: UNAUTHORIZED
        :status 403: INVALID_SENDER
        :status 422: UNPROCESSABLE_ENTITY
        """
        response, code = save_and_enqueue(bdf)
        return response, code

    @route("/data", methods=["GET"])
    @use_args(
        get_sensor_schema,
        location="query",
    )
    @permission_required_for_context("read", ctx_arg_pos=1, ctx_arg_name="sensor")
    def get_data(self, sensor_data_description: dict):
        """Get sensor data from FlexMeasures.

        .. :quickref: Data; Download sensor data

        **Example request**

        .. code-block:: json

            {
                "sensor": "ea1.2021-01.io.flexmeasures:fm1.1",
                "start": "2021-06-07T00:00:00+02:00",
                "duration": "PT1H",
                "resolution": "PT15M",
                "unit": "m³/h"
            }

        The unit has to be convertible from the sensor's unit.

        **Optional fields**

        - "resolution" (see :ref:`frequency_and_resolution`)
        - "horizon" (see :ref:`beliefs`)
        - "prior" (see :ref:`beliefs`)
        - "source" (see :ref:`sources`)

        :reqheader Authorization: The authentication token
        :reqheader Content-Type: application/json
        :resheader Content-Type: application/json
        :status 200: PROCESSED
        :status 400: INVALID_REQUEST
        :status 401: UNAUTHORIZED
        :status 403: INVALID_SENDER
        :status 422: UNPROCESSABLE_ENTITY
        """
        response = GetSensorDataSchema.load_data_and_make_response(
            sensor_data_description
        )
        d, s = request_processed()
        return dict(**response, **d), s

    @route("/<id>/schedules/trigger", methods=["POST"])
    @use_kwargs(
        {"sensor": SensorIdField(data_key="id")},
        location="path",
    )
    @use_kwargs(
        {
            "start_of_schedule": AwareDateTimeField(
                data_key="start", format="iso", required=True
            ),
            "belief_time": AwareDateTimeField(format="iso", data_key="prior"),
            "duration": PlanningDurationField(
                load_default=PlanningDurationField.load_default
            ),
            "flex_model": fields.Dict(data_key="flex-model"),
            "flex_context": fields.Dict(required=False, data_key="flex-context"),
            "force_new_job_creation": fields.Boolean(required=False),
        },
        location="json",
    )
    @permission_required_for_context("create-children", ctx_arg_name="sensor")
    def trigger_schedule(
        self,
        sensor: Sensor,
        start_of_schedule: datetime,
        duration: timedelta,
        belief_time: datetime | None = None,
        flex_model: dict | None = None,
        flex_context: dict | None = None,
        force_new_job_creation: bool | None = False,
        **kwargs,
    ):
        """
        Trigger FlexMeasures to create a schedule for a single flexible device, possibly taking into account inflexible devices.

        .. :quickref: Schedule; Trigger scheduling job for one device

        Trigger FlexMeasures to create a schedule for this sensor.
        The assumption is that this sensor is the power sensor on a flexible asset.

        In this request, you can describe:

        - the schedule's main features (when does it start, what unit should it report, prior to what time can we assume knowledge)
        - the flexibility model for the sensor (state and constraint variables, e.g. current state of charge of a battery, or connection capacity)
        - the flexibility context which the sensor operates in (other sensors under the same EMS which are relevant, e.g. prices)

        For details on flexibility model and context, see :ref:`describing_flexibility`.
        Below, we'll also list some examples.

        .. note:: To schedule an EMS with multiple flexible sensors at once,
                  use `this endpoint <../api/v3_0.html#post--api-v3_0-assets-(id)-schedules-trigger>`_ instead.

        The length of the schedule can be set explicitly through the 'duration' field.
        Otherwise, it is set by the config setting :ref:`planning_horizon_config`, which defaults to 48 hours.
        If the flex-model contains targets that lie beyond the planning horizon, the length of the schedule is extended to accommodate them.
        Finally, the schedule length is limited by :ref:`max_planning_horizon_config`, which defaults to 2520 steps of the sensor's resolution.
        Targets that exceed the max planning horizon are not accepted.

        The appropriate algorithm is chosen by FlexMeasures (based on asset type).
        It's also possible to use custom schedulers and custom flexibility models, see :ref:`plugin_customization`.

        If you have ideas for algorithms that should be part of FlexMeasures, let us know: https://flexmeasures.io/get-in-touch/

        **Example request A**

        This message triggers a schedule for a storage asset, starting at 10.00am, at which the state of charge (soc) is 12.1 kWh.

        .. code-block:: json

            {
                "start": "2015-06-02T10:00:00+00:00",
                "flex-model": {
                    "soc-at-start": "12.1 kWh"
                }
            }

        **Example request B**

        This message triggers a 24-hour schedule for a storage asset, starting at 10.00am,
        at which the state of charge (soc) is 12.1 kWh, with a target state of charge of 25 kWh at 4.00pm.

        The charging efficiency is constant (120%) and the discharging efficiency is determined by the contents of sensor
        with id 98. If just the ``roundtrip-efficiency`` is known, it can be described with its own field.
        The global minimum and maximum soc are set to 10 and 25 kWh, respectively.
        To guarantee a minimum SOC in the period prior, the sensor with ID 300 contains beliefs at 2.00pm and 3.00pm, for 15kWh and 20kWh, respectively.
        Storage efficiency is set to 99.99%, denoting the state of charge left after each time step equal to the sensor's resolution.
        Aggregate consumption (of all devices within this EMS) should be priced by sensor 9,
        and aggregate production should be priced by sensor 10,
        where the aggregate power flow in the EMS is described by the sum over sensors 13, 14, 15,
        and the power sensor of the flexible device being optimized (referenced in the endpoint URL).


        The battery consumption power capacity is limited by sensor 42 and the production capacity is constant (30 kW).

        Finally, the (contractual and physical) situation of the site is part of the flex-context.
        The site has a physical power capacity of 100 kVA, but the production capacity is limited to 80 kW,
        while the consumption capacity is limited by a dynamic capacity contract whose values are recorded under sensor 32.
        Breaching either capacity is penalized heavily in the optimization problem, with a price of 1000 EUR/kW.
        Finally, peaks over 50 kW in either direction are penalized with a price of 260 EUR/MW.
        These penalties can be used to steer the schedule into a certain behaviour (e.g. avoiding breaches and peaks),
        even if no direct financial impacts are expected at the given prices in the real world.
        For example, site owners may be requested by their network operators to reduce stress on the grid,
        be it explicitly or under a social contract.

        Note that, if forecasts for sensors 13, 14 and 15 are not available, a schedule cannot be computed.

        .. code-block:: json

            {
                "start": "2015-06-02T10:00:00+00:00",
                "duration": "PT24H",
                "flex-model": {
                    "soc-at-start": "12.1 kWh",
                    "state-of-charge" : {"sensor" : 24},
                    "soc-targets": [
                        {
                            "value": "25 kWh",
                            "datetime": "2015-06-02T16:00:00+00:00"
                        },
                    ],
                    "soc-minima": {"sensor" : 300},
                    "soc-min": "10 kWh",
                    "soc-max": "25 kWh",
                    "charging-efficiency": "120%",
                    "discharging-efficiency": {"sensor": 98},
                    "storage-efficiency": 0.9999,
                    "power-capacity": "25kW",
                    "consumption-capacity" : {"sensor": 42},
                    "production-capacity" : "30 kW"
                },
                "flex-context": {
                    "consumption-price": {"sensor": 9},
                    "production-price": {"sensor": 10},
                    "inflexible-device-sensors": [13, 14, 15],
                    "site-power-capacity": "100 kVA",
                    "site-production-capacity": "80 kW",
                    "site-consumption-capacity": {"sensor": 32},
                    "site-production-breach-price": "1000 EUR/kW",
                    "site-consumption-breach-price": "1000 EUR/kW",
                    "site-peak-consumption": "50 kW",
                    "site-peak-production": "50 kW",
                    "site-peak-consumption-price": "260 EUR/MW",
                    "site-peak-production-price": "260 EUR/MW"
                }
            }

        **Example response**

        This message indicates that the scheduling request has been processed without any error.
        A scheduling job has been created with some Universally Unique Identifier (UUID),
        which will be picked up by a worker.
        The given UUID may be used to obtain the resulting schedule: see /sensors/<id>/schedules/<uuid>.

        .. sourcecode:: json

            {
                "status": "PROCESSED",
                "schedule": "364bfd06-c1fa-430b-8d25-8f5a547651fb",
                "message": "Request has been processed."
            }

        :reqheader Authorization: The authentication token
        :reqheader Content-Type: application/json
        :resheader Content-Type: application/json
        :status 200: PROCESSED
        :status 400: INVALID_DATA
        :status 401: UNAUTHORIZED
        :status 403: INVALID_SENDER
        :status 405: INVALID_METHOD
        :status 422: UNPROCESSABLE_ENTITY
        """
        end_of_schedule = start_of_schedule + duration
        scheduler_kwargs = dict(
            asset_or_sensor=sensor,
            start=start_of_schedule,
            end=end_of_schedule,
            resolution=sensor.event_resolution,
            belief_time=belief_time,  # server time if no prior time was sent
            flex_model=flex_model,
            flex_context=flex_context,
        )

        try:
            job = create_scheduling_job(
                **scheduler_kwargs,
                enqueue=True,
                force_new_job_creation=force_new_job_creation,
            )
        except ValidationError as err:
            return invalid_flex_config(err.messages)
        except ValueError as err:
            return invalid_flex_config(str(err))

        db.session.commit()

        response = dict(schedule=job.id)
        d, s = request_processed()
        return dict(**response, **d), s

    @route("/<id>/schedules/<uuid>", methods=["GET"])
    @use_kwargs(
        {
            "sensor": SensorIdField(data_key="id"),
            "job_id": fields.Str(data_key="uuid"),
        },
        location="path",
    )
    @optional_duration_accepted(
        timedelta(hours=6)
    )  # todo: make this a Marshmallow field
    @permission_required_for_context("read", ctx_arg_name="sensor")
    def get_schedule(  # noqa: C901
        self, sensor: Sensor, job_id: str, duration: timedelta, **kwargs
    ):
        """Get a schedule from FlexMeasures.

        .. :quickref: Schedule; Download schedule for one device

        **Optional fields**

        - "duration" (6 hours by default; can be increased to plan further into the future)

        **Example response**

        This message contains a schedule indicating to consume at various power
        rates from 10am UTC onwards for a duration of 45 minutes.

        .. sourcecode:: json

            {
                "values": [
                    2.15,
                    3,
                    2
                ],
                "start": "2015-06-02T10:00:00+00:00",
                "duration": "PT45M",
                "unit": "MW"
            }

        :reqheader Authorization: The authentication token
        :reqheader Content-Type: application/json
        :resheader Content-Type: application/json
        :status 200: PROCESSED
        :status 400: INVALID_TIMEZONE, INVALID_DOMAIN, INVALID_UNIT, UNKNOWN_SCHEDULE, UNRECOGNIZED_CONNECTION_GROUP
        :status 401: UNAUTHORIZED
        :status 403: INVALID_SENDER
        :status 405: INVALID_METHOD
        :status 422: UNPROCESSABLE_ENTITY
        """

        planning_horizon = min(  # type: ignore
            duration, current_app.config.get("FLEXMEASURES_PLANNING_HORIZON")
        )

        # Look up the scheduling job
        connection = current_app.queues["scheduling"].connection

        try:  # First try the scheduling queue
            job = Job.fetch(job_id, connection=connection)
        except NoSuchJobError:
            return unrecognized_event(job_id, "job")

        if (
            not current_app.config.get("FLEXMEASURES_FALLBACK_REDIRECT")
            and job.is_failed
            and (job.meta.get("fallback_job_id") is not None)
        ):
            try:  # First try the scheduling queue
                job = Job.fetch(job.meta["fallback_job_id"], connection=connection)
            except NoSuchJobError:
                current_app.logger.error(
                    f"Fallback job with ID={job.meta['fallback_job_id']} (originator Job ID={job_id}) not found."
                )
                return unrecognized_event(job.meta["fallback_job_id"], "fallback-job")

        scheduler_info = job.meta.get("scheduler_info", dict(scheduler=""))
        scheduler_info_msg = f"{scheduler_info['scheduler']} was used."

        if job.is_finished:
            error_message = "A scheduling job has been processed with your job ID, but "

        elif job.is_failed:  # Try to inform the user on why the job failed
            e = job.meta.get(
                "exception",
                Exception(
                    "The job does not state why it failed. "
                    "The worker may be missing an exception handler, "
                    "or its exception handler is not storing the exception as job meta data."
                ),
            )
            message = f"Scheduling job failed with {type(e).__name__}: {e}. {scheduler_info_msg}"

            fallback_job_id = job.meta.get("fallback_job_id")

            # redirect to the fallback schedule endpoint if the fallback_job_id
            # is defined in the metadata of the original job
            if fallback_job_id is not None:
                return fallback_schedule_redirect(
                    message,
                    url_for(
                        "SensorAPI:get_schedule",
                        uuid=fallback_job_id,
                        id=sensor.id,
                        _external=True,
                    ),
                )
            else:
                return unknown_schedule(message)

        elif job.is_started:
            return unknown_schedule(f"Scheduling job in progress. {scheduler_info_msg}")
        elif job.is_queued:
            return unknown_schedule(
                f"Scheduling job waiting to be processed. {scheduler_info_msg}"
            )
        elif job.is_deferred:
            try:
                preferred_job = job.dependency
            except NoSuchJobError:
                return unknown_schedule(
                    f"Scheduling job waiting for unknown job to be processed. {scheduler_info_msg}"
                )
            return unknown_schedule(
                f'Scheduling job waiting for {preferred_job.status} job "{preferred_job.id}" to be processed. {scheduler_info_msg}'
            )
        else:
            return unknown_schedule(
                f"Scheduling job has an unknown status. {scheduler_info_msg}"
            )
        schedule_start = job.kwargs["start"]

        data_source = get_data_source_for_job(job)
        if data_source is None:
            return unknown_schedule(
                error_message
                + f"no data source could be found for {data_source}. {scheduler_info_msg}"
            )

        power_values = sensor.search_beliefs(
            event_starts_after=schedule_start,
            event_ends_before=schedule_start + planning_horizon,
            source=data_source,
            most_recent_beliefs_only=True,
            one_deterministic_belief_per_event=True,
        )

        sign = 1
        if sensor.measures_power and sensor.get_attribute(
            "consumption_is_positive", True
        ):
            sign = -1

        # For consumption schedules, positive values denote consumption. For the db, consumption is negative
        consumption_schedule = sign * simplify_index(power_values)["event_value"]
        if consumption_schedule.empty:
            return unknown_schedule(
                f"{error_message} the schedule was not found in the database. {scheduler_info_msg}"
            )

        # Update the planning window
        resolution = sensor.event_resolution
        start = consumption_schedule.index[0]
        duration = min(duration, consumption_schedule.index[-1] + resolution - start)
        consumption_schedule = consumption_schedule[
            start : start + duration - resolution
        ]
        response = dict(
            values=consumption_schedule.tolist(),
            start=isodate.datetime_isoformat(start),
            duration=duration_isoformat(duration),
            unit=sensor.unit,
        )

        d, s = request_processed(scheduler_info_msg)
        return dict(scheduler_info=scheduler_info, **response, **d), s

    @route("/<id>", methods=["GET"])
    @use_kwargs({"sensor": SensorIdField(data_key="id")}, location="path")
    @permission_required_for_context("read", ctx_arg_name="sensor")
    @as_json
    def fetch_one(self, id, sensor):
        """Fetch a given sensor.

        .. :quickref: Sensor; Get a sensor

        This endpoint gets a sensor.

        **Example response**

        .. sourcecode:: json

            {
                "name": "some gas sensor",
                "unit": "m³/h",
                "entity_address": "ea1.2023-08.localhost:fm1.1",
                "event_resolution": "PT10M",
                "generic_asset_id": 4,
                "timezone": "UTC",
                "id": 2
            }

        :reqheader Authorization: The authentication token
        :reqheader Content-Type: application/json
        :resheader Content-Type: application/json
        :status 200: PROCESSED
        :status 400: INVALID_REQUEST, REQUIRED_INFO_MISSING, UNEXPECTED_PARAMS
        :status 401: UNAUTHORIZED
        :status 403: INVALID_SENDER
        :status 422: UNPROCESSABLE_ENTITY
        """

        return sensor_schema.dump(sensor), 200

    @route("", methods=["POST"])
    @use_args(sensor_schema)
    @permission_required_for_context(
        "create-children",
        ctx_arg_pos=1,
        ctx_arg_name="generic_asset_id",
        ctx_loader=GenericAsset,
        pass_ctx_to_loader=True,
    )
    def post(self, sensor_data: dict):
        """Create new sensor.

        .. :quickref: Sensor; Create a new Sensor

        This endpoint creates a new Sensor.

        **Example request**

        .. sourcecode:: json

            {
                "name": "power",
                "event_resolution": "PT1H",
                "unit": "kWh",
                "generic_asset_id": 1,
            }

        **Example response**

        The whole sensor is returned in the response:

        .. sourcecode:: json

            {
                "name": "power",
                "unit": "kWh",
                "entity_address": "ea1.2023-08.localhost:fm1.1",
                "event_resolution": "PT1H",
                "generic_asset_id": 1,
                "timezone": "UTC",
                "id": 2
            }

        :reqheader Authorization: The authentication token
        :reqheader Content-Type: application/json
        :resheader Content-Type: application/json
        :status 201: CREATED
        :status 400: INVALID_REQUEST
        :status 401: UNAUTHORIZED
        :status 403: INVALID_SENDER
        :status 422: UNPROCESSABLE_ENTITY
        """
        sensor = Sensor(**sensor_data)
        db.session.add(sensor)
        db.session.commit()

        asset = sensor_schema.context["generic_asset"]
        AssetAuditLog.add_record(asset, f"Created sensor '{sensor.name}': {sensor.id}")

        return sensor_schema.dump(sensor), 201

    @route("/<id>", methods=["PATCH"])
    @use_args(partial_sensor_schema)
    @use_kwargs({"sensor": SensorIdField(data_key="id")}, location="path")
    @permission_required_for_context("update", ctx_arg_name="sensor")
    @as_json
    def patch(self, sensor_data: dict, id: int, sensor: Sensor):
        """Update a sensor given its identifier.

        .. :quickref: Sensor; Update a sensor

        This endpoint updates the descriptive data of an existing sensor.

        Any subset of sensor fields can be sent.
        However, the following fields are not allowed to be updated:
        - id
        - generic_asset_id
        - entity_address

        Only admin users have rights to update the sensor fields. Be aware that changing unit, event resolution and knowledge horizon should currently only be done on sensors without existing belief data (to avoid a serious mismatch), or if you really know what you are doing.

        **Example request**

        .. sourcecode:: json

            {
                "name": "POWER",
            }

        **Example response**

        The whole sensor is returned in the response:

        .. sourcecode:: json

            {
                "name": "some gas sensor",
                "unit": "m³/h",
                "entity_address": "ea1.2023-08.localhost:fm1.1",
                "event_resolution": "PT10M",
                "generic_asset_id": 4,
                "timezone": "UTC",
                "id": 2
            }

        :reqheader Authorization: The authentication token
        :reqheader Content-Type: application/json
        :resheader Content-Type: application/json
        :status 200: UPDATED
        :status 400: INVALID_REQUEST, REQUIRED_INFO_MISSING, UNEXPECTED_PARAMS
        :status 401: UNAUTHORIZED
        :status 403: INVALID_SENDER
        :status 422: UNPROCESSABLE_ENTITY
        """
        audit_log_data = list()
        for k, v in sensor_data.items():
            if getattr(sensor, k) != v:
                audit_log_data.append(
                    f"Field name: {k}, Old value: {getattr(sensor, k)}, New value: {v}"
                )
        audit_log_event = f"Updated sensor '{sensor.name}': {sensor.id}. Updated fields: {'; '.join(audit_log_data)}"

        AssetAuditLog.add_record(sensor.generic_asset, audit_log_event)

        for k, v in sensor_data.items():
            setattr(sensor, k, v)
        db.session.add(sensor)
        db.session.commit()
        return sensor_schema.dump(sensor), 200

    @route("/<id>", methods=["DELETE"])
    @use_kwargs({"sensor": SensorIdField(data_key="id")}, location="path")
    @permission_required_for_context("delete", ctx_arg_name="sensor")
    @as_json
    def delete(self, id: int, sensor: Sensor):
        """Delete a sensor given its identifier.

        .. :quickref: Sensor; Delete a sensor

        This endpoint deletes an existing sensor, as well as all measurements recorded for it.

        :reqheader Authorization: The authentication token
        :reqheader Content-Type: application/json
        :resheader Content-Type: application/json
        :status 204: DELETED
        :status 400: INVALID_REQUEST, REQUIRED_INFO_MISSING, UNEXPECTED_PARAMS
        :status 401: UNAUTHORIZED
        :status 403: INVALID_SENDER
        :status 422: UNPROCESSABLE_ENTITY
        """

        """Delete time series data."""
        db.session.execute(delete(TimedBelief).filter_by(sensor_id=sensor.id))

        AssetAuditLog.add_record(
            sensor.generic_asset, f"Deleted sensor '{sensor.name}': {sensor.id}"
        )

        sensor_name = sensor.name
        db.session.execute(delete(Sensor).filter_by(id=sensor.id))
        db.session.commit()
        current_app.logger.info("Deleted sensor '%s'." % sensor_name)
        return {}, 204

    @route("/<id>/data", methods=["DELETE"])
    @use_kwargs({"sensor": SensorIdField(data_key="id")}, location="path")
    @permission_required_for_context("delete", ctx_arg_name="sensor")
    @as_json
    def delete_data(self, id: int, sensor: Sensor):
        """Delete all data for a sensor.

        .. :quickref: Sensor; Delete sensor data

        This endpoint deletes all data for a sensor.

        :reqheader Authorization: The authentication token
        :reqheader Content-Type: application/json
        :resheader Content-Type: application/json
        :status 204: DELETED
        :status 400: INVALID_REQUEST, REQUIRED_INFO_MISSING, UNEXPECTED_PARAMS
        :status 401: UNAUTHORIZED
        :status 403: INVALID_SENDER
        :status 422: UNPROCESSABLE_ENTITY
        """
        db.session.execute(delete(TimedBelief).filter_by(sensor_id=sensor.id))
        db.session.commit()

        AssetAuditLog.add_record(
            sensor.generic_asset,
            f"Deleted data for sensor '{sensor.name}': {sensor.id}",
        )

        return {}, 204

    @route("/<id>/stats", methods=["GET"])
    @use_kwargs({"sensor": SensorIdField(data_key="id")}, location="path")
    @use_kwargs(
        {
            "sort_keys": fields.Boolean(data_key="sort", load_default=True),
            "event_start_time": fields.Str(load_default=None),
            "event_end_time": fields.Str(load_default=None),
        },
        location="query",
    )
    @permission_required_for_context("read", ctx_arg_name="sensor")
    @as_json
    def get_stats(
        self,
        id,
        sensor: Sensor,
        event_start_time: str,
        event_end_time: str,
        sort_keys: bool,
    ):
        """Fetch stats for a given sensor.

        .. :quickref: Sensor; Get sensor stats

        This endpoint fetches sensor stats for all the historical data.

        Example response

        .. sourcecode:: json

            {
                "some data source": {
                    "First event start": "2015-06-02T10:00:00+00:00",
                    "Last event end": "2015-10-02T10:00:00+00:00",
                    "Last recorded": "2015-10-02T10:01:12+00:00",
                    "Min value": 0.0,
                    "Max value": 100.0,
                    "Mean value": 50.0,
                    "Sum over values": 500.0,
                    "Number of values": 10
                }
            }

        :reqheader Authorization: The authentication token
        :reqheader Content-Type: application/json
        :resheader Content-Type: application/json
        :status 200: PROCESSED
        :status 400: INVALID_REQUEST, REQUIRED_INFO_MISSING, UNEXPECTED_PARAMS
        :status 401: UNAUTHORIZED
        :status 403: INVALID_SENDER
        :status 422: UNPROCESSABLE_ENTITY
        """

        return (
            get_sensor_stats(sensor, event_start_time, event_end_time, sort_keys),
            200,
        )

    @route("/<id>/status", methods=["GET"])
    @use_kwargs({"sensor": SensorIdField(data_key="id")}, location="path")
    @permission_required_for_context("read", ctx_arg_name="sensor")
    @as_json
    def get_status(self, id, sensor):
        """
        Fetch the current status for a given sensor.

        .. :quickref: Sensor; Get sensor status

        This endpoint fetches the current status data for the specified sensor.
        The status includes information about the sensor's status, staleness and resolution.

        Example response:

        .. sourcecode:: json

            [
                {
                    'staleness': None,
                    'stale': True,
                    'staleness_since': None,
                    'reason': 'no data recorded',
                    'source_type': None,
                    'id': 64906,
                    'name': 'power',
                    'resolution': '15 minutes',
                    'asset_name': 'Location 1',
                    'relation': 'sensor belongs to this asset'
                }
            ]

        :reqheader Authorization: The authentication token
        :reqheader Content-Type: application/json
        :resheader Content-Type: application/json
        :status 200: PROCESSED
        :status 400: INVALID_REQUEST, REQUIRED_INFO_MISSING, UNEXPECTED_PARAMS
        :status 401: UNAUTHORIZED
        :status 403: INVALID_SENDER
        :status 404: ASSET_NOT_FOUND
        :status 422: UNPROCESSABLE_ENTITY
        """

        status_data = serialize_sensor_status_data(sensor=sensor)

        return {"sensors_data": status_data}, 200
