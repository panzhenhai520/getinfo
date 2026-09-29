"""Embedded financial data provider adapters."""

from .akshare_cn import AKShareCNProvider
from .tushare_cn import TushareCNProvider
from .yahoo import YahooFinanceProvider
from .easyquotation import EasyQuotationProvider
from .alpha_vantage import AlphaVantageProvider
from .fred import FREDProvider
from .polymarket import PolymarketProvider
from .official_evidence import OfficialEvidenceProvider

__all__ = [
    "AKShareCNProvider", "TushareCNProvider", "YahooFinanceProvider",
    "EasyQuotationProvider",
    "AlphaVantageProvider",
    "FREDProvider",
    "PolymarketProvider",
    "OfficialEvidenceProvider",
]
