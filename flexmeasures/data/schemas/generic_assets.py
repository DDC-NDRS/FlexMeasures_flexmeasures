from __future__ import annotations

import json

from marshmallow import validates, ValidationError, fields, validates_schema
from flask_security import current_user
from sqlalchemy import select

from flexmeasures.data import ma, db
from flexmeasures.data.models.user import Account
from flexmeasures.data.models.generic_assets import GenericAsset, GenericAssetType
from flexmeasures.data.schemas.locations import LatitudeField, LongitudeField
from flexmeasures.data.schemas.utils import (
    FMValidationError,
    MarshmallowClickMixin,
    with_appcontext_if_needed,
)
from flexmeasures.auth.policy import user_has_admin_access
from flexmeasures.cli import is_running as running_as_cli
from flexmeasures.utils.coding_utils import flatten_unique


class JSON(fields.Field):
    def _deserialize(self, value, attr, data, **kwargs) -> dict:
        try:
            return json.loads(value)
        except ValueError:
            raise ValidationError("Not a valid JSON string.")

    def _serialize(self, value, attr, data, **kwargs) -> str:
        return json.dumps(value)


class GenericAssetSchema(ma.SQLAlchemySchema):
    """
    GenericAsset schema, with validations.
    """

    id = ma.auto_field(dump_only=True)
    name = fields.Str(required=True)
    account_id = ma.auto_field()
    latitude = LatitudeField(allow_none=True)
    longitude = LongitudeField(allow_none=True)
    generic_asset_type_id = fields.Integer(required=True)
    attributes = JSON(required=False)
    parent_asset_id = fields.Int(required=False, allow_none=True)
    child_assets = ma.Nested("GenericAssetSchema", many=True, dumb_only=True)
    production_price_sensor_id = fields.Int(required=False, allow_none=True)
    consumption_price_sensor_id = fields.Int(required=False, allow_none=True)
    inflexible_device_sensor_ids = fields.List(
        fields.Int, required=False, allow_none=True
    )

    class Meta:
        model = GenericAsset

    @validates_schema(skip_on_field_errors=False)
    def validate_name_is_unique_under_parent(self, data, **kwargs):
        if "name" in data:

            asset = db.session.scalars(
                select(GenericAsset)
                .filter_by(
                    name=data["name"],
                    parent_asset_id=data.get("parent_asset_id"),
                    account_id=data.get("account_id"),
                )
                .limit(1)
            ).first()

            if asset:
                raise ValidationError(
                    f"An asset with the name '{data['name']}' already exists under parent asset with id={data.get('parent_asset_id')}.",
                    "name",
                )

    @validates("generic_asset_type_id")
    def validate_generic_asset_type(self, generic_asset_type_id: int):
        generic_asset_type = db.session.get(GenericAssetType, generic_asset_type_id)
        if not generic_asset_type:
            raise ValidationError(
                f"GenericAssetType with id {generic_asset_type_id} doesn't exist."
            )

    @validates("parent_asset_id")
    def validate_parent_asset(self, parent_asset_id: int | None):
        if parent_asset_id is not None:
            parent_asset = db.session.get(GenericAsset, parent_asset_id)
            if not parent_asset:
                raise ValidationError(
                    f"Parent GenericAsset with id {parent_asset_id} doesn't exist."
                )

    @validates("account_id")
    def validate_account(self, account_id: int | None):
        if account_id is None and (
            running_as_cli() or user_has_admin_access(current_user, "update")
        ):
            return
        account = db.session.get(Account, account_id)
        if not account:
            raise ValidationError(f"Account with Id {account_id} doesn't exist.")
        if not running_as_cli() and (
            not user_has_admin_access(current_user, "update")
            and account_id != current_user.account_id
        ):
            raise ValidationError(
                "User is not allowed to create assets for this account."
            )

    @validates("attributes")
    def validate_attributes(self, attributes: dict):
        sensors_to_show = attributes.get("sensors_to_show", [])

        # Check type
        if not isinstance(sensors_to_show, list):
            raise ValidationError("sensors_to_show should be a list.")
        for sensor_listing in sensors_to_show:
            if not isinstance(sensor_listing, (int, list)):
                raise ValidationError(
                    "sensors_to_show should only contain sensor IDs (integers) or lists thereof."
                )
            if isinstance(sensor_listing, list):
                for sensor_id in sensor_listing:
                    if not isinstance(sensor_id, int):
                        raise ValidationError(
                            "sensors_to_show should only contain sensor IDs (integers) or lists thereof."
                        )

        # Check whether IDs represent accessible sensors
        from flexmeasures.data.schemas import SensorIdField

        sensor_ids = flatten_unique(sensors_to_show)
        for sensor_id in sensor_ids:
            SensorIdField().deserialize(sensor_id)


class GenericAssetTypeSchema(ma.SQLAlchemySchema):
    """
    GenericAssetType schema, with validations.
    """

    id = ma.auto_field()
    name = fields.Str()
    description = ma.auto_field()

    class Meta:
        model = GenericAssetType


class GenericAssetIdField(MarshmallowClickMixin, fields.Int):
    """Field that deserializes to a GenericAsset and serializes back to an integer."""

    @with_appcontext_if_needed()
    def _deserialize(self, value, attr, obj, **kwargs) -> GenericAsset:
        """Turn a generic asset id into a GenericAsset."""
        generic_asset = db.session.get(GenericAsset, value)
        if generic_asset is None:
            raise FMValidationError(f"No asset found with id {value}.")
        # lazy loading now (asset is somehow not in session after this)
        generic_asset.generic_asset_type
        return generic_asset

    def _serialize(self, asset, attr, data, **kwargs):
        """Turn a GenericAsset into a generic asset id."""
        return asset.id
