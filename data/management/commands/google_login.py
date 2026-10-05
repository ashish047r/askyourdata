from django.core.management.base import BaseCommand

from data.google_api import SCOPES


class Command(BaseCommand):
    help = "One-time: browser OAuth for GA4 + Ads read access; prints the refresh token for .env."

    def add_arguments(self, parser):
        parser.add_argument("client_secrets", help="Desktop-app OAuth client JSON (same kind your MCP servers use)")

    def handle(self, *args, client_secrets, **opts):
        from google_auth_oauthlib.flow import InstalledAppFlow

        creds = InstalledAppFlow.from_client_secrets_file(client_secrets, SCOPES).run_local_server(port=0)
        self.stdout.write(f"GOOGLE_CLIENT_ID={creds.client_id}")
        self.stdout.write(f"GOOGLE_CLIENT_SECRET={creds.client_secret}")
        self.stdout.write(f"GOOGLE_REFRESH_TOKEN={creds.refresh_token}")
        self.stdout.write("Paste these into .env and Render/GitHub secrets. Never commit them.")
