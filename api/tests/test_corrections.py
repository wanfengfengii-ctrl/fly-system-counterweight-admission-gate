"""重量修正：真实 HTTP + 真实 PostgreSQL 端到端验收，不使用任何假接口。"""

import threading
from concurrent.futures import ThreadPoolExecutor

import httpx


def load_piece(base_url, batten_id, piece_id, weight_grams):
    return httpx.post(
        f"{base_url}/api/battens/{batten_id}/loads",
        json={"piece_id": piece_id, "weight_grams": weight_grams},
        timeout=30,
    )


def batten_state(base_url, batten_id):
    resp = httpx.get(f"{base_url}/api/battens/{batten_id}", timeout=10)
    assert resp.status_code == 200
    return resp.json()


def transfer(base_url, source_id, load_id, target_id):
    return httpx.post(
        f"{base_url}/api/battens/{source_id}/loads/{load_id}/transfer",
        json={"target_batten_id": target_id},
        timeout=30,
    )


def correct_weight(base_url, batten_id, load_id, weight_grams):
    return httpx.post(
        f"{base_url}/api/battens/{batten_id}/loads/{load_id}/correct",
        json={"weight_grams": weight_grams},
        timeout=30,
    )


def _load_id(detail, piece_id):
    return next(l["load_id"] for l in detail["loads"] if l["piece_id"] == piece_id)


def test_correct_weight_reduce_updates_totals_and_preserves_identity(base_url):
    """减重：总重 / 余量按差值刷新，标识与最初登记时间保留。"""
    assert load_piece(base_url, "G-01", "CW-DIET", 20000).status_code == 201
    before = batten_state(base_url, "G-01")
    load_before = before["loads"][0]

    resp = correct_weight(base_url, "G-01", load_before["load_id"], 15000)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] is True
    assert body["load_id"] == load_before["load_id"]
    assert body["piece_id"] == "CW-DIET"
    assert body["previous_weight_grams"] == 20000
    assert body["weight_grams"] == 15000
    assert body["total_grams"] == 15000
    assert body["remaining_grams"] == 15000

    # 刷新后以数据库为准：同一笔记录，只改了重量
    after = batten_state(base_url, "G-01")
    assert after["total_grams"] == 15000
    assert after["remaining_grams"] == 15000
    assert len(after["loads"]) == 1
    fixed = after["loads"][0]
    assert fixed["load_id"] == load_before["load_id"]
    assert fixed["piece_id"] == "CW-DIET"
    assert fixed["weight_grams"] == 15000
    # 配重片标识与最初登记时间保留，不是重新登记
    assert fixed["created_at"] == load_before["created_at"]


def test_correct_weight_increase_to_exact_capacity_is_allowed(base_url):
    """增重至恰好满载：新旧重量差使合计等于核定值，允许写入。"""
    load_piece(base_url, "G-01", "CW-BULK-A", 20000)
    load_piece(base_url, "G-01", "CW-BULK-B", 5000)
    load_id = _load_id(batten_state(base_url, "G-01"), "CW-BULK-B")

    # 25000 - 5000 + 10000 = 30000，恰好达到核定值
    resp = correct_weight(base_url, "G-01", load_id, 10000)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] is True
    assert body["total_grams"] == 30000
    assert body["remaining_grams"] == 0

    state = batten_state(base_url, "G-01")
    assert state["total_grams"] == 30000
    assert state["remaining_grams"] == 0
    weights = {l["piece_id"]: l["weight_grams"] for l in state["loads"]}
    assert weights == {"CW-BULK-A": 20000, "CW-BULK-B": 10000}


def test_correct_weight_over_capacity_is_rejected_and_weight_unchanged(base_url):
    """超载拒绝：数据库中的原重量保持不变。"""
    load_piece(base_url, "G-01", "CW-HEAVY-A", 20000)
    load_piece(base_url, "G-01", "CW-HEAVY-B", 10000)
    before = batten_state(base_url, "G-01")
    load_id = _load_id(before, "CW-HEAVY-A")

    # 30000 - 20000 + 20001 = 30001，超出核定 1 克
    resp = correct_weight(base_url, "G-01", load_id, 20001)
    assert resp.status_code == 409
    body = resp.json()
    assert body["accepted"] is False
    assert body["reason"] == "OVER_CAPACITY"

    state = batten_state(base_url, "G-01")
    assert state["total_grams"] == 30000
    assert state["remaining_grams"] == 0
    weights = {l["piece_id"]: l["weight_grams"] for l in state["loads"]}
    assert weights == {"CW-HEAVY-A": 20000, "CW-HEAVY-B": 10000}


def test_correct_weight_out_of_range_is_rejected(base_url):
    """新重量沿用单片 100～25000 克范围，越界拒绝且原重量不变。"""
    load_piece(base_url, "G-01", "CW-RANGE", 20000)
    load_id = _load_id(batten_state(base_url, "G-01"), "CW-RANGE")

    for bad_weight in (0, 99, 25001, -100):
        resp = correct_weight(base_url, "G-01", load_id, bad_weight)
        assert resp.status_code == 422, f"重量 {bad_weight} 未被拒绝: {resp.text}"
        assert resp.json()["reason"] == "INVALID_WEIGHT"

    state = batten_state(base_url, "G-01")
    assert state["total_grams"] == 20000
    assert state["loads"][0]["weight_grams"] == 20000


def test_non_integer_correct_weight_is_rejected(base_url):
    """字符串、小数、布尔等伪装的新重量必须明确拒绝且不生效。"""
    load_piece(base_url, "G-01", "CW-TYPEFIX", 20000)
    load_id = _load_id(batten_state(base_url, "G-01"), "CW-TYPEFIX")

    for bad_weight in ("15000", 100.0, 199.5, True, None):
        resp = httpx.post(
            f"{base_url}/api/battens/G-01/loads/{load_id}/correct",
            json={"weight_grams": bad_weight},
            timeout=10,
        )
        assert resp.status_code == 422, f"载荷 {bad_weight!r} 未被拒绝: {resp.text}"
        body = resp.json()
        assert body["accepted"] is False
        assert body["reason"] == "INVALID_INPUT"

    state = batten_state(base_url, "G-01")
    assert state["total_grams"] == 20000
    assert state["loads"][0]["weight_grams"] == 20000


def test_correct_after_transfer_reports_position_changed(base_url):
    """记录已被其他终端转移：明确提示当前位置已变化，原重量不变。"""
    load_piece(base_url, "G-01", "CW-MOVED", 5000)
    load_id = _load_id(batten_state(base_url, "G-01"), "CW-MOVED")

    # 另一终端先把片子转移到 G-02
    assert transfer(base_url, "G-01", load_id, "G-02").status_code == 200

    # 旧界面仍按 G-01 的明细提交修正
    resp = correct_weight(base_url, "G-01", load_id, 8000)
    assert resp.status_code == 409
    body = resp.json()
    assert body["accepted"] is False
    assert body["reason"] == "POSITION_CHANGED"
    assert "当前位置已变化" in body["message"]

    # 片子留在 G-02，重量未被修改
    assert batten_state(base_url, "G-01")["loads"] == []
    dst = batten_state(base_url, "G-02")
    assert [l["piece_id"] for l in dst["loads"]] == ["CW-MOVED"]
    assert dst["loads"][0]["weight_grams"] == 5000
    assert dst["total_grams"] == 5000


def test_correct_weight_unknown_batten_or_load_is_rejected(base_url):
    resp = correct_weight(base_url, "G-99", 1, 5000)
    assert resp.status_code == 404
    assert resp.json()["reason"] == "BATTEN_NOT_FOUND"

    resp = correct_weight(base_url, "G-01", 999999, 5000)
    assert resp.status_code == 404
    assert resp.json()["reason"] == "LOAD_NOT_FOUND"


def test_concurrent_correct_and_transfer_leave_consistent_ownership_and_capacity(
    base_url,
):
    """修正与转移并发：同一吊杆行锁串行裁决，归属与容量最终一致。

    G-01 挂 CW-A 5000 + CW-FIX 20000（余 5000），G-02 挂 CW-B 20000 + CW-C 10000
    （共 30000，余 20000；单片不得超过 25000 克，故 30000 克拆成两片登记）：
      T1: 修正 CW-FIX 20000 → 25000（G-01 恰好满载 30000，允许）
      T2: 转移 CW-FIX G-01 → G-02（按旧重量恰好满载 50000，允许；
          按新重量 30000+25000=55000 超载，必须拒绝）
    无论哪个先拿到吊杆行锁：恰有一笔成功，CW-FIX 全库仍恰好登记一次，
    两杆均不超限，且重量与归属相互一致。
    """
    load_piece(base_url, "G-01", "CW-A", 5000)
    load_piece(base_url, "G-01", "CW-FIX", 20000)
    # 单片上限 25000 克：30000 克用两片合法配重片凑出，确保 G-02 余 20000 克
    rb1 = load_piece(base_url, "G-02", "CW-B", 20000)
    rb2 = load_piece(base_url, "G-02", "CW-C", 10000)
    assert rb1.status_code == 201 and rb2.status_code == 201
    load_id = _load_id(batten_state(base_url, "G-01"), "CW-FIX")

    barrier = threading.Barrier(2)

    def submit(call):
        barrier.wait(timeout=10)
        return call()

    with ThreadPoolExecutor(max_workers=2) as pool:
        correct_resp, transfer_resp = list(
            pool.map(
                submit,
                [
                    lambda: correct_weight(base_url, "G-01", load_id, 25000),
                    lambda: transfer(base_url, "G-01", load_id, "G-02"),
                ],
            )
        )

    # 不死锁（无 500）、不重复生效：恰有一笔成功
    assert sorted(r.status_code for r in (correct_resp, transfer_resp)) == [200, 409]

    g01 = batten_state(base_url, "G-01")
    g02 = batten_state(base_url, "G-02")
    # 容量守恒：两杆均不超限
    assert 0 <= g01["total_grams"] <= g01["capacity_grams"]
    assert 0 <= g02["total_grams"] <= g02["capacity_grams"]
    assert g01["remaining_grams"] >= 0
    assert g02["remaining_grams"] >= 0

    on_g01 = {l["piece_id"]: l for l in g01["loads"]}
    on_g02 = {l["piece_id"]: l for l in g02["loads"]}
    # 归属守恒：CW-FIX 全库恰好登记一次，只在一根杆上
    assert ("CW-FIX" in on_g01) != ("CW-FIX" in on_g02)
    assert "CW-A" in on_g01
    # 预置在 G-02 的两片始终留杆，不参与本次裁决
    assert "CW-B" in on_g02
    assert "CW-C" in on_g02

    if "CW-FIX" in on_g01:
        # 修正先生效：新重量留在源杆，转移按新重量裁决超载被拒
        assert correct_resp.status_code == 200
        assert correct_resp.json()["weight_grams"] == 25000
        assert transfer_resp.status_code == 409
        assert transfer_resp.json()["reason"] == "OVER_CAPACITY"
        assert on_g01["CW-FIX"]["weight_grams"] == 25000
        assert g01["total_grams"] == 30000
        assert g02["total_grams"] == 30000
    else:
        # 转移先生效：原重量随片迁走，修正看到归属已变被拒
        assert transfer_resp.status_code == 200
        assert correct_resp.status_code == 409
        assert correct_resp.json()["reason"] == "POSITION_CHANGED"
        assert on_g02["CW-FIX"]["weight_grams"] == 20000
        assert g01["total_grams"] == 5000
        assert g02["total_grams"] == 50000
