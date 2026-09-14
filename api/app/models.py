from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Batten(Base):
    """吊杆（固定夹具），全库仅 G-01 / G-02 两根。"""

    __tablename__ = "battens"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    capacity_grams: Mapped[int] = mapped_column(Integer, nullable=False)

    loads: Mapped[list["Load"]] = relationship(back_populates="batten")
    inspections: Mapped[list["Inspection"]] = relationship(back_populates="batten")


class Load(Base):
    """成功装载的配重片记录；被拒绝的请求永不写入此表。

    拆下（演出拆台）不删除记录：只把 removed_at 从 NULL 写为拆下时刻，
    配重片标识、重量、归属吊杆与最初登记时间全部保留以备追溯。
    removed_at IS NULL 即"在杆"，容量汇总、明细查询与转移 / 修正裁决
    只认在杆记录；历史唯一标识（含已拆下）仍被占用，不能再登记。
    """

    __tablename__ = "loads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batten_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("battens.id"), nullable=False, index=True
    )
    # 配重片标识全库唯一：同一标识只能成功登记一次（拆下后依旧占用）
    piece_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    weight_grams: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # 拆下时刻：NULL 表示仍在杆上；旧数据没有此列，补列后统一为 NULL（在杆）
    removed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    batten: Mapped[Batten] = relationship(back_populates="loads")


class Inspection(Base):
    """吊杆日检记录：按营业日期归档，每根吊杆同一营业日期只接纳一份。

    结论（合格 / 需处理）由服务端在归档时根据制动器、钢丝绳、限位装置
    三项检查结果判定并落库，客户端不能指定检查结果以外的任何结论字段。
    任一项异常时异常说明不能为空。被拒绝的请求永不写入此表，也不触碰
    配重装载相关数据。
    """

    __tablename__ = "inspections"
    __table_args__ = (
        # 数据库兜底：同一吊杆同一营业日期全库唯一，并发重复提交由约束拒绝
        UniqueConstraint(
            "batten_id", "inspection_date", name="uq_inspections_batten_date"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batten_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("battens.id"), nullable=False, index=True
    )
    # 营业日期：与吊杆一起构成唯一约束，同一吊杆同一天只接纳一份日检
    inspection_date: Mapped[date] = mapped_column(Date, nullable=False)
    brake_ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rope_ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    limit_ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # 任一项异常时必填（接口内校验）；全部正常时可为空
    abnormality_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 服务端判定结论：PASS（合格）/ NEEDS_ATTENTION（需处理）
    conclusion: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    batten: Mapped[Batten] = relationship(back_populates="inspections")
