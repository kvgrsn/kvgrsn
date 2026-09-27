from liqmon.config import ChainConfig
from liqmon.db.store import Store
from liqmon.indexer.event_indexer import EventIndexer, EventSpec, fetch_logs_adaptive
from liqmon.rpc.abi import pad_address_topic, topic_to_address
from liqmon.rpc.client import RpcError

TOPIC = "0x" + "11" * 32
POOL = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"


def chain(**kw):
    base = dict(
        key="test", chain_id=1, name="t", native_symbol="T", wrapped_native=POOL, multicall3=POOL,
        rpc_urls=("http://x",), confirmations=2, reorg_depth=8, max_log_range=100,
        requests_per_second=100.0, multicall_chunk=50, rpc_batch_size=10,
    )
    base.update(kw)
    return ChainConfig(**base)


class FakeClient:
    """Minimal chain: blocks 0..head with deterministic hashes and one log per 10 blocks."""

    def __init__(self, head=500, max_span=100):
        self.head = head
        self.max_span = max_span
        self.hash_salt = ""
        self.calls = 0

    async def block_number(self):
        return self.head

    async def get_block(self, n, full=False):
        return {"hash": f"0x{n:x}{self.hash_salt}", "number": hex(n)}

    async def get_logs(self, flt):
        self.calls += 1
        lo, hi = int(flt["fromBlock"], 16), int(flt["toBlock"], 16)
        if hi - lo + 1 > self.max_span:
            raise RpcError(-32602, "query exceeds max block range 100")
        out = []
        for b in range(lo, hi + 1):
            if b % 10 == 0:
                acct = "0x" + f"{b:040x}"
                out.append({
                    "topics": [TOPIC, pad_address_topic(POOL), pad_address_topic(acct)],
                    "blockNumber": hex(b), "blockHash": f"0x{b:x}{self.hash_salt}",
                    "transactionHash": f"0xtx{b}", "logIndex": "0x0",
                })
        return out


def spec():
    return EventSpec("borrow", POOL, TOPIC, lambda lg: topic_to_address(lg["topics"][2]))


async def test_adaptive_range_splitting():
    c = FakeClient(max_span=37)
    logs = await fetch_logs_adaptive(c, [POOL], [TOPIC], 0, 399, max_range=400)
    assert len(logs) == 40


async def test_incremental_sync_and_confirmations():
    c = FakeClient(head=500)
    store = Store(":memory:")
    idx = EventIndexer(c, store, chain(), "p", [spec()], deployment_block=0)
    st = await idx.sync()
    assert (st.from_block, st.to_block) == (0, 498)  # head - confirmations
    assert st.new_positions == 50  # blocks 0,10,...,490
    c.head = 600
    st2 = await idx.sync()
    assert st2.from_block == 499 and st2.to_block == 598
    assert st2.new_positions == 10


async def test_reorg_detection_rolls_back():
    c = FakeClient(head=300)
    store = Store(":memory:")
    idx = EventIndexer(c, store, chain(), "p", [spec()], deployment_block=0)
    await idx.sync()  # checkpoint 298 with hash
    c.head = 350
    await idx.sync()  # checkpoint 348
    c.hash_salt = "ff"  # every block hash changes -> no stored hash matches
    rolled = await idx.check_reorg()
    assert rolled is not None and rolled < 298
    assert store.get_checkpoint("p:positions") == rolled
