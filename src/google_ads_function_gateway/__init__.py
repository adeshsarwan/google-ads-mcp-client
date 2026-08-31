"""Google Ads Function Gateway public package."""

import logging

from google_ads_function_gateway.catalogue import GoogleAdsFunctionCatalogue

logging.getLogger("google_ads_function_gateway").addHandler(logging.NullHandler())

__all__ = ["GoogleAdsFunctionCatalogue"]
