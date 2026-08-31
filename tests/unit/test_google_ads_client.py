from __future__ import annotations

import unittest

from google_ads_function_gateway.client.google_ads import OfficialGoogleAdsClientWrapper
from google_ads_function_gateway.config import GoogleAdsSettings


class OfficialGoogleAdsClientWrapperTests(unittest.TestCase):
    def test_search_request_does_not_send_page_size(self) -> None:
        fake_google_ads_client = _FakeGoogleAdsClient()
        wrapper = OfficialGoogleAdsClientWrapper(GoogleAdsSettings(api_version="v25"))
        wrapper._client = fake_google_ads_client

        page = wrapper.search(
            customer_id="1234567890",
            query="SELECT campaign.id FROM campaign",
            page_token=None,
            request_id="req-1",
        )

        self.assertEqual(page.rows, ["row-1"])
        request = fake_google_ads_client.service.requests[0]
        self.assertNotIn("page_size", request.assigned)
        self.assertEqual(request.assigned["customer_id"], "1234567890")
        self.assertEqual(request.assigned["query"], "SELECT campaign.id FROM campaign")
        self.assertNotIn("page_token", request.assigned)

    def test_search_request_sends_next_page_token_when_present(self) -> None:
        fake_google_ads_client = _FakeGoogleAdsClient()
        wrapper = OfficialGoogleAdsClientWrapper(GoogleAdsSettings(api_version="v25"))
        wrapper._client = fake_google_ads_client

        wrapper.search(
            customer_id="1234567890",
            query="SELECT campaign.id FROM campaign",
            page_token="next-page",
            request_id="req-2",
        )

        request = fake_google_ads_client.service.requests[0]
        self.assertNotIn("page_size", request.assigned)
        self.assertEqual(request.assigned["page_token"], "next-page")


class _FakeGoogleAdsClient:
    def __init__(self) -> None:
        self.service = _FakeGoogleAdsService()

    def get_service(self, service_name: str, version: str | None = None) -> _FakeGoogleAdsService:
        self.service_names = [service_name, version]
        return self.service

    def get_type(self, type_name: str, version: str | None = None) -> _TrackedRequest:
        self.type_names = [type_name, version]
        return _TrackedRequest()


class _FakeGoogleAdsService:
    def __init__(self) -> None:
        self.requests: list[_TrackedRequest] = []

    def search(self, *, request: _TrackedRequest) -> _FakeSearchResponse:
        self.requests.append(request)
        return _FakeSearchResponse()


class _FakeSearchResponse:
    results = ["row-1"]
    next_page_token = None


class _TrackedRequest:
    def __init__(self) -> None:
        super().__setattr__("assigned", {})

    def __setattr__(self, name: str, value: object) -> None:
        self.assigned[name] = value


if __name__ == "__main__":
    unittest.main()
