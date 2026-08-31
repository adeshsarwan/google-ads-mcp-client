# Google Ads Function Gateway

Standalone, deterministic Google Ads function gateway for the Google Ads Function Catalogue v1 foundation and read-only functions 01-06.

The gateway is intentionally not an MCP conversational client. Future MCP clients should call the approved function names with validated parameters; they must not supply raw GAQL or construct Google Ads API logic at runtime.

## Development

Run the local test suite without live Google Ads credentials:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

Run syntax checks:

```bash
python3 -m compileall src tests
```

## Production Configuration

The official Google Ads client wrapper reads configuration from environment variables:

- `GOOGLE_ADS_DEVELOPER_TOKEN`
- `GOOGLE_ADS_CLIENT_ID`
- `GOOGLE_ADS_CLIENT_SECRET`
- `GOOGLE_ADS_REFRESH_TOKEN`
- `GOOGLE_ADS_LOGIN_CUSTOMER_ID`
- `GOOGLE_ADS_API_VERSION` optional; when omitted, the installed Google Ads library default is used
- `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS` comma-separated allow-list; default is deny-all
- `GOOGLE_ADS_PAGE_SIZE` optional
- `GOOGLE_ADS_RETRY_ATTEMPTS` optional

Install the official Google Ads dependency for live use:

```bash
python3 -m pip install -e ".[google-ads]"
```
