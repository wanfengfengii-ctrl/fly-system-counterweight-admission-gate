"""吊杆日检：真实 HTTP + 真实 PostgreSQL 端到端验收，不使用任何假接口。"""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import httpx


def today_str():
    return date.today().isoformat()


def yesterday_str():
    return (date.today() - timedelta(days=1)).isoformat()


def tomorrow_str():
    return (date.today() + timedelta(days=1)).isoformat()


def submit_inspection(
    base_url,
    batten_id,
    inspection_date,
    brake_ok=True,
    rope_ok=True,
    limit_ok=True,
    note=None,
):
    payload = {
        "inspection_date": inspection_date,
        "brake_ok": brake_ok,
        "rope_ok": rope_ok,
        "limit_ok": limit_ok,
    }
    if note is not None:
        payload["abnormality_note"] = note
    return httpx.post(
        f"{base_url}/api/battens/{batten_id}/inspections",
        json=payload,
        timeout=30,
    )


def inspection_history(base_url, batten_id):
    resp = httpx.get(f"{base_url}/api/battens/{batten_id}/inspections", timeout=10)
    assert resp.status_code == 200
    return resp.json()["inspections"]


def batten_state(base_url, batten_id):
    resp = httpx.get(f"{base_url}/api/battens/{batten_id}", timeout=10)
    assert resp.status_code == 200
    return resp.json()


def test_pass_inspection_is_archived_with_server_conclusion(base_url):
    """三项全部正常：服务端判定合格并归档，可按吊杆查询到该记录。"""
    resp = submit_inspection(base_url, "G-01", today_str())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["accepted"] is True
    rec = body["inspection"]
    assert rec["batten_id"] == "G-01"
    assert rec["inspection_date"] == today_str()
    assert rec["conclusion"] == "PASS"
    assert rec["conclusion_label"] == "合格"
    assert rec["brake_ok"] is True
    assert rec["rope_ok"] is True
    assert rec["limit_ok"] is True
    assert rec["abnormality_note"] is None
    assert rec["created_at"]  # 记录时间由服务端写入

    # 按吊杆查看最近记录：数据来自数据库
    history = inspection_history(base_url, "G-01")
    assert [r["inspection_id"] for r in history] == [rec["inspection_id"]]
    assert history[0]["conclusion"] == "PASS"
    assert history[0]["created_at"] == rec["created_at"]
    # 另一根吊杆不受影响
    assert inspection_history(base_url, "G-02") == []


def test_abnormal_inspection_with_note_gets_needs_attention(base_url):
    """任一项异常且说明齐全：服务端判定需处理并归档。"""
    resp = submit_inspection(
        base_url, "G-01", today_str(), rope_ok=False, note="钢丝绳发现断丝"
    )
    assert resp.status_code == 201, resp.text
    rec = resp.json()["inspection"]
    assert rec["conclusion"] == "NEEDS_ATTENTION"
    assert rec["conclusion_label"] == "需处理"
    assert rec["rope_ok"] is False
    assert rec["abnormality_note"] == "钢丝绳发现断丝"

    history = inspection_history(base_url, "G-01")
    assert len(history) == 1
    assert history[0]["conclusion"] == "NEEDS_ATTENTION"


def test_missing_abnormality_note_is_rejected_and_not_persisted(base_url):
    """任一项异常而说明缺失 / 空白：明确拒绝，不留下巡检记录。"""
    for bad_note in (None, "", "   ", " \t "):
        resp = submit_inspection(
            base_url, "G-01", today_str(), brake_ok=False, note=bad_note
        )
        assert resp.status_code == 422, f"说明 {bad_note!r} 未被拒绝: {resp.text}"
        body = resp.json()
        assert body["accepted"] is False
        assert body["reason"] == "MISSING_ABNORMALITY_NOTE"

    # 全部被拒后不留下任何巡检记录
    assert inspection_history(base_url, "G-01") == []


def test_invisible_only_note_is_rejected_and_not_persisted(base_url):
    """只含零宽空格等不可见字符的说明看起来为空：明确拒绝，不留下巡检记录。"""
    for invisible_note in (
        "\u200b",              # 零宽空格
        "\u200b\u200b\u200b",           # 多个零宽空格
        " \u200b ",            # 零宽空格夹普通空格
        "\u200c\u200d",   # 零宽非连接符 / 零宽连接符
        "\ufeff",              # BOM（零宽不换行空格）
        "\u2060",              # 单词连接符
        "\u00ad",              # 软连字符
        "\u3000\u200b",          # 全角空格 + 零宽空格
    ):
        resp = submit_inspection(
            base_url, "G-01", today_str(), brake_ok=False, note=invisible_note
        )
        assert resp.status_code == 422, f"说明 {invisible_note!r} 未被拒绝: {resp.text}"
        body = resp.json()
        assert body["accepted"] is False
        assert body["reason"] == "MISSING_ABNORMALITY_NOTE"

    # 全部被拒后不留下任何巡检记录
    assert inspection_history(base_url, "G-01") == []


def test_invisible_only_note_is_stored_as_empty_when_all_ok(base_url):
    """三项全部正常时，只含不可见字符的说明按未填写归档（说明为空）。"""
    resp = submit_inspection(base_url, "G-01", today_str(), note="\u200b \u200b")
    assert resp.status_code == 201, resp.text
    rec = resp.json()["inspection"]
    assert rec["conclusion"] == "PASS"
    assert rec["abnormality_note"] is None
    assert inspection_history(base_url, "G-01")[0]["abnormality_note"] is None


def test_note_with_visible_content_is_kept_verbatim(base_url):
    """说明中夹带不可见字符但有可见内容：正常归档，原文保留。"""
    resp = submit_inspection(
        base_url, "G-01", today_str(), rope_ok=False, note="钢丝\u200b绳断丝"
    )
    assert resp.status_code == 201, resp.text
    rec = resp.json()["inspection"]
    assert rec["conclusion"] == "NEEDS_ATTENTION"
    assert rec["abnormality_note"] == "钢丝\u200b绳断丝"


def test_conclusion_cannot_be_specified_by_client(base_url):
    """客户端夹带的结论字段不被采信：异常项存在时服务端仍判定需处理。"""
    resp = httpx.post(
        f"{base_url}/api/battens/G-01/inspections",
        json={
            "inspection_date": today_str(),
            "brake_ok": True,
            "rope_ok": False,
            "limit_ok": True,
            "abnormality_note": "钢丝绳发现断丝",
            "conclusion": "PASS",  # 客户端试图指定结论，必须被忽略
        },
        timeout=10,
    )
    assert resp.status_code == 201, resp.text
    rec = resp.json()["inspection"]
    assert rec["conclusion"] == "NEEDS_ATTENTION"
    assert inspection_history(base_url, "G-01")[0]["conclusion"] == "NEEDS_ATTENTION"


def test_same_batten_same_date_only_one_record(base_url):
    """每根吊杆同一营业日期只接纳一份：重复提交返回已完成提示，原记录不变。"""
    first = submit_inspection(base_url, "G-01", today_str())
    assert first.status_code == 201
    first_id = first.json()["inspection"]["inspection_id"]

    # 同日重复提交（即使内容不同）一律返回已完成提示，不覆盖原记录
    second = submit_inspection(
        base_url, "G-01", today_str(), rope_ok=False, note="重复提交不应覆盖"
    )
    assert second.status_code == 409
    body = second.json()
    assert body["accepted"] is False
    assert body["reason"] == "INSPECTION_EXISTS"
    assert "已完成" in body["message"]

    history = inspection_history(base_url, "G-01")
    assert len(history) == 1
    assert history[0]["inspection_id"] == first_id
    assert history[0]["conclusion"] == "PASS"
    assert history[0]["rope_ok"] is True
    assert history[0]["abnormality_note"] is None

    # 同一营业日期、不同吊杆互不影响
    other = submit_inspection(base_url, "G-02", today_str())
    assert other.status_code == 201

    # 同一吊杆、不同营业日期可以另存一份；最近记录按营业日期倒序
    previous = submit_inspection(base_url, "G-01", yesterday_str())
    assert previous.status_code == 201
    history = inspection_history(base_url, "G-01")
    assert [r["inspection_date"] for r in history] == [today_str(), yesterday_str()]


def test_future_date_is_rejected_and_not_persisted(base_url):
    """未来日期：明确拒绝，不留下巡检记录。"""
    resp = submit_inspection(base_url, "G-01", tomorrow_str())
    assert resp.status_code == 422
    body = resp.json()
    assert body["accepted"] is False
    assert body["reason"] == "INVALID_DATE"
    assert inspection_history(base_url, "G-01") == []


def test_unknown_batten_is_rejected(base_url):
    resp = submit_inspection(base_url, "G-99", today_str())
    assert resp.status_code == 404
    assert resp.json()["reason"] == "BATTEN_NOT_FOUND"

    resp = httpx.get(f"{base_url}/api/battens/G-99/inspections", timeout=10)
    assert resp.status_code == 404
    assert resp.json()["reason"] == "BATTEN_NOT_FOUND"


def test_non_boolean_check_items_are_rejected(base_url):
    """检查项必须是布尔值：字符串等一律 422，不做隐式转换，不落库。"""
    resp = httpx.post(
        f"{base_url}/api/battens/G-01/inspections",
        json={
            "inspection_date": today_str(),
            "brake_ok": "true",
            "rope_ok": True,
            "limit_ok": True,
        },
        timeout=10,
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["accepted"] is False
    assert body["reason"] == "INVALID_INPUT"
    assert inspection_history(base_url, "G-01") == []


def test_malformed_date_is_rejected(base_url):
    """营业日期格式非法：422 拒绝，不落库。"""
    resp = submit_inspection(base_url, "G-01", "2026-13-40")
    assert resp.status_code == 422
    assert resp.json()["reason"] == "INVALID_INPUT"
    assert inspection_history(base_url, "G-01") == []


def test_failed_inspections_leave_no_residue_and_do_not_touch_loads(base_url):
    """失败请求不留下巡检记录，也不改变配重装载 / 转移 / 修正 / 拆下数据。"""
    # 先装载一片配重作为现场状态
    load = httpx.post(
        f"{base_url}/api/battens/G-01/loads",
        json={"piece_id": "CW-INSP", "weight_grams": 20000},
        timeout=10,
    )
    assert load.status_code == 201

    # 各类失败请求：未来日期、缺异常说明、未知吊杆
    assert submit_inspection(base_url, "G-01", tomorrow_str()).status_code == 422
    assert (
        submit_inspection(base_url, "G-01", today_str(), rope_ok=False).status_code
        == 422
    )
    assert submit_inspection(base_url, "G-99", today_str()).status_code == 404

    # 不留下任何巡检记录
    assert inspection_history(base_url, "G-01") == []
    assert inspection_history(base_url, "G-02") == []

    # 配重装载数据未被改变
    state = batten_state(base_url, "G-01")
    assert state["total_grams"] == 20000
    assert state["remaining_grams"] == 10000
    assert [l["piece_id"] for l in state["loads"]] == ["CW-INSP"]

    # 合法日检成功归档后，装载数据同样不受影响
    ok = submit_inspection(base_url, "G-01", today_str())
    assert ok.status_code == 201
    state = batten_state(base_url, "G-01")
    assert state["total_grams"] == 20000
    assert state["remaining_grams"] == 10000
    assert [l["piece_id"] for l in state["loads"]] == ["CW-INSP"]


def test_concurrent_same_day_inspections_only_one_succeeds(base_url):
    """同一吊杆同一营业日期并发提交：唯一约束兜底，仅一份归档。"""
    barrier = threading.Barrier(2)

    def submit(note):
        barrier.wait(timeout=10)
        return submit_inspection(base_url, "G-01", today_str(), note=note)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, [None, "并发重复提交"]))

    assert sorted(r.status_code for r in responses) == [201, 409]
    loser = next(r for r in responses if r.status_code == 409).json()
    assert loser["reason"] == "INSPECTION_EXISTS"
    assert len(inspection_history(base_url, "G-01")) == 1
