"""Plan recommendation. Live plan data comes from GET /v1/me."""
from __future__ import annotations

from .liara import parse

# Liara hardware tiers (docs.liara.ir/paas/details/plans/hardware-plans)
TIER_FA = {0.5: "زمین", 1: "مریخ", 2: "مشتری", 4: "زحل", 8: "اورانوس", 16: "نپتون", 32: "پلوتون"}

STATIC_PLATFORMS = {"static", "react", "vue", "angular"}
HEAVY_PLATFORMS = {"laravel", "django", "next", "dotnet"}


def needed_ram_gb(platform: str, daily_visitors: int, has_database_work: bool) -> float:
    if platform in STATIC_PLATFORMS:
        base = 0.5
    elif platform in HEAVY_PLATFORMS:
        base = 1.0
    else:
        base = 0.5
    if daily_visitors > 50_000:
        base *= 4
    elif daily_visitors > 10_000:
        base *= 2
    if has_database_work and platform not in STATIC_PLATFORMS:
        base = max(base, 1.0)
    return base


def recommend(me: dict, platform: str, daily_visitors: int = 500, has_database_work: bool = False,
              feature_bundle: str | None = None) -> dict:
    all_plans = parse.plans(me)
    candidates = parse.deployable_plans(all_plans) or [p for p in all_plans if p["available"]]
    need = needed_ram_gb(platform, daily_visitors, has_database_work)
    fitting = [p for p in candidates if (p["ram_gb"] or 0) >= need and p["id"] != "free"]
    pick = fitting[0] if fitting else (candidates[-1] if candidates else None)
    bundle = feature_bundle or ("standard" if daily_visitors > 10_000 else "basic")
    offered = list(((pick or {}).get("bundles") or {}).keys())
    if offered and bundle not in offered:  # only bundles Liara marks as available for this plan
        bundle = "basic" if "basic" in offered else offered[0]
    reasons = [
        f"پلتفرم «{platform}» با حدود {daily_visitors:,} بازدید روزانه حداقل {need:g}GB رم نیاز دارد",
    ]
    if has_database_work:
        reasons.append("کار با دیتابیس/کش حافظه بیشتری مصرف می‌کند")
    if bundle == "basic":
        reasons.append("بسته ویژگی برنزی (basic) برای شروع کافی است؛ لاگ‌های طولانی‌تر و امکانات بیشتر "
                       "در بسته‌های standard/pro است")
    return {
        "recommended": pick and {
            **pick,
            "tier_fa": TIER_FA.get(pick["ram_gb"] or 0, ""),
            "bundle_plan_id": bundle,
            "bundle_monthly_toman": (pick.get("bundles") or {}).get(bundle),
            "available_bundles": offered,
        },
        "needed_ram_gb": need,
        "reasons": reasons,
        "alternatives": [{"id": p["id"], "ram_gb": p["ram_gb"], "cpu_cores": p["cpu_cores"],
                          "monthly_toman": p["monthly_toman"]} for p in candidates[:8]],
        "note": "قیمت‌ها از GET /v1/me خوانده می‌شوند و ممکن است با کنسول کمی تفاوت داشته باشند.",
    }
