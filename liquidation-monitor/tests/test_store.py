from liqmon.db.store import PositionEvent, Store


def ev(account, block, idx=0, tx=None):
    return PositionEvent("p", account, "borrow", block, f"0xh{block}", tx or f"0xt{block}{account}{idx}", idx)


def test_positions_are_derived_from_events_and_deduplicated():
    s = Store(":memory:")
    s.add_events([ev("0xA", 10), ev("0xA", 12), ev("0xB", 11)])
    s.add_events([ev("0xA", 10)])  # duplicate log ignored
    pos = {p.account: p for p in s.positions("p")}
    assert set(pos) == {"0xA", "0xB"}
    assert pos["0xA"].first_seen_block == 10 and pos["0xA"].last_event_block == 12


def test_closed_position_reactivates_on_new_borrow():
    s = Store(":memory:")
    s.add_events([ev("0xA", 10)])
    s.update_position_health("p", [("0xA", "closed", None, 0.0, 0.0)], 20)
    assert s.positions("p") == []
    s.add_events([ev("0xA", 30)])
    assert [p.account for p in s.positions("p")] == ["0xA"]


def test_rollback_discards_events_after_fork_point():
    s = Store(":memory:")
    s.add_events([ev("0xA", 10), ev("0xB", 15), ev("0xA", 16)])
    s.set_checkpoint("p:positions", 16, "0xhash16", keep_hashes=10)
    removed = s.rollback("p:positions", "p", 12)
    assert removed == 2
    pos = {p.account: p for p in s.positions("p")}
    assert set(pos) == {"0xA"}  # 0xB only existed after the fork point
    assert pos["0xA"].last_event_block == 10
    assert s.get_checkpoint("p:positions") == 12
    assert s.recent_block_hashes("p:positions") == []


def test_checkpoint_keeps_limited_hash_history():
    s = Store(":memory:")
    for b in range(1, 8):
        s.set_checkpoint("x", b, f"0x{b}", keep_hashes=3)
    assert [n for n, _ in s.recent_block_hashes("x")] == [7, 6, 5]
