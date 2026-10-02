"""Input validation for the API: anything that fails here never reaches the services."""

from django.core.validators import RegexValidator
from rest_framework import serializers

# Letters (any language), digits, spaces and the punctuation found in addresses.
# Anything else (control characters, <, >, quotes, etc.) is rejected up front.
location_chars = RegexValidator(
    r"^[\w\s,.'#&()/\-]+$",
    "Only letters, digits, spaces and , . ' # & ( ) / - are allowed.",
)


def location_field():
    """A short, plain-text location string (length- and character-limited)."""
    return serializers.CharField(
        min_length=2,
        max_length=200,
        trim_whitespace=True,
        validators=[location_chars],
        help_text='A US place: "City, ST", "City, State", "lat,lon" or a street address.',
    )


class RoutePlanRequestSerializer(serializers.Serializer):
    """
    Input for /api/v1/route/:
        from      start location (required)
        to        end location (required)
        geometry  "polyline" (default) or "points"
    """

    # "from" is a Python keyword, so the field is declared as "from_" and renamed below.
    from_ = location_field()
    to = location_field()
    # "polyline" is the default: a coast-to-coast route is ~0.13 MB encoded against ~0.81 MB
    # as a point list, and the point list is the only thing that forces the server to decode
    # the geometry at all. Ask for "points" when you want to hand it straight to a map.
    geometry = serializers.ChoiceField(choices=["points", "polyline"], default="polyline")

    def get_fields(self):
        """Expose the "from_" field to clients under its real name, "from"."""
        fields = super().get_fields()
        fields["from"] = fields.pop("from_")
        return fields

    def validate(self, attrs):
        """Cross-field check: a trip needs two different places."""
        if attrs["from"].strip().lower() == attrs["to"].strip().lower():
            raise serializers.ValidationError({"to": "Start and finish must be different."})
        return attrs
