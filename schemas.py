"""Request/response models for the dashboard API."""
from __future__ import annotations

from pydantic import BaseModel, Field


class ApproveDepositRequest(BaseModel):
    amount_dh: float = Field(..., gt=0, description="المبلغ النهائي المُضاف إلى الرصيد بالدرهم")


class RejectDepositRequest(BaseModel):
    reason: str | None = Field(
        default=None,
        max_length=500,
        description="سبب الرفض (اختياري — يُسجّل في السجلات فقط)",
    )


class AdjustBalanceRequest(BaseModel):
    amount_dh: float = Field(
        ...,
        description="مبلغ التعديل بالدرهم (موجب للإضافة، سالب للخصم)",
    )
    reason: str | None = Field(
        default=None,
        max_length=500,
        description="سبب التعديل (اختياري — يُسجّل في السجلات فقط)",
    )


class ChangeReferralLevelRequest(BaseModel):
    new_level: int = Field(..., ge=1, le=4, description="مستوى الإحالة الجديد (1–4)")


class UpdateOrderStatusRequest(BaseModel):
    status: str = Field(
        ...,
        min_length=1,
        max_length=50,
        description="الحالة الجديدة للطلب",
    )


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=1, max_length=200)
    remember_me: bool = Field(
        default=True,
        description="الإبقاء على تسجيل الدخول لفترة أطول عبر ملف تعريف ارتباط دائم",
    )


class PatchServiceRequest(BaseModel):
    name_ar: str | None = Field(default=None, max_length=500)
    local_price_dh: float | None = Field(default=None, ge=0)
    is_active: bool | None = Field(default=None)


class BroadcastRequest(BaseModel):
    message_html: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        description="نص الرسالة بصيغة HTML المدعومة في تيليغرام",
    )


class RejectWithdrawalRequest(BaseModel):
    reason: str | None = Field(
        default=None,
        max_length=500,
        description="سبب الرفض (يُرسل للمستخدم عبر تيليغرام)",
    )
