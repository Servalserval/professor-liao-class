"""遲滯的效果：先選一次市場，15 分鐘後再選一次（有／沒有遲滯），比較留下來的比例。"""
import time
from pmopt.mm.config import MMConfig
from pmopt.mm.feeds import PublicData
from pmopt.mm.universe import candidates, solve_budget

cfg, data = MMConfig(), PublicData()

def snapshot():
    rows = data.reward_markets()
    yes = [str(t["token_id"]) for m in rows for t in (m.get("tokens") or []) if str(t.get("outcome", "")).lower() == "yes"]
    return rows, data.books(yes)

def plan(rows, books, inc=frozenset()):
    p, mult = solve_budget(candidates(rows, books, cfg, time.time(), inc), cfg, inc)
    return p, mult

rows, books = snapshot()
a, ma = plan(rows, books)
A = frozenset(a.condition_id)
print(f"A: {len(A)} 個市場、本金 {a.capital.sum():,.0f}、門檻 {ma:.2f}", flush=True)
time.sleep(900)
rows, books = snapshot()
for name, inc in (("沒有遲滯", frozenset()), ("有遲滯", A)):
    b, mb = plan(rows, books, inc)
    B = set(b.condition_id)
    kept = len(A & B)
    print(f"{name}: {len(B)} 個市場、本金 {b.capital.sum():,.0f}、門檻 {mb:.2f}、留下 {kept}/{len(A)}（{kept / len(A):.0%}）、"
          f"新進 {len(B - A)}、預期淨額 {b.exp_net.sum():,.0f}", flush=True)
