"""
Tests for the listing universe (NSE main board + NSE SME + BSE-only companies).
"""

import os
import sys
from unittest.mock import MagicMock

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Point at the test database before any app module reads settings
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_db.db")

from app.services import listing_service
from app.services.listing_service import ListingService


def _main(symbol, isin, year=2025):
    return {"symbol": symbol, "company_name": f"{symbol} Ltd", "listing_date": pd.Timestamp(f"{year}-05-10"),
            "isin": isin, "segment": "NSE"}


def _sme(symbol, isin, year=2025):
    return {**_main(symbol, isin, year), "segment": "NSE SME"}


def _bse_row(code, scrip_id, isin, group="M"):
    return {"SCRIP_CD": str(code), "scrip_id": scrip_id, "Scrip_Name": f"{scrip_id} Ltd",
            "ISIN_NUMBER": isin, "GROUP": group}


@pytest.fixture
def service(tmp_path, monkeypatch):
    """ListingService with canned NSE/BSE data, no network and an empty snapshot."""
    nse = MagicMock()
    nse.get_stocks_by_ipo_year.return_value = [_main("MAINCO", "INE000A01011"), _main("DUPCO", "INE000A01029")]
    nse.get_sme_stocks_by_ipo_year.return_value = [
        _sme("SMEHAS", "INE111A01011"), _sme("SMENONE", "INE111A01029"), _main("DUPCO", "INE000A01029"),
    ]
    nse.fetch_equity_list.return_value = pd.DataFrame({"ISIN NUMBER": ["INE000A01011", "INE000A01029"]})
    nse.fetch_sme_list.return_value = pd.DataFrame({"ISIN NUMBER": ["INE111A01011", "INE111A01029"]})

    svc = ListingService(nse_service=nse)
    monkeypatch.chdir(tmp_path)
    svc.cache.cache_dir = str(tmp_path)
    svc.dating_cache.cache_dir = str(tmp_path)

    bse = pd.DataFrame([
        _bse_row(500001, "MAINCO", "INE000A01011", "A"),       # dual listed: NSE main board
        _bse_row(544500, "BSEIPO25", "INE222A01011"),          # BSE-only, first traded 2025-03
        _bse_row(544600, "BSEIPO26", "INE222A01029"),          # BSE-only, first traded 2026-02
        _bse_row(544700, "NODATA", "INE222A01037"),            # BSE-only, no Yahoo data
        _bse_row(544800, "AN ETF", "INF222A01011", "B"),       # ETF units are not companies
        _bse_row(540100, "SMEHAS", "INE111A01011"),            # also on NSE SME
        _bse_row(530000, "OLDCO", "INE333A01011", "A"),        # below the recent-listings code floor
    ])
    monkeypatch.setattr(svc, "_fetch_bse_list", lambda: bse)

    dated = {"BSEIPO25.BO": "2025-03", "BSEIPO26.BO": "2026-02", "NODATA.BO": None,
             "SMEHAS.NS": "2025-06", "SMENONE.NS": None}
    monkeypatch.setattr(svc, "_first_trade_months", lambda tickers: {t: dated.get(t) for t in tickers})
    return svc


class TestListingUniverse:
    def test_covers_nse_main_nse_sme_and_bse_only(self, service):
        stocks = {s["symbol"]: s for s in service.get_stocks_by_ipo_year(2025)}

        assert set(stocks) == {"MAINCO", "DUPCO", "SMEHAS", "SMENONE", "BSEIPO25"}
        assert stocks["MAINCO"]["segment"] == "NSE"
        assert stocks["SMEHAS"]["segment"] == "NSE SME"
        assert stocks["BSEIPO25"]["segment"] == "BSE"

    def test_company_in_both_nse_lists_is_counted_once(self, service):
        symbols = [s["symbol"] for s in service.get_stocks_by_ipo_year(2025)]

        assert symbols.count("DUPCO") == 1

    def test_bse_only_companies_are_dated_by_first_trade_month(self, service):
        by_symbol = {s["symbol"]: s for s in service.get_stocks_by_ipo_year(0)}

        assert pd.Timestamp(by_symbol["BSEIPO25"]["listing_date"]) == pd.Timestamp("2025-03-01")
        assert by_symbol["BSEIPO26"]["yf_symbol"] == "BSEIPO26.BO"
        assert "BSEIPO26" not in {s["symbol"] for s in service.get_stocks_by_ipo_year(2025)}

    def test_excludes_etfs_undated_and_pre_2016_scrips(self, service):
        symbols = {s["symbol"] for s in service.get_stocks_by_ipo_year(0)}

        assert not symbols & {"AN ETF", "NODATA", "OLDCO"}

    def test_nse_company_gets_its_bse_id_from_isin(self, service):
        stocks = {s["symbol"]: s for s in service.get_stocks_by_ipo_year(2025)}

        assert stocks["MAINCO"]["bse_symbol"] == "MAINCO"
        assert stocks["DUPCO"]["bse_symbol"] is None  # not on BSE: no pointless history request
        assert stocks["BSEIPO25"]["bse_symbol"] is None

    def test_sme_stocks_without_yahoo_data_are_flagged_unavailable(self, service):
        stocks = {s["symbol"]: s for s in service.get_stocks_by_ipo_year(2025)}

        assert stocks["SMENONE"]["unavailable"] is True
        assert stocks["SMEHAS"]["unavailable"] is False
        assert stocks["MAINCO"]["unavailable"] is False

    def test_inconclusive_check_leaves_stock_available(self, service, monkeypatch):
        monkeypatch.setattr(service, "_first_trade_months", lambda tickers: {})

        stocks = {s["symbol"]: s for s in service.get_stocks_by_ipo_year(2025)}

        assert stocks["SMENONE"]["unavailable"] is False


class TestFirstTradeMonths:
    def _download(self, frames):
        def fake(tickers, **kwargs):
            cols = {}
            for t in tickers:
                frame = frames.get(t)
                if frame is not None:
                    for field in ("High", "Close"):
                        cols[(t, field)] = frame
            return pd.DataFrame(cols) if cols else pd.DataFrame()
        return fake

    def test_reports_first_month_and_missing_tickers(self, monkeypatch):
        idx = pd.date_range("2025-03-01", periods=3, freq="MS")
        series = pd.Series([10.0, 11.0, 12.0], index=idx)
        monkeypatch.setattr(listing_service.yf, "download", self._download({
            "AAA.BO": series, listing_service.CONTROL_TICKER: series,
        }))
        service = ListingService(nse_service=MagicMock())

        months = service._first_trade_months(["AAA.BO", "BBB.BO"])

        assert months == {"AAA.BO": "2025-03", "BBB.BO": None}

    def test_throttled_chunk_is_inconclusive_not_missing(self, monkeypatch):
        monkeypatch.setattr(listing_service.yf, "download", self._download({}))
        service = ListingService(nse_service=MagicMock())

        assert service._first_trade_months(["AAA.BO", "BBB.BO"]) == {}
