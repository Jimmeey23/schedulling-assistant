"""Google Sheets reader using OAuth2 client credentials (no service account)."""
import os
import pandas as pd
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SPREADSHEET_ID = "16wFlke0bHFcmfn-3UyuYlGnImBq0DY7ouVYAlAFTZys"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def _build_service(client_id: str, client_secret: str, refresh_token: str):
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def read_sheet(sheet_name: str, client_id: str, client_secret: str, refresh_token: str) -> pd.DataFrame:
    service = _build_service(client_id, client_secret, refresh_token)
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SPREADSHEET_ID, range=sheet_name)
        .execute()
    )
    values = result.get("values", [])
    if not values:
        return pd.DataFrame()
    headers = values[0]
    rows = values[1:]
    # Pad short rows so all have same length as headers
    rows = [r + [""] * (len(headers) - len(r)) for r in rows]
    return pd.DataFrame(rows, columns=headers)
