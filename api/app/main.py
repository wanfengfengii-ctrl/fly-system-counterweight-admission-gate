from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Annotated

from fastapi import Depends, FastAPI, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import Base, SessionLocal, engine, get_db
from .models import Batten, Inspection, Load
from .schemas import InspectionCreate, LoadCreate, TransferCreate, WeightCorrect

MIN_WEIGHT_GRAMS = 100
MAX_WEIGHT_GRAMS = 25000
FIXTURE_BATTENS = {"G-01": 30000, "G-02": 50000}

# 日检结论：只能由服务端根据三项检查结果判定，客户端不能指定
CONCLUSION_PASS = "PASS"
CONCLUSION_NEEDS_ATTENTION = "NEEDS_ATTENTION"
CONCLUSION_LABELS = {
    CONCLUSION_PASS: "合格",
    CONCLUSION_NEEDS_ATTENTION: "需处理",
}


def ensure_fixture_battens(db: Session) -> None:
    """保证固定夹具 G-01 / G-02 存在且核定值正确。"""
    for batten_id, capacity in FIXTURE_BATTENS.items():
        batten = db.get(Batten, batten_id)
        if batten is None:
            db.add(Batten(id=batten_id, capacity_grams=capacity))
        elif batten.capacity_grams != capacity:
            batten.capacity_grams = capacity
    db.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    # 旧数据升级：create_all 不会给已存在的表补列。若旧库的 loads 表缺少
    # removed_at，统一补上且全部为 NULL —— 老记录一律视为仍在杆上。
    with engine.begin() as conn:
        columns = {col["name"] for col in inspect(conn).get_columns("loads")}
        if "loads" in inspect(conn).get_table_names() and "removed_at" not in columns:
            conn.execute(
                text("ALTER TABLE loads ADD COLUMN removed_at TIMESTAMP WITH TIME ZONE")
            )
    with SessionLocal() as db:
        ensure_fixture_battens(db)
    yield


app = FastAPI(title="剧场吊杆配重装载裁决服务", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    # 路径中的装载编号不合法（如 /loads/abc/remove、/loads/-1/transfer）：
    # loc 落在 path/load_id 上，与请求体无关，不能套用配重标识 / 重量提示。
    # 非数字报"必须是数字"；能解析成数字但不是正整数（负数、零）报
    # "必须是正整数"——负数编号绝不能落到库里被误报为记录不存在；
    # 转移接口的请求体只有目标参数 target_batten_id：缺 body、缺字段、空串、
    # 仅含空白、非字符串等校验失败（loc 落在 body / target_batten_id 上）都说明
    # 转移目标参数不合法，同样不能套用装载接口的提示；
    # 修正接口的请求体只有新重量一个参数，同样单独给出提示
    locs = {loc for err in exc.errors() for loc in err.get("loc", ())}
    if "load_id" in locs:
        if any(
            err.get("type") == "greater_than" and "load_id" in err.get("loc", ())
            for err in exc.errors()
        ):
            message = "请求格式不合法：装载编号必须是正整数"
        else:
            message = "请求格式不合法：装载编号必须是数字"
    elif request.url.path.endswith("/transfer") and (
        "target_batten_id" in locs or "body" in locs
    ):
        message = "请求格式不合法：转移目标参数不合法，目标吊杆编号必须是非空字符串"
    elif request.url.path.endswith("/correct"):
        message = "请求格式不合法：修正重量必须是整数克数"
    elif request.url.path.endswith("/inspections"):
        # 日检接口的请求体校验失败：日期格式错误、检查项非布尔、说明超长等
        message = "请求格式不合法：营业日期必须是 YYYY-MM-DD，三项检查项必须是布尔值"
    else:
        message = "请求格式不合法：配重片标识不能为空且不得包含不可见字符，重量必须是整数克数"
    return JSONResponse(
        status_code=422,
        content={
            "accepted": False,
            "reason": "INVALID_INPUT",
            "message": message,
        },
    )


def reject(status_code: int, reason: str, message: str, **extra) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"accepted": False, "reason": reason, "message": message, **extra},
    )


def batten_summary(db: Session, batten: Batten) -> dict:
    # 容量汇总只认在杆记录（removed_at IS NULL）；已拆下的配重片不再占容量
    total = db.scalar(
        select(func.coalesce(func.sum(Load.weight_grams), 0))
        .where(Load.batten_id == batten.id)
        .where(Load.removed_at.is_(None))
    )
    return {
        "batten_id": batten.id,
        "capacity_grams": batten.capacity_grams,
        "total_grams": total,
        "remaining_grams": batten.capacity_grams - total,
    }


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/battens")
def list_battens(db: Session = Depends(get_db)):
    battens = db.scalars(select(Batten).order_by(Batten.id)).all()
    summaries = []
    for batten in battens:
        summary = batten_summary(db, batten)
        summary["load_count"] = db.scalar(
            select(func.count(Load.id))
            .where(Load.batten_id == batten.id)
            .where(Load.removed_at.is_(None))
        )
        summaries.append(summary)
    return {"battens": summaries}


@app.get("/api/battens/{batten_id}")
def get_batten(batten_id: str, db: Session = Depends(get_db)):
    batten = db.get(Batten, batten_id)
    if batten is None:
        return reject(404, "BATTEN_NOT_FOUND", f"吊杆 {batten_id} 不存在")
    summary = batten_summary(db, batten)
    # 在杆明细：只返回仍在该吊杆上的记录，已拆下的配重片不再出现在明细中
    loads = db.scalars(
        select(Load)
        .where(Load.batten_id == batten_id)
        .where(Load.removed_at.is_(None))
        .order_by(Load.id)
    ).all()
    summary["loads"] = [
        {
            "load_id": load.id,
            "piece_id": load.piece_id,
            "weight_grams": load.weight_grams,
            "created_at": load.created_at.isoformat() if load.created_at else None,
        }
        for load in loads
    ]
    return summary


@app.post("/api/battens/{batten_id}/loads", status_code=201)
def create_load(batten_id: str, payload: LoadCreate, db: Session = Depends(get_db)):
    """逐片装载的原子裁决：行级锁串行化同一吊杆上的并发请求。"""
    if not MIN_WEIGHT_GRAMS <= payload.weight_grams <= MAX_WEIGHT_GRAMS:
        return reject(
            422,
            "INVALID_WEIGHT",
            f"单片重量必须在 {MIN_WEIGHT_GRAMS}～{MAX_WEIGHT_GRAMS} 克之间，"
            f"收到 {payload.weight_grams} 克",
        )

    # SELECT ... FOR UPDATE：锁定吊杆行，并发装载在此串行排队，
    # 后到的请求在锁释放后读到最新已提交总重再裁决。
    batten = db.scalar(
        select(Batten).where(Batten.id == batten_id).with_for_update()
    )
    if batten is None:
        db.rollback()
        return reject(404, "BATTEN_NOT_FOUND", f"吊杆 {batten_id} 不存在")

    existing = db.scalar(select(Load.id).where(Load.piece_id == payload.piece_id))
    if existing is not None:
        db.rollback()  # 失败请求不落库，同时释放行锁
        return reject(
            409,
            "PIECE_ID_EXISTS",
            f"配重片标识 {payload.piece_id} 已成功登记过，不能重复使用",
        )

    summary = batten_summary(db, batten)
    new_total = summary["total_grams"] + payload.weight_grams
    if new_total > batten.capacity_grams:
        db.rollback()
        return reject(
            409,
            "OVER_CAPACITY",
            f"吊杆 {batten_id} 当前总重 {summary['total_grams']} 克，"
            f"本片 {payload.weight_grams} 克，合计 {new_total} 克 "
            f"超出核定 {batten.capacity_grams} 克",
            **summary,
        )

    load = Load(
        batten_id=batten_id,
        piece_id=payload.piece_id,
        weight_grams=payload.weight_grams,
    )
    db.add(load)
    try:
        db.commit()
    except IntegrityError:
        # 并发下同一标识落在不同吊杆上时，由数据库唯一约束兜底
        db.rollback()
        return reject(
            409,
            "PIECE_ID_EXISTS",
            f"配重片标识 {payload.piece_id} 已成功登记过，不能重复使用",
        )

    return {
        "accepted": True,
        "message": f"配重片 {payload.piece_id} 已接纳",
        "batten_id": batten_id,
        "capacity_grams": batten.capacity_grams,
        "total_grams": new_total,
        "remaining_grams": batten.capacity_grams - new_total,
        "load": {
            "load_id": load.id,
            "piece_id": load.piece_id,
            "weight_grams": load.weight_grams,
        },
    }


def _lock_battens_in_fixed_order(db: Session, batten_ids: list[str]) -> dict[str, Batten]:
    """按吊杆编号固定顺序加行锁。

    转移涉及两根吊杆：无论方向是 G-01→G-02 还是 G-02→G-01，
    都按同一顺序（编号升序）逐行 SELECT ... FOR UPDATE，
    使相反方向的并发转移在锁队列上排成一队，避免相互等待形成死锁。
    """
    locked: dict[str, Batten] = {}
    for bid in sorted(set(batten_ids)):
        locked[bid] = db.scalar(
            select(Batten).where(Batten.id == bid).with_for_update()
        )
    return locked


@app.post("/api/battens/{batten_id}/loads/{load_id}/transfer")
def transfer_load(
    batten_id: str,
    # 装载编号必须是正整数：负数 / 零在参数校验阶段 422 拒绝，
    # 不会落到库里被误报为记录不存在
    load_id: Annotated[int, Path(gt=0)],
    payload: TransferCreate,
    db: Session = Depends(get_db),
):
    """把一片已登记配重片从源吊杆转移到目标吊杆。

    不新增/删除装载记录，只更新现有记录的归属，原始重量与登记时间保留。
    源、目标两根吊杆在同一事务内以固定顺序锁定，随后确认配重片仍属于
    源杆并校验目标余量，任一条件不满足则回滚，数据库保持原归属。
    """
    target_id = payload.target_batten_id

    locked = _lock_battens_in_fixed_order(db, [batten_id, target_id])
    source = locked.get(batten_id)
    target = locked.get(target_id)
    if source is None or target is None:
        missing = batten_id if source is None else target_id
        db.rollback()
        return reject(404, "BATTEN_NOT_FOUND", f"吊杆 {missing} 不存在")

    # 同杆判定放在存在性之后：源、目标填了同一个不存在的吊杆时，
    # 应明确报告吊杆不存在，而不是误报两杆相同
    if target_id == batten_id:
        db.rollback()  # 拒绝请求同时释放行锁
        return reject(
            422,
            "SAME_BATTEN",
            f"目标吊杆不能与源吊杆 {batten_id} 相同",
            source_batten_id=batten_id,
            target_batten_id=target_id,
        )

    # 锁内再确认配重片归属与在杆状态：并发转移 / 拆下时后到者会看到已提交的新归属
    # 或拆下标记；只认在杆记录，已拆下的配重片不能再转移
    load = db.scalar(
        select(Load)
        .where(Load.id == load_id)
        .where(Load.removed_at.is_(None))
        .with_for_update()
    )
    if load is None:
        # 可能是从未登记、已被转移走、或已拆下：先看记录到底存不存在，
        # 编号从未登记过才报 LOAD_NOT_FOUND，不能与"位置已变化"混为一谈
        existing = db.get(Load, load_id)
        if existing is None:
            db.rollback()
            return reject(
                404,
                "LOAD_NOT_FOUND",
                f"装载记录 {load_id} 不存在",
                source_batten_id=batten_id,
                target_batten_id=target_id,
                load_id=load_id,
            )
        # 归属在回滚前读出，避免回滚使对象过期后再触发查询
        current_batten_id = existing.batten_id
        db.rollback()
        if current_batten_id != batten_id:
            reason_msg = (
                f"配重片当前位置已变化：已不在源吊杆 {batten_id} 上，"
                "可能已被其他终端转移"
            )
        else:
            reason_msg = (
                f"配重片当前位置已变化：已从吊杆 {batten_id} 拆下，"
                "不能再转移"
            )
        return reject(
            409,
            "POSITION_CHANGED",
            reason_msg,
            source_batten_id=batten_id,
            target_batten_id=target_id,
            load_id=load_id,
        )
    if load.batten_id != batten_id:
        db.rollback()
        return reject(
            409,
            "POSITION_CHANGED",
            f"配重片当前位置已变化：已不在源吊杆 {batten_id} 上，"
            "可能已被其他终端转移",
            source_batten_id=batten_id,
            target_batten_id=target_id,
            load_id=load_id,
        )

    target_summary = batten_summary(db, target)
    new_target_total = target_summary["total_grams"] + load.weight_grams
    if new_target_total > target.capacity_grams:
        db.rollback()
        return reject(
            409,
            "OVER_CAPACITY",
            f"目标吊杆 {target_id} 当前总重 {target_summary['total_grams']} 克，"
            f"配重片 {load.piece_id} 重 {load.weight_grams} 克，"
            f"合计 {new_target_total} 克超出核定 {target.capacity_grams} 克",
            source_batten_id=batten_id,
            target_batten_id=target_id,
            **{
                f"target_{k}": v
                for k, v in target_summary.items()
                if k != "batten_id"
            },
        )

    # 只改归属：weight_grams 与 created_at 原样保留
    load.batten_id = target_id
    db.commit()

    source_summary = batten_summary(db, source)
    target_summary = batten_summary(db, target)
    return {
        "accepted": True,
        "message": f"配重片 {load.piece_id} 已从 {batten_id} 转移至 {target_id}",
        "load_id": load.id,
        "piece_id": load.piece_id,
        "weight_grams": load.weight_grams,
        "source_batten_id": batten_id,
        "target_batten_id": target_id,
        "source": source_summary,
        "target": target_summary,
    }


@app.post("/api/battens/{batten_id}/loads/{load_id}/correct")
def correct_load_weight(
    batten_id: str,
    # 装载编号必须是正整数：负数 / 零在参数校验阶段 422 拒绝，
    # 不会落到库里被误报为记录不存在
    load_id: Annotated[int, Path(gt=0)],
    payload: WeightCorrect,
    db: Session = Depends(get_db),
):
    """修正已登记配重片的标称重量。

    只更新现有装载记录的 weight_grams，不删除、不重新登记，
    配重片标识与最初登记时间保留。与装载、转移同一套串行化：
    同一事务内先锁定路径指定的吊杆，再确认装载记录仍归属于该杆，
    随后以新旧重量差重新核算容量（恰好达到核定值允许写入）。
    任一校验失败都回滚，数据库中的原重量保持不变。
    """
    if not MIN_WEIGHT_GRAMS <= payload.weight_grams <= MAX_WEIGHT_GRAMS:
        return reject(
            422,
            "INVALID_WEIGHT",
            f"单片重量必须在 {MIN_WEIGHT_GRAMS}～{MAX_WEIGHT_GRAMS} 克之间，"
            f"收到 {payload.weight_grams} 克",
        )

    # SELECT ... FOR UPDATE：与装载、转移在同一吊杆行锁上排队，
    # 并发修正 / 转移 / 装载彼此串行，按已提交的最新总重裁决
    batten = db.scalar(
        select(Batten).where(Batten.id == batten_id).with_for_update()
    )
    if batten is None:
        db.rollback()
        return reject(404, "BATTEN_NOT_FOUND", f"吊杆 {batten_id} 不存在")

    # 锁内再确认配重片归属与在杆状态：并发转移 / 拆下提交后，后到者看到已提交的
    # 新归属或拆下标记；只认在杆记录
    load = db.scalar(
        select(Load)
        .where(Load.id == load_id)
        .where(Load.removed_at.is_(None))
        .with_for_update()
    )
    if load is None:
        # 从未登记报 LOAD_NOT_FOUND；已被转移走或已拆下报当前位置已变化
        existing = db.get(Load, load_id)
        if existing is None:
            db.rollback()
            return reject(
                404,
                "LOAD_NOT_FOUND",
                f"装载记录 {load_id} 不存在",
                batten_id=batten_id,
                load_id=load_id,
            )
        # 归属在回滚前读出，避免回滚使对象过期后再触发查询
        current_batten_id = existing.batten_id
        db.rollback()
        if current_batten_id != batten_id:
            message = (
                f"配重片当前位置已变化：已不在吊杆 {batten_id} 上，"
                "可能已被其他终端转移"
            )
        else:
            message = (
                f"配重片当前位置已变化：已从吊杆 {batten_id} 拆下，"
                "不能再修正"
            )
        return reject(
            409,
            "POSITION_CHANGED",
            message,
            batten_id=batten_id,
            load_id=load_id,
        )
    if load.batten_id != batten_id:
        db.rollback()
        return reject(
            409,
            "POSITION_CHANGED",
            f"配重片当前位置已变化：已不在吊杆 {batten_id} 上，"
            "可能已被其他终端转移",
            batten_id=batten_id,
            load_id=load_id,
        )

    summary = batten_summary(db, batten)
    # 以新旧重量差重新核算容量：新总重 = 当前总重 - 原重量 + 新重量
    new_total = summary["total_grams"] - load.weight_grams + payload.weight_grams
    if new_total > batten.capacity_grams:
        db.rollback()
        return reject(
            409,
            "OVER_CAPACITY",
            f"吊杆 {batten_id} 当前总重 {summary['total_grams']} 克，"
            f"配重片 {load.piece_id} 由 {load.weight_grams} 克修正为 "
            f"{payload.weight_grams} 克后合计 {new_total} 克 "
            f"超出核定 {batten.capacity_grams} 克",
            **summary,
        )

    # 只改重量：piece_id 与 created_at 原样保留
    previous_weight = load.weight_grams
    load.weight_grams = payload.weight_grams
    db.commit()

    return {
        "accepted": True,
        "message": (
            f"配重片 {load.piece_id} 重量已从 {previous_weight} 克"
            f"修正为 {payload.weight_grams} 克"
        ),
        "load_id": load.id,
        "piece_id": load.piece_id,
        "previous_weight_grams": previous_weight,
        "weight_grams": payload.weight_grams,
        "batten_id": batten_id,
        "capacity_grams": batten.capacity_grams,
        "total_grams": new_total,
        "remaining_grams": batten.capacity_grams - new_total,
    }


@app.post("/api/battens/{batten_id}/loads/{load_id}/remove")
def remove_load(
    batten_id: str,
    # 装载编号必须是正整数：负数 / 零在参数校验阶段 422 拒绝，
    # 不会落到库里被误报为记录不存在
    load_id: Annotated[int, Path(gt=0)],
    db: Session = Depends(get_db),
):
    """演出拆台：确认从当前吊杆明细取下一片配重。

    不删除记录，只在吊杆行锁内确认该记录仍处于在杆状态，再写入拆下时刻；
    容量随本片重量立即释放，而配重片标识、重量、归属与最初登记时间全部
    保留以备追溯。已被其他终端转移走时返回当前位置已变化；已拆下则幂等
    返回明确结果，不重复释放容量。
    """
    # SELECT ... FOR UPDATE：与装载 / 转移 / 修正在同一吊杆行锁上排队，
    # 并发拆下 / 转移 / 装载彼此串行，容量只按最新已提交状态释放一次
    batten = db.scalar(
        select(Batten).where(Batten.id == batten_id).with_for_update()
    )
    if batten is None:
        db.rollback()
        return reject(404, "BATTEN_NOT_FOUND", f"吊杆 {batten_id} 不存在")

    # 锁内取出完整记录（不过滤在杆状态），再依次裁决归属与是否已拆下
    load = db.scalar(select(Load).where(Load.id == load_id).with_for_update())
    if load is None:
        db.rollback()
        return reject(
            404,
            "LOAD_NOT_FOUND",
            f"装载记录 {load_id} 不存在",
            batten_id=batten_id,
            load_id=load_id,
        )
    if load.batten_id != batten_id:
        db.rollback()
        return reject(
            409,
            "POSITION_CHANGED",
            f"配重片当前位置已变化：已不在吊杆 {batten_id} 上，"
            "可能已被其他终端转移",
            batten_id=batten_id,
            load_id=load_id,
        )
    if load.removed_at is not None:
        # 已拆下：幂等返回明确结果，绝不二次写入、不重复释放容量。
        # 汇总仍在锁内读取，保证与本次裁决同一快照；回滚仅释放行锁。
        summary = batten_summary(db, batten)
        removed_at_iso = load.removed_at.isoformat()
        db.rollback()
        return {
            "accepted": True,
            "message": (
                f"配重片 {load.piece_id} 已处于拆下状态，无需重复拆下，"
                "容量未重复释放"
            ),
            "already_removed": True,
            "load_id": load.id,
            "piece_id": load.piece_id,
            "weight_grams": load.weight_grams,
            "released_grams": 0,
            "removed_at": removed_at_iso,
            "batten_id": batten_id,
            **summary,
        }

    # 只写拆下时刻：piece_id / weight_grams / batten_id / created_at 原样保留
    released_grams = load.weight_grams
    removed_at = datetime.now(timezone.utc)
    load.removed_at = removed_at
    db.commit()

    # 汇总只认在杆记录：本片已排除，总重立即下降、余量立即回升
    summary = batten_summary(db, batten)
    return {
        "accepted": True,
        "message": (
            f"配重片 {load.piece_id} 已确认从 {batten_id} 拆下，"
            f"释放容量 {released_grams} 克"
        ),
        "already_removed": False,
        "load_id": load.id,
        "piece_id": load.piece_id,
        "weight_grams": released_grams,
        "released_grams": released_grams,
        "removed_at": removed_at.isoformat(),
        "batten_id": batten_id,
        **summary,
    }


def inspection_payload(record: Inspection) -> dict:
    """日检记录的对外契约：结论、检查明细与记录时间全部来自数据库。"""
    return {
        "inspection_id": record.id,
        "batten_id": record.batten_id,
        "inspection_date": record.inspection_date.isoformat(),
        "brake_ok": record.brake_ok,
        "rope_ok": record.rope_ok,
        "limit_ok": record.limit_ok,
        "abnormality_note": record.abnormality_note,
        "conclusion": record.conclusion,
        "conclusion_label": CONCLUSION_LABELS[record.conclusion],
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


def _find_inspection(db: Session, batten_id: str, inspection_date: date):
    return db.scalar(
        select(Inspection)
        .where(Inspection.batten_id == batten_id)
        .where(Inspection.inspection_date == inspection_date)
    )


@app.post("/api/battens/{batten_id}/inspections", status_code=201)
def create_inspection(
    batten_id: str,
    payload: InspectionCreate,
    db: Session = Depends(get_db),
):
    """吊杆日检归档：每根吊杆同一营业日期只接纳一份记录。

    结论由服务端判定：制动器、钢丝绳、限位装置三项全部正常为"合格"，
    任一项异常为"需处理"，客户端不能指定结论。任一项异常时异常说明
    不能为空。未知吊杆、未来日期、缺少异常说明一律按错误信封拒绝；
    被拒绝的请求不落库，也不触碰配重装载 / 转移 / 修正 / 拆下数据。
    """
    batten = db.get(Batten, batten_id)
    if batten is None:
        return reject(404, "BATTEN_NOT_FOUND", f"吊杆 {batten_id} 不存在")

    # 营业日期不能是未来日期：日检只登记今天或以往的营业日
    if payload.inspection_date > date.today():
        return reject(
            422,
            "INVALID_DATE",
            f"营业日期 {payload.inspection_date.isoformat()} 是未来日期，"
            "日检只能登记今天或以往的营业日",
        )

    all_ok = payload.brake_ok and payload.rope_ok and payload.limit_ok
    note = (payload.abnormality_note or "").strip()
    if not all_ok and not note:
        return reject(
            422,
            "MISSING_ABNORMALITY_NOTE",
            "存在异常检查项时，异常说明不能为空",
        )

    # 每根吊杆同一营业日期只接纳一份：已归档则返回已完成提示，
    # 并把已归档记录一并返回，便于页面展示既有结论
    existing = _find_inspection(db, batten_id, payload.inspection_date)
    if existing is not None:
        return reject(
            409,
            "INSPECTION_EXISTS",
            f"吊杆 {batten_id} {payload.inspection_date.isoformat()} 的日检已完成，"
            "不能重复提交",
            inspection=inspection_payload(existing),
        )

    # 结论只能由服务端根据三项检查结果判定，请求体中没有结论字段
    conclusion = CONCLUSION_PASS if all_ok else CONCLUSION_NEEDS_ATTENTION
    record = Inspection(
        batten_id=batten_id,
        inspection_date=payload.inspection_date,
        brake_ok=payload.brake_ok,
        rope_ok=payload.rope_ok,
        limit_ok=payload.limit_ok,
        abnormality_note=note or None,
        conclusion=conclusion,
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        # 并发下同一吊杆同一营业日期的重复提交，由数据库唯一约束兜底
        db.rollback()
        existing = _find_inspection(db, batten_id, payload.inspection_date)
        return reject(
            409,
            "INSPECTION_EXISTS",
            f"吊杆 {batten_id} {payload.inspection_date.isoformat()} 的日检已完成，"
            "不能重复提交",
            inspection=inspection_payload(existing) if existing else None,
        )
    db.refresh(record)

    return {
        "accepted": True,
        "message": (
            f"吊杆 {batten_id} {payload.inspection_date.isoformat()} 日检已归档："
            f"{CONCLUSION_LABELS[conclusion]}"
        ),
        "inspection": inspection_payload(record),
    }


@app.get("/api/battens/{batten_id}/inspections")
def list_inspections(
    batten_id: str,
    limit: int = 10,
    db: Session = Depends(get_db),
):
    """按吊杆查看最近日检记录：按营业日期倒序，全部来自数据库。"""
    batten = db.get(Batten, batten_id)
    if batten is None:
        return reject(404, "BATTEN_NOT_FOUND", f"吊杆 {batten_id} 不存在")
    records = db.scalars(
        select(Inspection)
        .where(Inspection.batten_id == batten_id)
        .order_by(Inspection.inspection_date.desc(), Inspection.id.desc())
        .limit(min(max(limit, 1), 50))
    ).all()
    return {
        "batten_id": batten_id,
        "inspections": [inspection_payload(record) for record in records],
    }


@app.post("/api/reset")
def reset(db: Session = Depends(get_db)):
    """恢复验收场景：清空全部装载记录与日检记录，复位两根空吊杆。"""
    db.execute(delete(Load))
    db.execute(delete(Inspection))
    ensure_fixture_battens(db)
    battens = db.scalars(select(Batten).order_by(Batten.id)).all()
    return {"battens": [batten_summary(db, batten) for batten in battens]}
