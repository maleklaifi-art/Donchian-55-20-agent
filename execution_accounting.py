"""Value actual exchange commissions without charging base-asset fees twice."""
import math


def position_cost(position):
    """Remaining committed cash; legacy positions retain their recorded basis."""
    cost = position.get('entry_cost_usdt')
    if cost is None:
        cost = float(position.get('entry_price', 0)) * float(position.get('quantity', 0))
    return float(cost) - float(position.get('exit_proceeds_usdt', 0))


def commissions(order, client, market, rule, price):
    result = {'base_quantity': 0.0, 'extra_cost_usdt': 0.0, 'total_usdt': 0.0,
              'complete': True, 'details': [], 'error': None}
    try:
        fills = order.get('fills')
        if not isinstance(fills, list) or not fills:
            fills = client.order_trades(rule.symbol, int(order['orderId']))
        if not isinstance(fills, list) or not fills:
            raise ValueError('Exchange commission details unavailable')
        covered = 0.0
        for fill in fills:
            amount = float(fill['commission'])
            qty = float(fill['qty'])
            asset = str(fill['commissionAsset'])
            if not math.isfinite(amount) or amount < 0 or not math.isfinite(qty) or qty <= 0:
                raise ValueError('Invalid exchange commission or fill quantity')
            covered += qty
            # Retain the raw exchange record even if third-asset conversion fails.
            result['details'].append({'asset': asset, 'amount': amount})
            if asset == rule.base_asset:
                result['base_quantity'] += amount
                value = amount * price
            elif asset == rule.quote_asset:
                value = amount
            elif amount == 0:
                value = 0.0
            else:
                rate = float(market.best_ask(asset + rule.quote_asset))
                if not math.isfinite(rate) or rate <= 0:
                    raise ValueError('Commission conversion price unavailable')
                value = amount * rate
            result['total_usdt'] += value
            if asset != rule.base_asset:
                result['extra_cost_usdt'] += value
        if not math.isclose(covered, float(order['executedQty']), rel_tol=1e-8, abs_tol=1e-12):
            raise ValueError('Commission fills do not cover the executed quantity')
    except Exception as exc:
        result['complete'] = False
        result['error'] = str(exc)
    return result
