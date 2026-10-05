"""GA4 Data API + Google Ads REST fetchers (read-only). Same OAuth refresh-token approach as the MCP servers."""

import datetime as dt
import os

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCOPES = ["https://www.googleapis.com/auth/analytics.readonly", "https://www.googleapis.com/auth/adwords"]

GA4_DIMS = ["date", "sessionSource", "sessionMedium", "deviceCategory"]
GA4_METRICS = ["sessions", "totalUsers", "newUsers", "engagedSessions", "keyEvents", "totalRevenue"]

ADS_QUERY = """
SELECT segments.date, campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type,
       metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions, metrics.conversions_value
FROM campaign
WHERE segments.date BETWEEN '{start}' AND '{end}'
"""


def get_credentials() -> Credentials:
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return creds


def fetch_ga4_daily(property_id: str, start: dt.date, end: dt.date, creds: Credentials) -> list[dict]:
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.analytics.data_v1beta.types import DateRange, Dimension, Metric, RunReportRequest

    api = BetaAnalyticsDataClient(credentials=creds)
    rows, offset = [], 0
    while True:
        resp = api.run_report(RunReportRequest(
            property=f"properties/{property_id}",
            date_ranges=[DateRange(start_date=start.isoformat(), end_date=end.isoformat())],
            dimensions=[Dimension(name=d) for d in GA4_DIMS],
            metrics=[Metric(name=m) for m in GA4_METRICS],
            limit=100_000,
            offset=offset,
        ))
        for r in resp.rows:
            d = [v.value for v in r.dimension_values]
            m = [v.value for v in r.metric_values]
            rows.append({
                "date": dt.datetime.strptime(d[0], "%Y%m%d").date(),
                "session_source": d[1][:255], "session_medium": d[2][:255], "device_category": d[3][:32],
                "sessions": int(m[0]), "total_users": int(m[1]), "new_users": int(m[2]),
                "engaged_sessions": int(m[3]), "key_events": float(m[4]), "total_revenue": float(m[5]),
            })
        offset += len(resp.rows)
        if not resp.rows or offset >= resp.row_count:
            return rows


def _ads_search(customer_id: str, manager_id: str, query: str, creds: Credentials) -> list[dict]:
    version = os.environ["GOOGLE_ADS_API_VERSION"]  # e.g. the version your my-google-ads-mcp server.py uses
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "developer-token": os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
        "Content-Type": "application/json",
    }
    login = manager_id or os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "")
    if login:
        headers["login-customer-id"] = login.replace("-", "")
    url = f"https://googleads.googleapis.com/{version}/customers/{customer_id.replace('-', '')}/googleAds:searchStream"
    resp = requests.post(url, headers=headers, json={"query": query}, timeout=120)
    if not resp.ok:
        raise RuntimeError(f"Google Ads API {resp.status_code}: {resp.text[:500]}")
    return [r for batch in resp.json() for r in batch.get("results", [])]


def fetch_ads_campaign_daily(customer_id: str, manager_id: str, start: dt.date, end: dt.date,
                             creds: Credentials) -> tuple[list[dict], str]:
    results = _ads_search(customer_id, manager_id, ADS_QUERY.format(start=start, end=end), creds)
    rows = []
    for r in results:
        c, m = r["campaign"], r.get("metrics", {})
        rows.append({
            "date": dt.date.fromisoformat(r["segments"]["date"]),
            "campaign_id": int(c["id"]), "campaign_name": c.get("name", "")[:255],
            "campaign_status": c.get("status", ""), "channel_type": c.get("advertisingChannelType", ""),
            "impressions": int(m.get("impressions", 0)), "clicks": int(m.get("clicks", 0)),
            "cost": int(m.get("costMicros", 0)) / 1_000_000,
            "conversions": float(m.get("conversions", 0)), "conversions_value": float(m.get("conversionsValue", 0)),
        })
    currency = _ads_search(customer_id, manager_id, "SELECT customer.currency_code FROM customer", creds)
    return rows, (currency[0]["customer"].get("currencyCode", "") if currency else "")
