"""Quick test to debug sell allowance issues."""
import sys
sys.path.insert(0, '.')

from polymarket_client import get_clob_client, fetch_active_btc_markets
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

client = get_clob_client()

# Get current active markets
markets = fetch_active_btc_markets()
if not markets:
    print("No active markets")
    sys.exit(1)

m = markets[0]
print(f"Market: {m.question}")
print(f"UP token: {m.token_id_up[:30]}...")
print(f"DN token: {m.token_id_down[:30]}...")

# Check neg_risk
try:
    neg = client.get_neg_risk(m.token_id_up)
    print(f"\nneg_risk for UP: {neg}")
except Exception as e:
    print(f"neg_risk error: {e}")

# Check balance/allowance for CONDITIONAL 
for label, tid in [("UP", m.token_id_up), ("DOWN", m.token_id_down)]:
    try:
        params = BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL,
            token_id=tid,
        )
        result = client.get_balance_allowance(params)
        print(f"\n{label} CONDITIONAL balance: {result}")
    except Exception as e:
        print(f"{label} balance error: {e}")

# Try update_balance_allowance
for label, tid in [("UP", m.token_id_up), ("DOWN", m.token_id_down)]:
    try:
        params = BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL,
            token_id=tid,
        )
        result = client.update_balance_allowance(params)
        print(f"\n{label} update_balance result: {result}")
    except Exception as e:
        print(f"{label} update_balance error: {e}")

# Also check COLLATERAL balance
try:
    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    result = client.get_balance_allowance(params)
    print(f"\nCOLLATERAL balance: {result}")
except Exception as e:
    print(f"COLLATERAL balance error: {e}")
