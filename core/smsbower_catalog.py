# -*- coding: utf-8 -*-
"""SMSBower 国家/服务目录与实时价格查询。

拆成两层，因为两者取数方式完全不同：

1. **目录**（国家、服务）：平台把完整表格内嵌在公开的 API 文档页里，无需 API Key，
   但要拉一个 ~600KB 的页面再解析，所以本地缓存 6 小时。
2. **价格 / 余额**：走 handler_api 的 `getPrices` / `getBalance`，必须带 `SMS_API_KEY`。
   价格变化快，只做 60 秒短缓存，避免点一次刷新打一次平台。

价格单位：SMSBower 计价为 USD，`cost` 是单个号码的单价。
"""
from __future__ import annotations

import html as _html
import logging
import re
import threading
import time
from typing import Any

from curl_cffi.requests import Session as CurlSession

from config import IMPERSONATE
from config import codex as _cfg

logger = logging.getLogger(__name__)

# 平台把国家表/服务表内嵌在文档页 HTML 里；不同版本换过域名，按顺序回退。
_CATALOG_URLS = (
    "https://smsbower.app/api/?page=client",
    "https://smsbower.page/api/?page=client",
)

# 文档页体积大且极少变动，缓存久一点。
_CATALOG_TTL = 6 * 3600
# 价格随库存波动，短缓存即可。
_PRICE_TTL = 60
# 档位数据只在展开国家时拉取，缓存可以稍久。
_TIER_TTL = 300

_COUNTRY_RE = re.compile(
    r'\{"id":(\d+),"title":"([^"]+)","iso":"([^"]*)","prefix":(\d+),'
    r'"phone_length":\[[^\]]*\],"activate_org_code":"([^"]*)"'
)

# ISO 3166-1 alpha-2 -> 简体中文国名。平台只给英文名，用户检索/辨认不便；
# 未收录的条目回退英文名。
_CN_NAMES = {
    "AE": "阿联酋", "AF": "阿富汗", "AG": "安提瓜和巴布达", "AI": "安圭拉",
    "AL": "阿尔巴尼亚", "AM": "亚美尼亚", "AO": "安哥拉", "AR": "阿根廷",
    "AT": "奥地利", "AU": "澳大利亚", "AW": "阿鲁巴", "AZ": "阿塞拜疆",
    "BA": "波黑", "BB": "巴巴多斯", "BD": "孟加拉国", "BE": "比利时",
    "BF": "布基纳法索", "BG": "保加利亚", "BH": "巴林", "BI": "布隆迪",
    "BJ": "贝宁", "BM": "百慕大", "BN": "文莱", "BO": "玻利维亚",
    "BR": "巴西", "BS": "巴哈马", "BT": "不丹", "BW": "博茨瓦纳",
    "BY": "白俄罗斯", "BZ": "伯利兹", "CA": "加拿大", "CD": "刚果（金）",
    "CF": "中非", "CG": "刚果（布）", "CH": "瑞士", "CI": "科特迪瓦",
    "CL": "智利", "CM": "喀麦隆", "CN": "中国", "CO": "哥伦比亚",
    "CR": "哥斯达黎加", "CU": "古巴", "CV": "佛得角", "CY": "塞浦路斯",
    "CZ": "捷克", "DE": "德国", "DJ": "吉布提", "DK": "丹麦",
    "DM": "多米尼克", "DO": "多米尼加", "DZ": "阿尔及利亚", "EC": "厄瓜多尔",
    "EE": "爱沙尼亚", "EG": "埃及", "ER": "厄立特里亚", "ES": "西班牙",
    "ET": "埃塞俄比亚", "FI": "芬兰", "FJ": "斐济", "FR": "法国",
    "GA": "加蓬", "GB": "英国", "GD": "格林纳达", "GE": "格鲁吉亚",
    "GF": "法属圭亚那", "GH": "加纳", "GI": "直布罗陀", "GL": "格陵兰",
    "GM": "冈比亚", "GN": "几内亚", "GP": "瓜德罗普", "GQ": "赤道几内亚",
    "GR": "希腊", "GT": "危地马拉", "GW": "几内亚比绍", "GY": "圭亚那",
    "HK": "中国香港", "HN": "洪都拉斯", "HR": "克罗地亚", "HT": "海地",
    "HU": "匈牙利", "ID": "印度尼西亚", "IE": "爱尔兰", "IL": "以色列",
    "IN": "印度", "IQ": "伊拉克", "IR": "伊朗", "IS": "冰岛",
    "IT": "意大利", "JM": "牙买加", "JO": "约旦", "JP": "日本",
    "KE": "肯尼亚", "KG": "吉尔吉斯斯坦", "KH": "柬埔寨", "KM": "科摩罗",
    "KN": "圣基茨和尼维斯", "KR": "韩国", "KW": "科威特", "KY": "开曼群岛",
    "KZ": "哈萨克斯坦", "LA": "老挝", "LB": "黎巴嫩", "LC": "圣卢西亚",
    "LI": "列支敦士登", "LK": "斯里兰卡", "LR": "利比里亚", "LS": "莱索托",
    "LT": "立陶宛", "LU": "卢森堡", "LV": "拉脱维亚", "LY": "利比亚",
    "MA": "摩洛哥", "MC": "摩纳哥", "MD": "摩尔多瓦", "ME": "黑山",
    "MG": "马达加斯加", "MK": "北马其顿", "ML": "马里", "MM": "缅甸",
    "MN": "蒙古", "MO": "中国澳门", "MQ": "马提尼克", "MR": "毛里塔尼亚",
    "MS": "蒙特塞拉特", "MT": "马耳他", "MU": "毛里求斯", "MV": "马尔代夫",
    "MW": "马拉维", "MX": "墨西哥", "MY": "马来西亚", "MZ": "莫桑比克",
    "NA": "纳米比亚", "NC": "新喀里多尼亚", "NE": "尼日尔", "NG": "尼日利亚",
    "NI": "尼加拉瓜", "NL": "荷兰", "NO": "挪威", "NP": "尼泊尔",
    "NZ": "新西兰", "OM": "阿曼", "PA": "巴拿马", "PE": "秘鲁",
    "PG": "巴布亚新几内亚", "PH": "菲律宾", "PK": "巴基斯坦", "PL": "波兰",
    "PR": "波多黎各", "PS": "巴勒斯坦", "PT": "葡萄牙", "PY": "巴拉圭",
    "QA": "卡塔尔", "RE": "留尼汪", "RO": "罗马尼亚", "RS": "塞尔维亚",
    "RU": "俄罗斯", "RW": "卢旺达", "SA": "沙特阿拉伯", "SC": "塞舌尔",
    "SD": "苏丹", "SE": "瑞典", "SG": "新加坡", "SI": "斯洛文尼亚",
    "SK": "斯洛伐克", "SL": "塞拉利昂", "SN": "塞内加尔", "SO": "索马里",
    "SR": "苏里南", "SS": "南苏丹", "ST": "圣多美和普林西比", "SV": "萨尔瓦多",
    "SX": "荷属圣马丁", "SY": "叙利亚", "SZ": "斯威士兰", "TD": "乍得",
    "TG": "多哥", "TH": "泰国", "TJ": "塔吉克斯坦", "TL": "东帝汶",
    "TM": "土库曼斯坦", "TN": "突尼斯", "TR": "土耳其", "TT": "特立尼达和多巴哥",
    "TW": "中国台湾", "TZ": "坦桑尼亚", "UA": "乌克兰", "UG": "乌干达",
    "US": "美国", "UY": "乌拉圭", "UZ": "乌兹别克斯坦", "VC": "圣文森特和格林纳丁斯",
    "VE": "委内瑞拉", "VN": "越南", "VU": "瓦努阿图", "XK": "科索沃",
    "YE": "也门", "ZA": "南非", "ZM": "赞比亚", "ZW": "津巴布韦",
}

# 平台个别条目的 ISO 码不标准（直布罗陀给了三字母 GIB、美国虚拟号给了 UV），
# 中文名和旗帜都拿不到，这里按英文名兜底修正。
_CN_TITLE_FALLBACK = {
    "Gibraltar": ("直布罗陀", "GI"),
    "United States (virtual)": ("美国（虚拟号）", "US"),
}
_SERVICE_RE = re.compile(
    r'\{"id":(\d+),"title":"([^"]+)","sender_title":"[^"]*",'
    r'"activate_org_code":"([^"]+)","slug":"[^"]*","sms_pattern":[^,]*,"is_active":(\d)'
)

_lock = threading.Lock()
_catalog_cache: dict[str, Any] = {"data": None, "at": 0.0}
_price_cache: dict[str, dict[str, Any]] = {}


class SmsBowerError(RuntimeError):
    """SMSBower 查询失败。"""


def _flag(iso: str) -> str:
    """把 ISO 3166 两位代码转成旗帜 emoji；不合法时返回空串。"""
    code = str(iso or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in code)


def _api_base() -> str:
    return (
        str(getattr(_cfg, "SMSBOWER_API_BASE", "") or "").strip()
        or "https://smsbower.page/stubs/handler_api.php"
    )


def _api_key() -> str:
    """SMSBower 专属 Key 优先，没填回退共用的 SMS_API_KEY。"""
    dedicated = str(getattr(_cfg, "SMSBOWER_API_KEY", "") or "").strip()
    if dedicated:
        return dedicated
    return str(getattr(_cfg, "SMS_API_KEY", "") or "").strip()


def _fetch(url: str, params: dict | None = None, timeout: int = 30) -> Any:
    session = CurlSession(impersonate=IMPERSONATE)
    session.timeout = timeout
    try:
        return session.get(url, params=params)
    finally:
        try:
            session.close()
        except Exception:
            pass


def _load_catalog(force: bool = False) -> dict[str, Any]:
    """拉取并解析国家表 / 服务表。"""
    with _lock:
        cached = _catalog_cache["data"]
        if not force and cached and (time.time() - _catalog_cache["at"]) < _CATALOG_TTL:
            return cached

    last_error = ""
    for url in _CATALOG_URLS:
        try:
            resp = _fetch(url)
            if resp.status_code != 200:
                last_error = f"HTTP {resp.status_code}"
                continue
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue

        page = _html.unescape(resp.text or "")

        countries = []
        for cid, title, iso, prefix, code in _COUNTRY_RE.findall(page):
            if not code:
                continue
            iso_norm = iso.strip().upper()
            name_zh = _CN_NAMES.get(iso_norm, "")
            if not name_zh and title in _CN_TITLE_FALLBACK:
                name_zh, iso_norm = _CN_TITLE_FALLBACK[title]
            countries.append({
                "id": int(cid),
                "code": code,
                "name": title,
                "name_zh": name_zh,
                "iso": iso_norm,
                "prefix": prefix,
                "flag": _flag(iso_norm),
            })

        services = []
        for sid, title, code, active in _SERVICE_RE.findall(page):
            if active != "1":
                continue
            services.append({"id": int(sid), "code": code, "name": title})

        if not countries:
            last_error = "页面解析出 0 个国家（页面结构可能已变）"
            continue

        # 按国家名排序，前端下拉直接用。
        countries.sort(key=lambda item: item["name_zh"] or item["name"].lower())
        services.sort(key=lambda item: item["name"].lower())

        data = {
            "countries": countries,
            "services": services,
            "source_url": url,
            "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with _lock:
            _catalog_cache["data"] = data
            _catalog_cache["at"] = time.time()
        logger.info("[SMSBower] 目录已更新：%s 个国家 / %s 个服务", len(countries), len(services))
        return data

    raise SmsBowerError(f"无法获取 SMSBower 国家/服务目录：{last_error}")


def catalog(force: bool = False) -> dict[str, Any]:
    return _load_catalog(force=force)


def prices(service: str = "dr", force: bool = False) -> dict[str, Any]:
    """查询某个服务在各国的单价与库存。

    返回 {"countries": {国家代码: {"cost": float, "count": int}}, ...}。
    没有 API Key 时不报错，而是返回 available=False，让前端提示去配 Key。
    """
    service = str(service or "dr").strip() or "dr"
    key = _api_key()
    if not key:
        return {
            "available": False,
            "reason": "missing_key",
            "message": "未配置 SMSBower API Key，只能显示国家列表，无法查询实时价格与库存",
            "countries": {},
        }

    if not force:
        with _lock:
            cached = _price_cache.get(service)
        if cached and (time.time() - cached["at"]) < _PRICE_TTL:
            return cached["data"]

    try:
        resp = _fetch(_api_base(), {
            "api_key": key,
            "action": "getPrices",
            "service": service,
        })
    except Exception as exc:
        return {
            "available": False,
            "reason": "network",
            "message": f"请求价格失败：{type(exc).__name__}: {exc}",
            "countries": {},
        }

    text = (resp.text or "").strip()
    compact = text.replace(" ", "").lower()
    if resp.status_code in (401, 403) or '"status":0' in compact or "no access" in compact:
        return {
            "available": False,
            "reason": "bad_key",
            "message": "SMSBower 拒绝了 API Key（无效 / 过期 / 未实名）",
            "countries": {},
        }
    if text == "BAD_KEY":
        return {"available": False, "reason": "bad_key", "message": "API Key 无效（BAD_KEY）", "countries": {}}
    if text == "BAD_SERVICE":
        return {"available": False, "reason": "bad_service",
                "message": f"平台不认这个服务代码：{service}（OpenAI 应为 dr）", "countries": {}}

    try:
        payload = resp.json()
    except Exception:
        return {"available": False, "reason": "bad_response",
                "message": f"价格响应不是 JSON：{text[:160]}", "countries": {}}

    if not isinstance(payload, dict):
        return {"available": False, "reason": "bad_response",
                "message": f"价格响应格式异常：{str(payload)[:160]}", "countries": {}}

    # 文档格式：{国家代码: {服务代码: {cost, count}}}
    countries: dict[str, dict[str, Any]] = {}
    for country_code, by_service in payload.items():
        if not isinstance(by_service, dict):
            continue
        entry = by_service.get(service)
        if not isinstance(entry, dict):
            # 某些版本不带 service 维度，直接就是 {cost, count}
            entry = by_service if "cost" in by_service else None
        if not isinstance(entry, dict):
            continue
        try:
            cost = float(entry.get("cost") or 0)
        except (TypeError, ValueError):
            continue
        try:
            count = int(entry.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if cost <= 0:
            continue
        countries[str(country_code)] = {"cost": cost, "count": count, "cost_text": f"{cost:.4f}".rstrip("0").rstrip(".")}

    data = {
        "available": True,
        "service": service,
        "countries": countries,
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with _lock:
        _price_cache[service] = {"data": data, "at": time.time()}
    return data


def price_tiers(country: str, service: str = "dr", force: bool = False) -> dict:
    """查询单个国家内的价格档位（getPricesV3，按供应商拆分）。

    同一国家不同供应商报价不同（如越南从 $0.054 到 $0.33 共 10 档）；
    前端展开国家行时按需调用。返回按价格升序的档位列表。
    """
    country = str(country or "").strip()
    service = str(service or "dr").strip() or "dr"
    if not country:
        return {"available": False, "reason": "bad_args", "message": "缺少 country", "tiers": []}

    key = _api_key()
    if not key:
        return {"available": False, "reason": "missing_key",
                "message": "未配置 SMSBower API Key，无法查询价格档位", "tiers": []}

    cache_key = f"tiers:{service}:{country}"
    if not force:
        with _lock:
            cached = _price_cache.get(cache_key)
        if cached and (time.time() - cached["at"]) < _TIER_TTL:
            return cached["data"]

    try:
        resp = _fetch(_api_base(), {
            "api_key": key,
            "action": "getPricesV3",
            "service": service,
            "country": country,
        })
    except Exception as exc:
        return {"available": False, "reason": "network",
                "message": f"请求价格档位失败：{type(exc).__name__}: {exc}", "tiers": []}

    text = (resp.text or "").strip()
    compact = text.replace(" ", "").lower()
    if resp.status_code in (401, 403) or '"status":0' in compact or "no access" in compact:
        return {"available": False, "reason": "bad_key",
                "message": "SMSBower 拒绝了 API Key", "tiers": []}
    try:
        payload = resp.json()
    except Exception:
        return {"available": False, "reason": "bad_response",
                "message": f"价格档位响应不是 JSON：{text[:160]}", "tiers": []}

    # 文档格式：{国家代码: {服务代码: {供应商ID: {count, price, provider_id}}}}
    by_service = payload.get(country) if isinstance(payload, dict) else None
    providers = by_service.get(service) if isinstance(by_service, dict) else None
    if providers is None and isinstance(by_service, dict) and len(by_service) == 1:
        # 极少数版本会省一层服务维度
        providers = next(iter(by_service.values()))
    if not isinstance(providers, dict):
        return {"available": False, "reason": "empty",
                "message": f"该国在服务 {service} 下没有可用档位", "tiers": []}

    tiers = []
    for pid, info in providers.items():
        if not isinstance(info, dict):
            continue
        try:
            price = float(info.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        try:
            count = int(info.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        tiers.append({
            "provider_id": int(info.get("provider_id") or pid or 0),
            "price": price,
            "price_text": f"{price:.4f}".rstrip("0").rstrip("."),
            "count": count,
        })
    tiers.sort(key=lambda t: (t["price"], -t["count"]))

    data = {"available": True, "country": country, "service": service, "tiers": tiers}
    with _lock:
        _price_cache[cache_key] = {"data": data, "at": time.time()}
    return data


def balance() -> dict[str, Any]:
    """查询账户余额。"""
    key = _api_key()
    if not key:
        return {"available": False, "reason": "missing_key", "message": "未配置 SMSBower API Key"}
    try:
        resp = _fetch(_api_base(), {"api_key": key, "action": "getBalance"})
    except Exception as exc:
        return {"available": False, "reason": "network", "message": f"{type(exc).__name__}: {exc}"}

    text = (resp.text or "").strip()
    # 成功格式：ACCESS_BALANCE:12.34
    if text.startswith("ACCESS_BALANCE:"):
        raw = text.split(":", 1)[1].strip()
        try:
            return {"available": True, "amount": float(raw), "currency": "USD"}
        except ValueError:
            return {"available": False, "reason": "bad_response", "message": f"余额格式异常：{raw}"}
    if text == "BAD_KEY":
        return {"available": False, "reason": "bad_key", "message": "API Key 无效（BAD_KEY）"}
    compact = text.replace(" ", "").lower()
    if resp.status_code in (401, 403) or '"status":0' in compact or "no access" in compact:
        return {"available": False, "reason": "bad_key", "message": "SMSBower 拒绝了 API Key（无效 / 过期 / 未实名）"}
    return {"available": False, "reason": "bad_response", "message": f"余额响应异常：{text[:160]}"}


def overview(service: str = "dr", force: bool = False) -> dict[str, Any]:
    """把目录、价格、余额合成一份给前端的完整数据。"""
    result: dict[str, Any] = {"ok": True}

    try:
        cat = catalog()
        result["countries"] = cat["countries"]
        result["services"] = cat["services"]
        result["catalog_fetched_at"] = cat["fetched_at"]
    except SmsBowerError as exc:
        result["ok"] = False
        result["countries"] = []
        result["services"] = []
        result["catalog_error"] = str(exc)
        return result

    price_data = prices(service=service, force=force)
    result["prices_available"] = bool(price_data.get("available"))
    result["prices_message"] = price_data.get("message", "")
    result["prices"] = price_data.get("countries", {})
    result["prices_fetched_at"] = price_data.get("fetched_at", "")

    # 把价格并进国家条目，前端一次遍历就能渲染。
    merged = []
    for item in result["countries"]:
        price = result["prices"].get(item["code"])
        merged.append({
            **item,
            "cost": price["cost"] if price else None,
            "cost_text": price["cost_text"] if price else "",
            "stock": price["count"] if price else None,
        })
    # 有报价且库存 > 0 的排前面，按价格升序；其余按名称。
    merged.sort(key=lambda row: (
        row["cost"] is None or not row["stock"],
        row["cost"] if row["cost"] is not None else 0,
        row["name_zh"] or row["name"].lower(),
    ))
    result["countries"] = merged
    return result