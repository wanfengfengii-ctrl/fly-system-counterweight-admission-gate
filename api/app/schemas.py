from pydantic import BaseModel, Field


class LoadCreate(BaseModel):
    piece_id: str = Field(min_length=1, max_length=128)
    weight_grams: int
