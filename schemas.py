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
    auto_delete_seconds: int | None = Field(
        default=None,
        ge=10,
        le=604800,
        description="حذف الرسالة تلقائياً بعد هذه المدة بالثواني",
    )


class PrivateMessageRequest(BaseModel):
    user_ids: list[int] = Field(
        ...,
        min_length=1,
        max_length=100,
        description="معرّفات المستخدمين المستهدفين (1–100)",
    )
    message_html: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        description="نص الرسالة بصيغة HTML المدعومة في تيليغرام",
    )
    auto_delete_seconds: int | None = Field(
        default=None,
        ge=10,
        le=604800,
        description="حذف الرسالة تلقائياً بعد هذه المدة بالثواني",
    )


class LaunchTimedAnnouncementRequest(BaseModel):
    message_html: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        description="نص الإعلان بصيغة HTML المدعومة في تيليغرام",
    )
    ends_at: str = Field(
        ...,
        min_length=1,
        max_length=40,
        description="وقت انتهاء الإعلان (ISO 8601)",
    )
    auto_delete_seconds: int | None = Field(
        default=None,
        ge=10,
        le=604800,
        description="حذف كل رسالة إعلان تلقائياً بعد هذه المدة من لحظة إرسالها",
    )


class RejectWithdrawalRequest(BaseModel):
    reason: str | None = Field(
        default=None,
        max_length=500,
        description="سبب الرفض (يُرسل للمستخدم عبر تيليغرام)",
    )


class RejectManualOrderRequest(BaseModel):
    reason: str | None = Field(
        default=None,
        max_length=500,
        description="سبب الرفض (يُرسل للمستخدم عبر تيليغرام)",
    )


class SendManualOrderNotifyRequest(BaseModel):
    message: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="نص الرسالة للعميل (صاحب الطلب) عبر تيليغرام",
    )


class CreateProviderRequest(BaseModel):
    slug: str = Field(..., min_length=1, max_length=50)
    name: str = Field(..., min_length=1, max_length=200)
    api_base_url: str = Field(..., min_length=1, max_length=500)
    adapter_type: str = Field(default="gozibra_v2", max_length=50)
    is_active: bool = Field(default=True)


class PatchProviderRequest(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    api_base_url: str | None = Field(default=None, max_length=500)
    adapter_type: str | None = Field(default=None, max_length=50)
    is_active: bool | None = Field(default=None)


class CreateProviderAccountRequest(BaseModel):
    account_key: str = Field(..., min_length=1, max_length=50)
    api_key_env: str = Field(..., min_length=1, max_length=100)
    display_name: str | None = Field(default=None, max_length=200)
    is_active: bool = Field(default=True)


class PatchProviderAccountRequest(BaseModel):
    api_key_env: str | None = Field(default=None, max_length=100)
    display_name: str | None = Field(default=None, max_length=200)
    is_active: bool | None = Field(default=None)
