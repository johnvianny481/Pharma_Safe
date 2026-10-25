"""Data-scope regions for cases.

A case's region comes from the country where the reaction occurred. Users can be limited
to regions (``ps_regions`` claim), so the mapping must be deterministic and fail closed:
a case whose country is not yet known is in region ``unknown``, which only users with
unrestricted region scope can see.
"""

# EU member states plus Iceland, Liechtenstein and Norway (the EEA).
EEA_COUNTRIES = frozenset(
    {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT",
     "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO"}
)  # fmt: skip

UNKNOWN_REGION = "unknown"
OTHER_REGION = "other"


def region_for_country(country: str | None) -> str:
    if country is None:
        return UNKNOWN_REGION
    if country in EEA_COUNTRIES:
        return "eu"
    if country == "US":
        return "us"
    if country == "IN":
        return "in"
    return OTHER_REGION
