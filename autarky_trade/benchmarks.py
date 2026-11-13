"""Cost-aware reference strategies; descriptive comparisons, not p-values."""
from __future__ import annotations
import random
from .core import Account


def reference(bars, strategy, costs, start, end, train_end, seeds=31):
    # Estimate target-signal occupancy and switching probability on training only.
    flags = [strategy.signal([b.close for b in bars[max(0,i-101):i]]) for i in range(100,train_end)]
    p = sum(flags)/len(flags) if flags else 0.
    switches = sum(a!=b for a,b in zip(flags,flags[1:]))
    # Two-state Markov null with comparable target occupancy and turnover.
    rise = min(1.,switches/(2*max(1,flags.count(False))))
    fall = min(1.,switches/(2*max(1,flags.count(True))))
    def simulate(draw):
        a = Account(); state = False
        for b in bars[start:end]:
            state = draw(state)
            a.fill(state and not a.stopped,b.open,b.open,costs)
            a.mark(b.close)
        a.fill(False,bars[end-1].close,bars[end-1].close,costs)
        return a.cash/1000-1
    # Same position fraction and costs as the candidate; cash is uninvested.
    buy_hold = simulate(lambda state: True)
    values = []
    for seed in range(seeds):
        rng = random.Random(seed)
        values.append(simulate(lambda state: rng.random() >= fall if state else rng.random() < rise))
    values.sort()
    return {'cash_return':0.,'fractional_buy_hold_return':buy_hold,
            'random_median':values[len(values)//2],'random_p75':values[int(.75*(len(values)-1))],
            'random_seeds':seeds,'training_signal_occupancy':p,'training_switches':switches}
