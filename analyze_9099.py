"""
Polybot Snipez — 90c -> 99c analysis

Reads the CSVs the strategy writes and answers the question the whole thing
rests on:

    when a 5-minute contract reaches the entry threshold with X seconds
    left, how often does it reach 99c — with someone actually there to
    buy — before it reverses?

    python analyze_9099.py                 # every day on disk
    python analyze_9099.py --date 2026-09-17
    python analyze_9099.py --threshold 0.90 --asset BTC

Two things are reported separately on purpose, because they are not the
same number and the gap between them IS the strategy's edge or the lack of
one:

    tp_price_reached        the market got to 99c
    tp_queue_adjusted_fill  enough volume went through 99c to reach an
                            order sitting behind the queue that was there

Every observed threshold (85 / 88 / 90 / 92 / 95) is reported on its own
terms, so the data picks the entry level rather than us assuming 90c.
"""

import argparse
import csv
import glob
import os
import statistics
from collections import Counter, defaultdict

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# The time-remaining buckets we care about, in seconds.
BUCKETS = [5, 10, 15, 20, 30, 45, 60, 90, 120]


def _load(prefix: str, date: str | None) -> list[dict]:
    pattern = f"{prefix}_{date}.csv" if date else f"{prefix}_*.csv"
    rows = []
    for path in sorted(glob.glob(os.path.join(DATA_DIR, pattern))):
        with open(path, newline="", encoding="utf-8") as f:
            rows.extend(csv.DictReader(f))
    return rows


def _f(row: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(row.get(key, "") or default)
    except (TypeError, ValueError):
        return default


def _b(row: dict, key: str) -> bool:
    return str(row.get(key, "")).strip().lower() == "true"


def _bucket(secs: float) -> str:
    for edge in BUCKETS:
        if secs <= edge:
            return f"<={edge}s"
    return f">{BUCKETS[-1]}s"


def _pct(n: int, d: int) -> str:
    return f"{n / d * 100:5.1f}%" if d else "    --"


# ── the headline table ───────────────────────────────────────────────────

def report_by_threshold(outcomes: list[dict]):
    """Optimistic vs queue-adjusted fill rate, per observed entry threshold."""
    levels = sorted({_f(r, "observe_level") for r in outcomes if r.get("observe_level")})
    if not levels:
        return
    print("\n" + "=" * 96)
    print("  Entry threshold comparison — did a take-profit rested HERE actually fill?")
    print("=" * 96)
    print(f"  {'level':>7} {'n':>6}  {'reached 99c':>12} {'price reached':>14}"
          f" {'QUEUE-ADJ fill':>15}  {'median queue ahead':>19}")
    print("  " + "-" * 92)
    for level in levels:
        rows = [r for r in outcomes if _f(r, "observe_level") == level]
        n = len(rows)
        reached99 = sum(1 for r in rows if _b(r, "reached_99"))
        optimistic = sum(1 for r in rows if _b(r, "tp_price_reached"))
        queue = sum(1 for r in rows if _b(r, "tp_queue_adjusted_fill"))
        ahead = [_f(r, "shadow_queue_ahead") for r in rows]
        med_ahead = statistics.median(ahead) if ahead else 0
        print(f"  {level:>7.2f} {n:>6}  {_pct(reached99, n):>12} {_pct(optimistic, n):>14}"
              f" {_pct(queue, n):>15}  {med_ahead:>19,.0f}")
    print("\n  If 'price reached' is high and 'QUEUE-ADJ fill' is low, the move is")
    print("  real and the fill is not — the queue at 99c is the binding constraint,")
    print("  not the probability of getting there.")


def report_reference_quality(rows: list[dict]):
    """How much of the data has the reference the market actually settles on."""
    if not rows:
        return
    official = sum(1 for r in rows if _b(r, "official_reference_available"))
    print("\n" + "=" * 96)
    print("  Settlement reference")
    print("=" * 96)
    print(f"  rows with the OFFICIAL reference (Chainlink TWAP-60s): "
          f"{official}/{len(rows)} ({_pct(official, len(rows)).strip()})")
    if official < len(rows):
        reasons = Counter(r.get("official_reference_reason", "") for r in rows
                          if not _b(r, "official_reference_available"))
        for reason, count in reasons.most_common(3):
            print(f"    {count:>6}  unavailable: {reason or 'unknown'}")
        print("  The rest use a Binance TWAP PROXY, recorded as such. Proxy distance")
        print("  is a filter input only — it never forced an exit.")
    cover = [_f(r, "binance_window_coverage") for r in rows if r.get("binance_window_coverage")]
    if cover:
        full = sum(1 for c in cover if c >= 0.95)
        print(f"  windows observed end to end: {full}/{len(cover)} "
              f"({_pct(full, len(cover)).strip()}) — partial windows have a TWAP that")
        print("  is not comparable to the official one, so filter on coverage first.")


def report_reach_rates(outcomes: list[dict], threshold: float):
    print("\n" + "=" * 96)
    print(f"  P(reach target | crossed {threshold:.2f} with X seconds left)   "
          f"— bid side, n={len(outcomes)}")
    print("=" * 96)
    print(f"  {'bucket':<10} {'n':>5}  {'95c':>7} {'97c':>7} {'98c':>7} {'99c':>7}"
          f"  {'med s->99':>10}  {'queue-adj fill':>15}  {'our TP filled':>14}")
    print("  " + "-" * 92)

    groups = defaultdict(list)
    for row in outcomes:
        groups[_bucket(_f(row, "trigger_secs_remaining"))].append(row)

    order = [f"<={e}s" for e in BUCKETS] + [f">{BUCKETS[-1]}s"]
    for name in order:
        rows = groups.get(name)
        if not rows:
            continue
        n = len(rows)
        hits = {t: sum(1 for r in rows if _b(r, f"reached_{t}")) for t in (95, 97, 98, 99)}
        secs99 = [_f(r, "secs_to_99") for r in rows
                  if _b(r, "reached_99") and r.get("secs_to_99")]
        depth99 = [_f(r, "depth_at_99") for r in rows if _b(r, "reached_99")]
        traded = [r for r in rows if _b(r, "traded")]
        filled = sum(1 for r in traded if _b(r, "our_tp_filled"))

        queue_fills = sum(1 for r in rows if _b(r, "tp_queue_adjusted_fill"))
        med_secs = f"{statistics.median(secs99):9.1f}s" if secs99 else f"{'--':>10}"
        tp_col = (f"{filled:>3}/{len(traded):<3} {_pct(filled, len(traded))}"
                  if traded else f"{'--':>14}")
        print(
            f"  {name:<10} {n:>5}  {_pct(hits[95], n)} {_pct(hits[97], n)} "
            f"{_pct(hits[98], n)} {_pct(hits[99], n)}  {med_secs}  "
            f"{_pct(queue_fills, n):>15}  {tp_col}"
        )
        _ = depth99

    print("\n  'our TP filled' counts trades where OUR resting sell actually filled.")
    print("  A high 99c column with a low TP column means the print was there and")
    print("  the fill was not — queue position, not probability.")


def report_resolution(outcomes: list[dict]):
    resolved = [r for r in outcomes if r.get("resolution") in ("Up", "Down")]
    if not resolved:
        return
    correct = sum(1 for r in resolved if _b(r, "prediction_correct"))
    print(f"\n  Settlement check: the flagged side won {correct}/{len(resolved)} "
          f"({_pct(correct, len(resolved)).strip()}) of resolved candidates.")
    reached99 = [r for r in resolved if _b(r, "reached_99")]
    if reached99:
        won = sum(1 for r in reached99 if _b(r, "prediction_correct"))
        print(f"  Of those that printed 99c, {_pct(won, len(reached99)).strip()} went on to win.")


def report_rejections(candidates: list[dict]):
    if not candidates:
        return
    traded = sum(1 for c in candidates if _b(c, "traded"))
    print("\n" + "=" * 96)
    print(f"  Candidates: {len(candidates)} seen, {traded} traded, "
          f"{len(candidates) - traded} rejected")
    print("=" * 96)
    reasons = Counter(
        (c.get("reason_rejected") or "").split("(")[0]
        for c in candidates if not _b(c, "traded")
    )
    for reason, count in reasons.most_common(12):
        print(f"  {count:>6}  {reason or 'unknown'}")


def report_trades(trades: list[dict]):
    if not trades:
        print("\n  No trades recorded yet.")
        return
    print("\n" + "=" * 96)
    print(f"  Trades: {len(trades)}")
    print("=" * 96)
    filled = [t for t in trades if _f(t, "entry_filled_qty") > 0]
    pnl = sum(_f(t, "realized_pnl") for t in filled)
    fees = sum(_f(t, "fees_total") for t in filled)
    gross = sum(_f(t, "gross_pnl") for t in filled)
    wins = [t for t in filled if _f(t, "realized_pnl") > 0]
    tp_filled = [t for t in filled if _b(t, "tp_actually_filled")]

    print(f"  filled entries      {len(filled)}   (unfilled: {len(trades) - len(filled)})")
    print(f"  win rate            {_pct(len(wins), len(filled)).strip()}")
    print(f"  gross P&L           ${gross:+.2f}")
    print(f"  fees                ${fees:.2f}"
          f"   ({fees / gross * 100:.0f}% of gross)" if gross > 0 else f"  fees                ${fees:.2f}")
    print(f"  net P&L             ${pnl:+.2f}")
    if filled:
        print(f"  average per trade   ${pnl / len(filled):+.3f}")
    print(f"  TP actually filled  {len(tp_filled)}/{len(filled)} "
          f"({_pct(len(tp_filled), len(filled)).strip()})")

    reached = [t for t in filled if _b(t, "tp_price_reached")]
    queue_ok = [t for t in filled if _b(t, "tp_queue_adjusted_fill")]
    print(f"  TP price reached    {len(reached)}/{len(filled)} "
          f"({_pct(len(reached), len(filled)).strip()})")
    print(f"  TP queue-adj fill   {len(queue_ok)}/{len(filled)} "
          f"({_pct(len(queue_ok), len(filled)).strip()})")
    ahead = [_f(t, "tp_queue_ahead") for t in filled if t.get("tp_queue_ahead")]
    if ahead:
        print(f"  median queue ahead  {statistics.median(ahead):,.0f} shares")
    sources = Counter(t.get("entry_fee_source", "?") for t in filled)
    print(f"  fee sources         {dict(sources)}   "
          f"(clob_trades = what the exchange actually charged)")

    modes = Counter(t.get("mode", "?") for t in trades)
    print(f"  modes               {dict(modes)}")
    print("\n  exit reasons:")
    for reason, count in Counter(t.get("exit_reason", "?") for t in trades).most_common():
        subset = [t for t in trades if t.get("exit_reason") == reason]
        sub_pnl = sum(_f(t, "realized_pnl") for t in subset)
        print(f"    {count:>4}  {reason:<28} ${sub_pnl:+.2f}")

    slip = [
        _f(t, "entry_fill_price") - _f(t, "entry_requested_price")
        for t in filled if _f(t, "entry_fill_price")
    ]
    if slip:
        print(f"\n  entry slippage      avg ${statistics.mean(slip):+.4f}/share")
    partials = sum(1 for t in filled if _b(t, "entry_partial"))
    print(f"  partial entries     {partials}/{len(filled)}")


def main():
    ap = argparse.ArgumentParser(description="Analyse 90c -> 99c data")
    ap.add_argument("--date", help="UTC date to analyse, e.g. 2026-09-17 (default: all)")
    ap.add_argument("--asset", help="filter to one asset, e.g. BTC")
    ap.add_argument("--threshold", type=float, default=0.90,
                    help="entry threshold the data was collected at (labelling only)")
    args = ap.parse_args()

    candidates = _load("candidates", args.date)
    outcomes = _load("candidate_outcomes", args.date)
    trades = _load("trades_9099", args.date)

    if args.asset:
        a = args.asset.upper()
        candidates = [c for c in candidates if c.get("asset", "").upper() == a]
        outcomes = [o for o in outcomes if o.get("asset", "").upper() == a]
        trades = [t for t in trades if t.get("asset", "").upper() == a]

    if not any((candidates, outcomes, trades)):
        print(f"  No data in {DATA_DIR}. Run the strategy first "
              f"(python run_9099.py) and come back once some markets have closed.")
        return 1

    if outcomes:
        report_by_threshold(outcomes)
        report_reach_rates(outcomes, args.threshold)
        report_resolution(outcomes)
        report_reference_quality(outcomes + candidates)
    else:
        print("\n  No candidate outcomes yet — those are written once a market closes.")
    report_rejections(candidates)
    report_trades(trades)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
