"""演出拆台（确认拆下）：真实 HTTP + 真实 PostgreSQL 端到端验收，不使用任何假接口。"""

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


def remove(base_url, batten_id, load_id):
    return httpx.post(
        f"{base_url}/api/battens/{batten_id}/loads/{load_id}/remove",
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


def _direct_db_url():
    """真实数据库直连地址：容器内取 DATABASE_URL（@db），宿主机默认 localhost。"""
    import os

    raw = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg2://rigging:rigging@localhost:5432/rigging",
    )
    # psycopg2 不认 SQLAlchemy 的 "+psycopg2" 方言后缀
    return raw.replace("postgresql+psycopg2://", "postgresql://")


def test_remove_releases_capacity_and_disappears_from_detail(base_url):
    """拆下后容量立即释放：总重下降、余量回升，明细不再包含该片。"""
    assert load_piece(base_url, "G-01", "CW-RM", 20000).status_code == 201
    before = batten_state(base_url, "G-01")
    load_before = next(l for l in before["loads"] if l["piece_id"] == "CW-RM")
    assert before["total_grams"] == 20000
    assert before["remaining_grams"] == 10000

    resp = remove(base_url, "G-01", load_before["load_id"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] is True
    assert body["already_removed"] is False
    # 明确给出本次释放的重量与刷新后的总重 / 余量
    assert body["released_grams"] == 20000
    assert body["weight_grams"] == 20000
    assert body["total_grams"] == 0
    assert body["remaining_grams"] == 30000
    assert body["removed_at"]

    # 在杆明细与容量汇总只认在杆记录：该片已消失、容量已释放
    after = batten_state(base_url, "G-01")
    assert after["total_grams"] == 0
    assert after["remaining_grams"] == 30000
    assert after["loads"] == []


def test_remove_frees_capacity_for_new_load(base_url):
    """拆下腾出的余量必须真实可用：再登记一片此前放不下的配重。"""
    assert load_piece(base_url, "G-01", "CW-OCC1", 20000).status_code == 201
    assert load_piece(base_url, "G-01", "CW-OCC2", 5000).status_code == 201
    # 已满 25000，再放 8000 会超 30000
    blocked = load_piece(base_url, "G-01", "CW-WANT", 8000)
    assert blocked.status_code == 409
    assert blocked.json()["reason"] == "OVER_CAPACITY"

    lid = _load_id(batten_state(base_url, "G-01"), "CW-OCC1")
    resp = remove(base_url, "G-01", lid)
    assert resp.status_code == 200
    assert resp.json()["released_grams"] == 20000

    # 拆下 20000 后余量 25000，8000 克的新片可以登记
    admitted = load_piece(base_url, "G-01", "CW-WANT", 8000)
    assert admitted.status_code == 201, admitted.text
    state = batten_state(base_url, "G-01")
    assert state["total_grams"] == 5000 + 8000
    assert state["remaining_grams"] == 17000


def test_removed_record_keeps_identity_weight_owner_and_time(base_url):
    """拆下是软删除：记录仍在库中，标识 / 重量 / 归属 / 登记时间全部保留可追溯。"""
    import psycopg2

    assert load_piece(base_url, "G-01", "CW-KEEP", 12000).status_code == 201
    before = batten_state(base_url, "G-01")
    row_before = before["loads"][0]

    resp = remove(base_url, "G-01", row_before["load_id"])
    assert resp.status_code == 200

    # 直接查真实数据库：记录未删除，拆下时刻已写入，其余字段原样保留
    conn = psycopg2.connect(_direct_db_url())
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT batten_id, piece_id, weight_grams, created_at, removed_at "
            "FROM loads WHERE id = %s",
            (row_before["load_id"],),
        )
        db_row = cur.fetchone()
    finally:
        conn.close()
    assert db_row is not None
    batten_id, piece_id, weight_grams, created_at, removed_at = db_row
    assert batten_id == "G-01"  # 归属保留
    assert piece_id == "CW-KEEP"  # 标识保留
    assert weight_grams == 12000  # 重量保留
    assert created_at.isoformat() == row_before["created_at"]  # 最初登记时间保留
    assert removed_at is not None  # 拆下时间已写入


def test_repeat_remove_is_idempotent_and_does_not_double_release(base_url):
    """重复拆下：返回明确结果且本次释放重量为 0，容量不被重复释放。"""
    assert load_piece(base_url, "G-01", "CW-DUP-RM", 7000).status_code == 201
    lid = _load_id(batten_state(base_url, "G-01"), "CW-DUP-RM")

    first = remove(base_url, "G-01", lid)
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["accepted"] is True
    assert first_body["already_removed"] is False
    assert first_body["released_grams"] == 7000
    removed_at = first_body["removed_at"]

    second = remove(base_url, "G-01", lid)
    assert second.status_code == 200
    body = second.json()
    assert body["accepted"] is True
    # 幂等：明确告知已拆下，本次释放重量为 0，拆下时刻不被改写
    assert body["already_removed"] is True
    assert body["released_grams"] == 0
    assert body["removed_at"] == removed_at
    assert body["total_grams"] == 0
    assert body["remaining_grams"] == 30000

    # 容量没有被二次释放（始终是空杆）
    state = batten_state(base_url, "G-01")
    assert state["total_grams"] == 0
    assert state["remaining_grams"] == 30000
    assert state["loads"] == []


def test_removed_piece_id_cannot_be_registered_again(base_url):
    """旧标识不能再登记：即使已拆下，全库唯一标识仍被占用。"""
    assert load_piece(base_url, "G-01", "CW-OLD-ID", 5000).status_code == 201
    lid = _load_id(batten_state(base_url, "G-01"), "CW-OLD-ID")
    assert remove(base_url, "G-01", lid).status_code == 200

    # 在原吊杆重新登记同一标识 -> 拒绝
    again_same = load_piece(base_url, "G-01", "CW-OLD-ID", 5000)
    assert again_same.status_code == 409
    assert again_same.json()["reason"] == "PIECE_ID_EXISTS"

    # 换到另一根吊杆同样拒绝：历史唯一标识全库占用
    again_other = load_piece(base_url, "G-02", "CW-OLD-ID", 5000)
    assert again_other.status_code == 409
    assert again_other.json()["reason"] == "PIECE_ID_EXISTS"

    # 两根杆都没有因此产生新记录
    assert batten_state(base_url, "G-01")["loads"] == []
    assert batten_state(base_url, "G-02")["loads"] == []


def test_concurrent_remove_and_transfer_only_one_takes_effect(base_url):
    """拆下与转移并发：同一吊杆行锁串行，恰一个动作生效，容量与归属最终一致。

    无论锁队列先执行哪个：另一个必须收到 POSITION_CHANGED，
    配重片不会既被拆下又被转移，容量绝不重复变化。
    """
    assert load_piece(base_url, "G-01", "CW-RACE", 10000).status_code == 201
    lid = _load_id(batten_state(base_url, "G-01"), "CW-RACE")

    barrier = threading.Barrier(2)

    def submit(call):
        barrier.wait(timeout=10)
        return call()

    with ThreadPoolExecutor(max_workers=2) as pool:
        remove_resp, transfer_resp = list(
            pool.map(
                submit,
                [
                    lambda: remove(base_url, "G-01", lid),
                    lambda: transfer(base_url, "G-01", lid, "G-02"),
                ],
            )
        )

    # 不死锁（无 500）、不重复生效：恰有一个动作成功
    assert sorted(r.status_code for r in (remove_resp, transfer_resp)) == [200, 409]
    loser = next(r for r in (remove_resp, transfer_resp) if r.status_code == 409)
    assert loser.json()["reason"] == "POSITION_CHANGED"

    g01 = batten_state(base_url, "G-01")
    g02 = batten_state(base_url, "G-02")
    in_detail = {
        l["piece_id"] for d in (g01, g02) for l in d["loads"]
    }
    # 在杆明细里该片要么不在（已拆下），要么恰在一根杆上（已转移）
    assert "CW-RACE" not in in_detail or sum(
        "CW-RACE" in {l["piece_id"] for l in d["loads"]} for d in (g01, g02)
    ) == 1

    if remove_resp.status_code == 200:
        # 拆下先生效：容量在 G-01 释放，转移看到已拆下被拒
        assert remove_resp.json()["released_grams"] == 10000
        assert transfer_resp.json()["reason"] == "POSITION_CHANGED"
        assert g01["total_grams"] == 0
        assert g01["remaining_grams"] == 30000
        assert g02["loads"] == []
    else:
        # 转移先生效：片子在 G-02，拆下看到位置已变化被拒，容量未被重复释放
        assert transfer_resp.status_code == 200
        assert remove_resp.json()["reason"] == "POSITION_CHANGED"
        assert g01["total_grams"] == 0
        assert g01["remaining_grams"] == 30000
        assert [l["piece_id"] for l in g02["loads"]] == ["CW-RACE"]
        assert g02["total_grams"] == 10000
        assert g02["remaining_grams"] == 40000


def test_remove_after_transfer_reports_position_changed_and_no_capacity_released(base_url):
    """记录已被其他终端转移：旧界面拆下时反馈当前位置已变化，源杆容量不变。"""
    assert load_piece(base_url, "G-01", "CW-MOVED", 5000).status_code == 201
    lid = _load_id(batten_state(base_url, "G-01"), "CW-MOVED")
    assert transfer(base_url, "G-01", lid, "G-02").status_code == 200

    # 旧界面仍以为它在 G-01，点“确认拆下”
    resp = remove(base_url, "G-01", lid)
    assert resp.status_code == 409
    body = resp.json()
    assert body["accepted"] is False
    assert body["reason"] == "POSITION_CHANGED"
    assert "当前位置已变化" in body["message"]

    # 没有错误地从 G-01 释放容量；片子完好挂在 G-02
    assert batten_state(base_url, "G-01")["loads"] == []
    g02 = batten_state(base_url, "G-02")
    assert [l["piece_id"] for l in g02["loads"]] == ["CW-MOVED"]
    assert g02["total_grams"] == 5000
    assert g02["remaining_grams"] == 45000


def test_correct_or_transfer_after_remove_is_rejected(base_url):
    """拆下后不能再转移或修正：只认在杆记录，拒绝且状态不变。"""
    assert load_piece(base_url, "G-01", "CW-FROZEN", 9000).status_code == 201
    lid = _load_id(batten_state(base_url, "G-01"), "CW-FROZEN")
    assert remove(base_url, "G-01", lid).status_code == 200

    t = transfer(base_url, "G-01", lid, "G-02")
    assert t.status_code == 409
    assert t.json()["reason"] == "POSITION_CHANGED"

    c = correct_weight(base_url, "G-01", lid, 12345)
    assert c.status_code == 409
    assert c.json()["reason"] == "POSITION_CHANGED"

    # 依旧拆下、空杆，重量等历史数据未被改动
    assert batten_state(base_url, "G-01")["loads"] == []
    assert batten_state(base_url, "G-02")["loads"] == []


def test_remove_unknown_batten_or_load_is_rejected(base_url):
    assert remove(base_url, "G-99", 1).status_code == 404
    assert remove(base_url, "G-99", 1).json()["reason"] == "BATTEN_NOT_FOUND"

    resp = remove(base_url, "G-01", 999999)
    assert resp.status_code == 404
    assert resp.json()["reason"] == "LOAD_NOT_FOUND"


def test_remove_with_non_numeric_load_id_reports_invalid_load_id(base_url):
    """装载编号不是数字：必须说明装载编号不合法，而非误报配重标识 / 重量有误。"""
    resp = remove(base_url, "G-01", "abc")
    assert resp.status_code == 422
    body = resp.json()
    assert body["accepted"] is False
    assert body["reason"] == "INVALID_INPUT"
    assert "装载编号" in body["message"]
    assert "配重片标识" not in body["message"]


def test_legacy_rows_without_removed_at_are_treated_as_on_rod(base_url):
    """旧数据升级：没有 removed_at 的历史记录统一视为在杆，可正常拆下并释放容量。"""
    import psycopg2

    # 经正常接口登记一片，随后直接把 removed_at 列置空，模拟“旧数据没有该列”
    assert load_piece(base_url, "G-01", "CW-LEGACY", 6000).status_code == 201
    lid = _load_id(batten_state(base_url, "G-01"), "CW-LEGACY")

    conn = psycopg2.connect(_direct_db_url())
    try:
        cur = conn.cursor()
        cur.execute("UPDATE loads SET removed_at = NULL WHERE id = %s", (lid,))
        conn.commit()
    finally:
        conn.close()

    # 旧记录被视为在杆：计入总重、出现在明细
    state = batten_state(base_url, "G-01")
    assert state["total_grams"] == 6000
    assert any(l["piece_id"] == "CW-LEGACY" for l in state["loads"])

    # 可以正常拆下并释放容量
    resp = remove(base_url, "G-01", lid)
    assert resp.status_code == 200
    assert resp.json()["released_grams"] == 6000
    after = batten_state(base_url, "G-01")
    assert after["total_grams"] == 0
    assert after["remaining_grams"] == 30000
    assert after["loads"] == []
