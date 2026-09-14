import unicodedata
from datetime import date

from pydantic import BaseModel, Field, StrictBool, StrictInt, field_validator

# 不可见字符：控制字符（换行 \n、回车 \r、制表 \t 等）、格式字符（零宽空格等）、
# 行 / 段分隔符 —— 登记后会在明细中返回异常文本，一律拒绝
_INVISIBLE_CATEGORIES = {"Cc", "Cf", "Zl", "Zp"}


def _contains_invisible_chars(value: str) -> bool:
    return any(unicodedata.category(ch) in _INVISIBLE_CATEGORIES for ch in value)


def visible_content(value: str) -> str:
    """去掉空白与不可见字符后剩下的可见内容。

    与标识校验同一套不可见字符定义（控制字符、格式字符、行/段分隔符）：
    零宽空格（U+200B）、零宽连接符、BOM 等都不算有效内容——只含这些字符的
    说明在历史里看起来为空，必须视为未填写。
    """
    return "".join(
        ch
        for ch in value
        if not ch.isspace() and unicodedata.category(ch) not in _INVISIBLE_CATEGORIES
    )


class LoadCreate(BaseModel):
    piece_id: str = Field(min_length=1, max_length=128)
    # 严格整数：字符串 "100"、小数 100.0、布尔值一律拒绝，不做隐式转换
    weight_grams: StrictInt

    @field_validator("piece_id")
    @classmethod
    def normalize_piece_id(cls, value: str) -> str:
        # 首尾空白（空格、制表、换行等）不参与标识：先规整再判定，
        # " CW-1 " 与 "CW-1" 视为同一标识，唯一性裁决按规整后的值进行
        normalized = value.strip()
        if not normalized:
            # 仅含空白的标识不是有效标识，明确拒绝
            raise ValueError("配重片标识不能为空白")
        if _contains_invisible_chars(normalized):
            raise ValueError("配重片标识不能包含换行等不可见字符")
        return normalized


class TransferCreate(BaseModel):
    # 转移目标吊杆编号；与源杆相同、不存在由接口在锁内明确拒绝
    target_batten_id: str = Field(min_length=1, max_length=32)

    @field_validator("target_batten_id")
    @classmethod
    def normalize_target_batten_id(cls, value: str) -> str:
        # 与配重片标识同一套规整：首尾空白不参与编号；仅含空白
        # （空格、制表、换行等）的目标不是合法编号，必须在参数校验阶段
        # 明确拒绝，不能落到锁内被误报为吊杆不存在
        normalized = value.strip()
        if not normalized:
            raise ValueError("目标吊杆编号不能为空白")
        return normalized


class WeightCorrect(BaseModel):
    # 修正后的单片重量：严格整数，字符串 "100"、小数 100.0 等一律拒绝
    weight_grams: StrictInt


class InspectionCreate(BaseModel):
    """日检提交：只含检查事实，结论由服务端根据三项检查结果判定。

    请求体中没有结论字段——即使客户端夹带结论字段也不会被采信，
    归档结论永远以服务端判定为准。
    """

    # 营业日期：YYYY-MM-DD；未来日期由接口在判定阶段明确拒绝
    inspection_date: date
    # 三项检查结果：严格布尔，字符串 "true"、数字 1 等一律拒绝，不做隐式转换
    brake_ok: StrictBool
    rope_ok: StrictBool
    limit_ok: StrictBool
    # 异常说明：任一项异常时必填（接口内校验，空白视为缺失）；全部正常时可为空
    abnormality_note: str | None = Field(default=None, max_length=500)
