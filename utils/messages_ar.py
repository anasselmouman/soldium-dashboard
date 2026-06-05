"""Arabic user-facing strings for the admin dashboard (Moroccan SMM context)."""
from __future__ import annotations

from pathlib import Path


def deposit_not_found(deposit_id: int) -> str:
    return f"الطلب #{deposit_id} غير موجود."


def deposit_not_pending(deposit_id: int, status: str) -> str:
    return f"الطلب #{deposit_id} ليس معلّقًا (الحالة: {status})."


def deposit_already_processed(deposit_id: int) -> str:
    return f"الطلب #{deposit_id} مُعالَج مسبقًا أو لم يعد معلّقًا."


def amount_must_be_positive() -> str:
    return "يجب أن يكون المبلغ بالدرهم أكبر من صفر."


def amount_exceeds_max(max_dh: float) -> str:
    return f"المبلغ يتجاوز الحد الأقصى المسموح ({max_dh} درهم)."


def amount_below_min(min_dh: float) -> str:
    return f"المبلغ أقل من الحد الأدنى لهذه الطريقة ({min_dh} درهم)."


def database_not_found(db_path: Path) -> str:
    return f"قاعدة البيانات غير موجودة: {db_path}"


DB_BUSY = "قاعدة البيانات مشغولة. انتظر لحظة ثم أعد المحاولة."

LOAD_DEPOSITS_FAILED = "تعذّر تحميل الإيداعات المعلقة."

APPROVE_FAILED = "تعذّر قبول الطلب."

REJECT_FAILED = "تعذّر رفض الطلب."

HEALTH_DB_QUERY_FAILED = "فشل الاستعلام عن قاعدة البيانات."

LOAD_USERS_FAILED = "تعذّر تحميل قائمة المستخدمين."

ADJUST_BALANCE_FAILED = "تعذّر تعديل الرصيد."

CHANGE_REFERRAL_LEVEL_FAILED = "تعذّر تغيير مستوى الإحالة."


def user_not_found(user_id: int) -> str:
    return f"المستخدم #{user_id} غير موجود."


def balance_adjust_zero() -> str:
    return "يجب أن يكون مبلغ التعديل مختلفًا عن صفر."


def balance_adjust_failed() -> str:
    return "تعذّر تعديل الرصيد (قد يكون الرصيد غير كافٍ بعد الخصم)."


def invalid_referral_level() -> str:
    return "مستوى الإحالة يجب أن يكون بين 1 و 4."


LOAD_ORDERS_FAILED = "تعذّر تحميل قائمة الطلبات."

UPDATE_ORDER_STATUS_FAILED = "تعذّر تحديث حالة الطلب."


def order_not_found(_order_id: int = 0) -> str:
    return "الطلب غير موجود."


def order_status_unchanged(_order_id: int = 0) -> str:
    return "لا يمكن تعديل حالة هذا الطلب (حالة نهائية أو غير مسموح بها)."


def order_status_update_failed() -> str:
    return "تعذّر تحديث الحالة. قد تكون الحالة الحالية نهائية أو غير متوافقة."


INVALID_LOGIN = "اسم المستخدم أو كلمة المرور غير صحيحة."

LOGIN_NOT_CONFIGURED = "لم يتم ضبط ADMIN_USERNAME و ADMIN_PASSWORD و SECRET_KEY في ملف البيئة."

LOAD_STATS_FAILED = "تعذّر تحميل الإحصائيات."

LOAD_ANALYTICS_FAILED = "تعذّر تحميل مؤشرات التحليل المالي."

BROADCAST_FAILED = "تعذّر إرسال البث الجماعي."

PRIVATE_MESSAGE_FAILED = "تعذّر إرسال الرسالة الخاصة."

NO_VALID_RECIPIENTS = "لا يوجد أي مستلم صالح من بين المعرّفات المحددة."

TIMED_ANNOUNCEMENT_FAILED = "تعذّر إطلاق الإعلان المؤقت."

STOP_TIMED_ANNOUNCEMENT_FAILED = "تعذّر إيقاف الإعلان المؤقت."

LOAD_SERVICES_CATALOG_FAILED = "تعذّر تحميل كتالوج الخدمات المحلي."

SYNC_PROVIDER_SERVICES_FAILED = "تعذّر مزامنة خدمات المزوّد."

SAVE_SERVICES_CATALOG_FAILED = "تعذّر حفظ كتالوج الخدمات."


def service_not_found(service_id: str) -> str:
    return f"الخدمة #{service_id} غير موجودة في الكتالوج."


LOAD_WITHDRAWALS_FAILED = "تعذّر تحميل طلبات السحب المعلقة."

APPROVE_WITHDRAWAL_FAILED = "تعذّر إتمام طلب السحب."

REJECT_WITHDRAWAL_FAILED = "تعذّر رفض طلب السحب."


def withdrawal_not_found(withdrawal_id: int) -> str:
    return f"طلب السحب #{withdrawal_id} غير موجود."


def withdrawal_not_pending(withdrawal_id: int, status: str) -> str:
    return f"طلب السحب #{withdrawal_id} ليس معلّقًا (الحالة: {status})."


def withdrawal_already_processed(withdrawal_id: int) -> str:
    return f"طلب السحب #{withdrawal_id} مُعالَج مسبقًا أو لم يعد معلّقًا."


LOAD_MANUAL_ORDERS_FAILED = "تعذّر تحميل طلبات التنفيذ اليدوي المعلقة."

COMPLETE_MANUAL_ORDER_FAILED = "تعذّر إتمام طلب التنفيذ اليدوي."

REJECT_MANUAL_ORDER_FAILED = "تعذّر رفض طلب التنفيذ اليدوي."

LOAD_MANUAL_ORDER_HISTORY_FAILED = "تعذّر تحميل سجل طلبات التنفيذ اليدوي."


def manual_order_not_found(_order_id: int = 0) -> str:
    return "طلب التنفيذ اليدوي غير موجود."


def manual_order_not_pending(_order_id: int = 0, status: str = "") -> str:
    status_text = f" (الحالة: {status})" if status else ""
    return f"طلب التنفيذ اليدوي ليس معلّقًا{status_text}."


def manual_order_already_processed(_order_id: int = 0) -> str:
    return "طلب التنفيذ اليدوي مُعالَج مسبقًا أو لم يعد معلّقًا."


SEND_MANUAL_ORDER_NOTIFY_FAILED = "تعذّر إرسال الإشعار للعميل."

MANUAL_ORDER_REF_UNAVAILABLE = (
    "تعذّر الحصول على رقم الطلب من الموزّد. "
    "تحقق من إعدادات API أو بيانات الطلب ثم أعد المحاولة."
)

BOT_TOKEN_NOT_CONFIGURED = "لم يتم ضبط BOT_TOKEN — تعذّر إرسال الرسائل عبر تيليغرام."


LOAD_USER_PROFILE_FAILED = "تعذّر تحميل الملف الشامل للعميل."
