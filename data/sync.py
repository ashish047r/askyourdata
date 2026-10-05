from .models import AdsCampaignDaily, Client, Ga4Daily

GA4_KEY = ["date", "session_source", "session_medium", "device_category"]
GA4_VALUES = ["sessions", "total_users", "new_users", "engaged_sessions", "key_events", "total_revenue"]
ADS_KEY = ["date", "campaign_id"]
ADS_VALUES = ["campaign_name", "campaign_status", "channel_type", "impressions", "clicks", "cost",
              "conversions", "conversions_value"]


def upsert_rows(model, client: Client, rows: list[dict], key: list[str], values: list[str]) -> int:
    """INSERT ... ON CONFLICT DO UPDATE: re-syncing the same days is idempotent and picks up late conversions."""
    model.objects.bulk_create(
        [model(client=client, **r) for r in rows],
        batch_size=1000,
        update_conflicts=True,
        unique_fields=["client", *key],
        update_fields=values,
    )
    return len(rows)


def upsert_ga4(client, rows):
    return upsert_rows(Ga4Daily, client, rows, GA4_KEY, GA4_VALUES)


def upsert_ads(client, rows):
    return upsert_rows(AdsCampaignDaily, client, rows, ADS_KEY, ADS_VALUES)
