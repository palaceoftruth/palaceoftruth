from pydantic import BaseModel


class TagListResponse(BaseModel):
    tags: list[str]
    total: int
