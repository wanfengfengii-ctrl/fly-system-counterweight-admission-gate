from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import Base, SessionLocal, engine, get_db
from .models import Batten, Load
from .schemas import LoadCreate

MIN_WEIGHT_GRAMS = 100
MAX_WEIGHT_GRAMS = 25000
FIXTURE_BATTENS = {"G-01": 30000, "G-02": 50000}


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
async def validation_error_handler(_: Request, __: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "accepted": False,
            "reason": "INVALID_INPUT",
            "message": "请求格式不合法：配重片标识不能为空，重量必须是整数克数",
        },
    )


def reject(status_code: int, reason: str, message: str, **extra) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"accepted": False, "reason": reason, "message": message, **extra},
    )


def batten_summary(db: Session, batten: Batten) -> dict:
    total = db.scalar(
        select(func.coalesce(func.sum(Load.weight_grams), 0)).where(
            Load.batten_id == batten.id
        )
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
            select(func.count(Load.id)).where(Load.batten_id == batten.id)
        )
        summaries.append(summary)
    return {"battens": summaries}


@app.get("/api/battens/{batten_id}")
def get_batten(batten_id: str, db: Session = Depends(get_db)):
    batten = db.get(Batten, batten_id)
    if batten is None:
        return reject(404, "BATTEN_NOT_FOUND", f"吊杆 {batten_id} 不存在")
    summary = batten_summary(db, batten)
    loads = db.scalars(
        select(Load).where(Load.batten_id == batten_id).order_by(Load.id)
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


@app.post("/api/reset")
def reset(db: Session = Depends(get_db)):
    """恢复验收场景：清空全部装载记录，复位两根空吊杆。"""
    db.execute(delete(Load))
    ensure_fixture_battens(db)
    battens = db.scalars(select(Batten).order_by(Batten.id)).all()
    return {"battens": [batten_summary(db, batten) for batten in battens]}
