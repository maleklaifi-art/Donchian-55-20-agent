#!/usr/bin/env python3
"""Connectivity/account check. This file never places an order."""
import os
from demo_trader import PublicMarketClient, TestnetExecutionClient, load_rules


def main():
    market = PublicMarketClient(15)
    t = market.get('/api/v3/time')
    print('Production public market API: OK', t.get('serverTime'))

    key = os.environ.get('BINANCE_TESTNET_API_KEY', '')
    secret = os.environ.get('BINANCE_TESTNET_API_SECRET', '')
    if not key or not secret:
        print('Testnet credentials: NOT SET')
        print('Set BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET, then run this again.')
        return

    test = TestnetExecutionClient(key, secret, 15, 5000)
    account = test.account()
    rules = load_rules(test.exchange_info())
    usdt = next((x for x in account.get('balances', []) if x.get('asset') == 'USDT'), None)
    print('Spot Testnet authenticated API: OK')
    print('canTrade:', account.get('canTrade'))
    print('USDT balance:', usdt)
    print('Testnet trading symbols:', len(rules))
    print('NO ORDERS WERE PLACED.')


if __name__ == '__main__':
    main()
