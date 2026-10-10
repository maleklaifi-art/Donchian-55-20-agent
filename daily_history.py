"""Persistent, incrementally refreshed UTC daily candles for continuous Wilder ATR."""
import json
import math
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from strategy import Candle, DAY_MS


def validate(bars):
    for i, bar in enumerate(bars):
        values = (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.quote_volume)
        if not all(math.isfinite(v) for v in values):
            raise ValueError("Non-finite daily history")
        if (min(bar.open, bar.high, bar.low, bar.close) <= 0 or
                min(bar.volume, bar.quote_volume) < 0 or
                bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close) or
                bar.open_time % DAY_MS or bar.close_time != bar.open_time + DAY_MS - 1):
            raise ValueError("Invalid daily history candle")
        if i and bar.open_time != bars[i - 1].open_time + DAY_MS:
            raise ValueError("Daily history has a gap or duplicate")


class DailyHistory:
    def __init__(self, directory):
        self.directory = Path(directory)

    def fetch(self, get, symbol, now_ms, page_size=1000):
        # Cache files are segregated by symbol. The scanner requests each once.
        if not symbol.isalnum():
            raise ValueError("Invalid history symbol")
        target = (now_ms // DAY_MS) * DAY_MS - 1
        path = self.directory / (symbol + '.json')
        bars = []
        if path.exists():
            try:
                cache = json.loads(path.read_text())
                if cache['version'] != 1 or cache['symbol'] != symbol:
                    raise ValueError('Invalid history cache metadata')
                bars = [Candle(**row) for row in cache['candles']]
                validate(bars)
            except (ValueError, KeyError, TypeError):
                bars = []  # Rebuild damaged caches from listing; never seed from a tail.
        bars = [bar for bar in bars if bar.close_time <= target]
        cursor = bars[-1].open_time + DAY_MS if bars else 0
        page_size = max(60, min(int(page_size), 1000))
        while cursor <= target:
            rows = get('/api/v3/klines', {'symbol': symbol, 'interval': '1d',
                       'startTime': cursor, 'endTime': target, 'limit': page_size})
            if not rows:
                break
            page = [Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]),
                           float(r[4]), float(r[5]), int(r[6]), float(r[7])) for r in rows]
            if page[0].open_time < cursor or page[-1].close_time > target:
                raise ValueError('Exchange returned candles outside the closed history request')
            validate(page)
            if bars and page[0].open_time != cursor:
                raise ValueError('Exchange returned a gap in daily history')
            bars.extend(page)
            cursor = bars[-1].open_time + DAY_MS
        validate(bars)
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self.directory, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump({'version': 1, 'symbol': symbol, 'candles': [asdict(b) for b in bars]}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return bars
