import os
import json
import time
import requests
from datetime import datetime, timedelta, timezone

# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  수집 대상 필터 (★여기만 바꾸면 대상이 달라짐★)                              ║
# ╠══════════════════════════════════════════════════════════════════════════╣
# ║  MEDIA_TAG     : 광고명에 반드시 포함돼야 하는 미디어 태그 ("F_V" = 영상)   ║
# ║  NAME_INCLUDES : 광고명에 '모두' 포함돼야 하는 키워드 리스트                ║
# ║    - 지금      : ["BT"]        → F_V + 광고명에 BT 포함                     ║
# ║    - F_V 전체  : []            → 빈 리스트면 추가 키워드 필터 없음          ║
# ║    - 다른 코드 : ["PN"] 등으로 교체                                         ║
# ╚══════════════════════════════════════════════════════════════════════════╝
MEDIA_TAG     = "F_V"
NAME_INCLUDES = ["BT"]

# ── 설정 ────────────────────────────────────────────────────────────────────
ACCESS_TOKEN  = os.environ["META_ACCESS_TOKEN"]
AD_ACCOUNT_ID = os.environ["META_AD_ACCOUNT_ID"]
MONTH_TAG     = os.environ.get("MONTH_TAG", "")
DATE_START    = os.environ.get("DATE_START", "")
DATE_STOP     = os.environ.get("DATE_STOP", "")
SLACK_BOT_TOKEN  = os.environ.get("SLACK_BOT_TOKEN", "")   # 새 불씨 슬랙 알림 (Bot Token, 없으면 알림 생략)
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "")  # 알림 보낼 채널 ID
SITE_URL      = os.environ.get("SITE_URL", "https://kimsj1-web.github.io/ad-archive-video/")

API_VERSION  = "v19.0"
BASE_URL     = f"https://graph.facebook.com/{API_VERSION}"
ARCHIVE_FILE = "archive.json"
IMAGES_DIR   = "images"  # 썸네일/영상 저장 폴더

# ── 수집/불씨 공통 기준 ───────────────────────────────────────────────────────
# "조회당 비용 10원 이하" 또는 "일 광고비 150만원 이상" 중 하나라도 충족하면 수집.
#   - 아카이브 탭: '일 광고비' = 일 평균 광고비(총지출 ÷ 집행일수)
#   - 불씨 탭    : '일 광고비' = 일 최고 광고비(하루 최고 지출)
CPV_THRESHOLD        = 10          # 조회당 비용(원) 이하
DAILY_SPEND_THRESHOLD = 1_500_000  # 일 광고비(원) 이상

def qualifies(cost_per_view, daily_spend, total_views):
    """수집/불씨 기준 충족 여부.
    - daily_spend: 호출하는 쪽이 '일 평균'(아카이브) 또는 '일 최고'(불씨)를 넘겨줌
    - 조회수가 0이면 조회당비용 계산 불가 → 일광고비 기준만 적용"""
    cpv_ok   = (total_views > 0) and (cost_per_view <= CPV_THRESHOLD)
    spend_ok = daily_spend >= DAILY_SPEND_THRESHOLD
    return cpv_ok or spend_ok

# ── 성과 등급 기준 (총 광고비 기준) ──────────────────────────────────────────
def get_grade(total_spend):
    if total_spend >= 10_000_000:
        return "SS급"
    elif total_spend >= 5_000_000:
        return "S급"
    elif total_spend >= 3_000_000:
        return "A급"
    elif total_spend >= 1_000_000:
        return "B급"
    return "C급"   # 기준(cpv/일광고비)으로 들어왔지만 총액은 낮은 경우도 보여줌

def get_grade_color(grade):
    return {
        "SS급": "#BF5AF2", "S급": "#FF4B4B", "A급": "#FF9500",
        "B급": "#34C759", "C급": "#8E8E93",
    }.get(grade, "#8E8E93")

GRADE_ORDER = {"SS급": 0, "S급": 1, "A급": 2, "B급": 3, "C급": 4}

def month_index(tag):
    """'YY.MM' → 정수 월 인덱스 (비교용)."""
    yy, mm = tag.split(".")
    return (2000 + int(yy)) * 12 + (int(mm) - 1)

# 영상 로컬 보관 정책: 월 태그가 이 개월수 이상 지나면 로컬 영상 대신 메타 링크로 연결 + 파일 삭제
VIDEO_LOCAL_MONTHS = 1

def is_old_month(month_tag):
    """월 태그가 현재(KST) 기준 VIDEO_LOCAL_MONTHS 이상 지났으면 True (→ 메타 링크)."""
    try:
        KST = timezone(timedelta(hours=9))
        now = datetime.now(KST)
        cur = now.year * 12 + (now.month - 1)
        return (cur - month_index(month_tag)) >= VIDEO_LOCAL_MONTHS
    except Exception:
        return False

# ── API 헬퍼 ─────────────────────────────────────────────────────────────────
RATE_LIMIT_CODES    = {4, 17, 32, 613}          # 앱/유저/페이지 레이트 리밋
RATE_LIMIT_SUBCODES = {2446079, 1487742, 1015}  # "User request limit reached" 등

def api_get(url, params, _retries=5):
    """메타 Graph API GET. 레이트 리밋(code 17 등)·일시 오류는 잠시 쉬었다가 자동 재시도."""
    params["access_token"] = ACCESS_TOKEN
    delay_rate = 60   # 레이트 리밋 대기(초), 재시도마다 증가
    delay_tmp  = 5    # 일시 오류 대기(초)
    resp = None
    for attempt in range(_retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=60)
        except requests.exceptions.RequestException as e:
            if attempt < _retries:
                print(f"  ⏳ 네트워크 오류 → {delay_tmp}s 후 재시도 ({attempt+1}/{_retries}): {e}")
                time.sleep(delay_tmp); delay_tmp = min(delay_tmp * 2, 60); continue
            raise
        if resp.status_code == 200:
            return resp.json()

        err = {}
        try:
            err = resp.json().get("error", {})
        except Exception:
            pass
        code, subcode = err.get("code"), err.get("error_subcode")
        rate_limited = (code in RATE_LIMIT_CODES) or (subcode in RATE_LIMIT_SUBCODES)
        transient    = err.get("is_transient", False) or resp.status_code in (500, 502, 503, 504)

        if (rate_limited or transient) and attempt < _retries:
            wait = delay_rate if rate_limited else delay_tmp
            kind = "레이트 리밋" if rate_limited else "일시 오류"
            print(f"  ⏳ {kind}(code {code}) → {wait}s 대기 후 재시도 ({attempt+1}/{_retries})")
            time.sleep(wait)
            if rate_limited:
                delay_rate = min(delay_rate + 60, 300)
            else:
                delay_tmp = min(delay_tmp * 2, 60)
            continue

        print(f"  ⚠️  API 오류: {resp.status_code} | 응답: {resp.text[:300]}")
        resp.raise_for_status()
        return resp.json()

    print(f"  ⚠️  재시도 소진 → 마지막 응답: {resp.text[:300] if resp is not None else 'N/A'}")
    resp.raise_for_status()

# ── 영상 액션 배열 파싱 헬퍼 ──────────────────────────────────────────────────
def _sum_action(rows_field, action_type=None):
    """[{action_type, value}, ...] 형태 필드의 value 합산.
    action_type이 None이면 전체 합산(단일 action 필드용: video_play_actions 등)."""
    total = 0.0
    if not rows_field:
        return 0.0
    for a in rows_field:
        if action_type is None or a.get("action_type") == action_type:
            try:
                total += float(a.get("value", 0))
            except (TypeError, ValueError):
                pass
    return total

def extract_video_metrics(row):
    """단일 insights row(또는 합산 대상)에서 영상 지표 원자료 추출.
    반환: dict(spend, plays, views3s, p25, clicks, link_clicks, purchases)
    - plays   : video_play_actions (재생 시작)
    - views3s : actions 내 video_view (3초 조회)
    - p25     : video_p25_watched_actions (25% 도달)
    """
    spend       = float(row.get("spend", 0) or 0)
    plays       = _sum_action(row.get("video_play_actions"))
    views3s     = _sum_action(row.get("actions"), "video_view")
    p25         = _sum_action(row.get("video_p25_watched_actions"))
    clicks      = float(row.get("clicks", 0) or 0)
    link_clicks = float(row.get("inline_link_clicks", 0) or 0)
    purchases   = _sum_action(row.get("actions"), "purchase")
    return {
        "spend": spend, "plays": plays, "views3s": views3s, "p25": p25,
        "clicks": clicks, "link_clicks": link_clicks, "purchases": purchases,
    }

# 영상 insights 요청에 쓰는 공통 필드
VIDEO_FIELDS = (
    "spend,clicks,inline_link_clicks,actions,"
    "video_play_actions,video_p25_watched_actions"
)

# ── 조회 기간 계산 ────────────────────────────────────────────────────────────
def get_time_range():
    if DATE_START and DATE_STOP:
        return DATE_START, DATE_STOP
    if MONTH_TAG:
        yy, mm = MONTH_TAG.split(".")
        year, month = int("20" + yy), int(mm)
        start = datetime(year, month, 1)
        today = datetime.today().replace(hour=0, minute=0, second=0, microsecond=0)
        return start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")
    today = datetime.today()
    return (today - timedelta(days=7)).strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")

# ── 광고명 필터 ───────────────────────────────────────────────────────────────
def name_matches(name):
    """MEDIA_TAG + NAME_INCLUDES 전부 포함하는 광고명만 True."""
    if MEDIA_TAG and MEDIA_TAG not in name:
        return False
    for kw in NAME_INCLUDES:
        if kw not in name:
            return False
    return True

# ── 광고 목록 조회 (월 아카이브용) ────────────────────────────────────────────
def fetch_ads(month_tag):
    """MEDIA_TAG + 월 태그 + NAME_INCLUDES 포함 광고 목록."""
    url = f"{BASE_URL}/{AD_ACCOUNT_ID}/ads"
    filtering = [
        {"field": "name", "operator": "CONTAIN", "value": MEDIA_TAG},
        {"field": "name", "operator": "CONTAIN", "value": month_tag},
    ]
    for kw in NAME_INCLUDES:
        filtering.append({"field": "name", "operator": "CONTAIN", "value": kw})
    params = {
        "fields": "id,name,creative{id}",
        "filtering": json.dumps(filtering),
        "limit": 500,
    }
    all_ads = []
    while True:
        data = api_get(url, params)
        all_ads.extend(data.get("data", []))
        next_url = data.get("paging", {}).get("next")
        if not next_url:
            break
        url, params = next_url, {}
    return all_ads

# ── 크리에이티브 조회 (영상 썸네일/소스) ──────────────────────────────────────
def fetch_creative_media(creative_id):
    """반환: {"image_url": 썸네일, "video_url": 영상 소스, "video_permalink": 메타 페이지}"""
    result = {"image_url": "", "video_url": "", "video_permalink": ""}
    if not creative_id:
        return result
    try:
        url = f"{BASE_URL}/{creative_id}"
        data = api_get(url, {"fields": "thumbnail_url,image_url,object_story_spec"})
        spec = data.get("object_story_spec", {})
        result["image_url"] = data.get("thumbnail_url", "") or \
                              spec.get("video_data", {}).get("image_url", "")
        video_id = spec.get("video_data", {}).get("video_id", "")
        if video_id:
            video_info = fetch_video_source(video_id)
            result["video_url"] = video_info["source"]
            result["video_permalink"] = video_info["permalink"]
            if not result["image_url"]:
                result["image_url"] = fetch_video_thumbnail(video_id)
    except Exception as e:
        print(f"    크리에이티브 조회 실패 (creative {creative_id}): {e}")
    return result

def fetch_video_source(video_id):
    """영상 다운로드 URL + 메타 영상 페이지 URL 반환"""
    try:
        data = api_get(f"{BASE_URL}/{video_id}", {"fields": "source,permalink_url"})
        return {
            "source": data.get("source", ""),
            "permalink": f"https://www.facebook.com{data['permalink_url']}" if data.get("permalink_url") else ""
        }
    except Exception as e:
        print(f"    영상 소스 조회 실패 (video {video_id}): {e}")
        return {"source": "", "permalink": ""}

def fetch_video_thumbnail(video_id):
    try:
        data = api_get(f"{BASE_URL}/{video_id}/thumbnails", {})
        thumbs = data.get("data", [])
        for t in thumbs:
            if t.get("is_preferred"):
                return t.get("uri", "")
        return thumbs[0].get("uri", "") if thumbs else ""
    except Exception as e:
        print(f"    영상 썸네일 조회 실패 (video {video_id}): {e}")
        return ""

# ── 미디어 파일 다운로드 & 저장 ──────────────────────────────────────────────
def download_media(url, filename, skip_if_exists=False):
    if not url:
        return ""
    os.makedirs(IMAGES_DIR, exist_ok=True)
    filepath = os.path.join(IMAGES_DIR, filename)
    if skip_if_exists and os.path.exists(filepath) and os.path.getsize(filepath) > 0:
        return filepath
    try:
        resp = requests.get(url, timeout=30, stream=True)
        resp.raise_for_status()
        with open(filepath, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        print(f"    💾 저장: {filename}")
        return filepath
    except Exception as e:
        print(f"    ⚠️  다운로드 실패 ({filename}): {e}")
        return ""

def safe_filename(ad_name, suffix, ext):
    import re
    clean = re.sub(r'[\/:*?"<>|]', '_', ad_name)[:80]
    return f"{clean}_{suffix}.{ext}"

# ── 인사이트 조회 (날짜별 — 집행일수/피크 계산용) ─────────────────────────────
def fetch_insights(ad_id, date_start, date_stop):
    """단일 광고의 일자별 영상 인사이트 → 집계값 반환."""
    url = f"{BASE_URL}/{ad_id}/insights"
    params = {
        "fields": VIDEO_FIELDS,
        "time_range": json.dumps({"since": date_start, "until": date_stop}),
        "time_increment": 1,
    }
    try:
        rows = api_get(url, params).get("data", [])
        if not rows:
            return None

        active_rows = [r for r in rows if float(r.get("spend", 0) or 0) > 0]
        if not active_rows:
            return None

        active_days = len(active_rows)
        agg = {"spend": 0.0, "plays": 0.0, "views3s": 0.0, "p25": 0.0,
               "clicks": 0.0, "link_clicks": 0.0, "purchases": 0.0}
        date_spend = {}
        for r in rows:
            m = extract_video_metrics(r)
            for k in agg:
                agg[k] += m[k]
            dt = r.get("date_start", "")
            if m["spend"] > 0:
                date_spend[dt] = date_spend.get(dt, 0.0) + m["spend"]

        total_spend = agg["spend"]
        daily_spend = total_spend / active_days if active_days else 0
        peak_daily  = max(date_spend.values()) if date_spend else 0

        # 파생 지표
        cost_per_view = total_spend / agg["views3s"] if agg["views3s"] else 0  # 조회당 비용(3초)
        play3_rate    = (agg["views3s"] / agg["plays"] * 100) if agg["plays"] else 0  # 3초 재생율
        p25_rate      = (agg["p25"] / agg["plays"] * 100) if agg["plays"] else 0      # 25% 재생율
        cpc           = total_spend / agg["clicks"] if agg["clicks"] else 0

        return {
            "total_spend":   total_spend,
            "daily_spend":   daily_spend,
            "peak_daily":    peak_daily,
            "active_days":   active_days,
            "active_dates":  [r.get("date_start", "") for r in active_rows],
            "plays":         agg["plays"],
            "views3s":       agg["views3s"],
            "p25":           agg["p25"],
            "clicks":        agg["clicks"],
            "link_clicks":   agg["link_clicks"],
            "purchases":     agg["purchases"],
            "cost_per_view": cost_per_view,
            "play3_rate":    play3_rate,
            "p25_rate":      p25_rate,
            "cpc":           cpc,
        }
    except Exception as e:
        print(f"    인사이트 조회 실패: {e}")
        return None

# ── 일광고비(불씨) 수집 ───────────────────────────────────────────────────────
def fetch_account_active_ads(date_start, date_stop):
    """최근 기간에 '지출이 있었던' 광고 목록만 가볍게 조회."""
    url = f"{BASE_URL}/{AD_ACCOUNT_ID}/insights"
    params = {
        "level": "ad",
        "fields": "ad_id,ad_name,spend",
        "time_range": json.dumps({"since": date_start, "until": date_stop}),
        "limit": 500,
    }
    rows = []
    while True:
        data = api_get(url, params)
        rows.extend(data.get("data", []))
        next_url = data.get("paging", {}).get("next")
        if not next_url:
            break
        url, params = next_url, {}
    return rows

def fetch_ad_daily_rows(ad_id, date_start, date_stop):
    """단일 광고의 일자별 영상 인사이트 rows."""
    url = f"{BASE_URL}/{ad_id}/insights"
    params = {
        "fields": VIDEO_FIELDS,
        "time_range": json.dumps({"since": date_start, "until": date_stop}),
        "time_increment": 1,
    }
    try:
        return api_get(url, params).get("data", [])
    except Exception as e:
        print(f"    일자별 인사이트 실패 (ad {ad_id}): {e}")
        return []

def fetch_ad_creative_id(ad_id):
    try:
        data = api_get(f"{BASE_URL}/{ad_id}", {"fields": "creative{id}"})
        return data.get("creative", {}).get("id", "")
    except Exception as e:
        print(f"    크리에이티브 ID 조회 실패 (ad {ad_id}): {e}")
        return ""

def collect_daily_spikes(date_start, date_stop):
    """최근 7일 집행된 F_V+BT 영상 중 '불씨 기준' 충족분 반환.
    불씨 기준 = 조회당 비용 10원 이하  또는  일 평균 광고비 150만원 이상
    (등급/성냥불 구분 없이, 기준 충족하면 전부 수집)"""
    print(f"🔥 불씨 스캔: {date_start} ~ {date_stop} ({MEDIA_TAG}+{'/'.join(NAME_INCLUDES) or '전체'} / 최근 7일)")

    fire_seen = load_fire_seen()
    today_kst = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d")

    try:
        active = fetch_account_active_ads(date_start, date_stop)
    except Exception as e:
        print(f"  ⚠️  광고 목록 조회 실패 → 불씨 탭은 이번엔 건너뜀: {e}")
        return []

    name_to_ids = {}
    for r in active:
        name = r.get("ad_name", "")
        if not name_matches(name):
            continue
        if float(r.get("spend", 0) or 0) <= 0:
            continue
        name_to_ids.setdefault(name, []).append(r.get("ad_id", ""))

    print(f"  → 대상 집행 광고 {len(name_to_ids)}개 → 광고별 일자 조회")

    results = []
    for name, ad_ids in name_to_ids.items():
        date_spend = {}
        agg = {"spend": 0.0, "plays": 0.0, "views3s": 0.0, "p25": 0.0,
               "clicks": 0.0, "link_clicks": 0.0, "purchases": 0.0}
        for ad_id in ad_ids:
            for d in fetch_ad_daily_rows(ad_id, date_start, date_stop):
                m = extract_video_metrics(d)
                dt = d.get("date_start", "")
                date_spend[dt] = date_spend.get(dt, 0.0) + m["spend"]
                for k in agg:
                    agg[k] += m[k]

        if not date_spend:
            continue
        peak_daily    = max(date_spend.values())
        active_days   = sum(1 for v in date_spend.values() if v > 0)
        daily_spend   = agg["spend"] / active_days if active_days else 0  # 일 평균 광고비(표시용)
        cost_per_view = agg["spend"] / agg["views3s"] if agg["views3s"] else 0
        play3_rate    = (agg["views3s"] / agg["plays"] * 100) if agg["plays"] else 0
        p25_rate      = (agg["p25"] / agg["plays"] * 100) if agg["plays"] else 0
        cpc           = agg["spend"] / agg["clicks"] if agg["clicks"] else 0

        # ── 불씨 기준 판정 (조회당 10원 이하 OR 일 최고 광고비 150만 이상) ──
        if not qualifies(cost_per_view, peak_daily, agg["views3s"]):
            continue
        peak_date = max(date_spend, key=date_spend.get)

        print(f"  🔥 불씨 | {name[:42]} | 조회당 {cost_per_view:.1f}원 / 일최고 {peak_daily:,.0f}원 / 일평균 {daily_spend:,.0f}원")

        creative_id = fetch_ad_creative_id(ad_ids[0])
        media = fetch_creative_media(creative_id)
        img_filename = safe_filename(name, "daily", "jpg")
        local_img = download_media(media["image_url"], img_filename)
        local_video = ""
        if media.get("video_url"):
            vid_filename = safe_filename(name, "daily", "mp4")
            local_video = download_media(media["video_url"], vid_filename)

        # 최초 발견일 기록 → 오늘 처음 불씨로 잡혔으면 NEW
        rec = fire_seen.get(name)
        if isinstance(rec, str):
            rec = {"불씨": rec}
        if not isinstance(rec, dict):
            rec = {}
        first_seen = rec.get("불씨")
        if not first_seen:
            first_seen = today_kst
            rec["불씨"] = today_kst
        fire_seen[name] = rec
        is_new = (first_seen == today_kst)

        results.append({
            "name":             name,
            "peak_daily_spend": peak_daily,
            "daily_spend":      daily_spend,
            "peak_date":        peak_date,
            "total_spend":      agg["spend"],
            "cost_per_view":    cost_per_view,
            "play3_rate":       play3_rate,
            "p25_rate":         p25_rate,
            "cpc":              cpc,
            "views3s":          agg["views3s"],
            "active_days":      active_days,
            "image_url":        local_img if local_img else media["image_url"],
            "is_video":         True,
            "video_url":        local_video if local_video else media.get("video_url", ""),
            "video_permalink":  media.get("video_permalink", ""),
            "first_seen":       first_seen,
            "is_new":           is_new,
        })

    save_fire_seen(fire_seen)

    # 정렬: NEW 먼저 → 일광고비 높은 순
    results.sort(key=lambda x: (not x["is_new"], -x["daily_spend"]))
    new_cnt = sum(1 for r in results if r["is_new"])
    print(f"  → 불씨 {len(results)}개 (오늘 NEW {new_cnt}개)")
    return results

# ── 순위(월별/주별) 집계 ──────────────────────────────────────────────────────
def _rank_entry(name, acc):
    spend = acc["spend"]
    cpv   = spend / acc["views3s"] if acc["views3s"] else 0
    p3    = (acc["views3s"] / acc["plays"] * 100) if acc["plays"] else 0
    p25r  = (acc["p25"] / acc["plays"] * 100) if acc["plays"] else 0
    return {
        "name":          name,
        "spend":         round(spend),
        "cost_per_view": round(cpv, 1),
        "play3_rate":    round(p3, 1),
        "p25_rate":      round(p25r, 1),
        "views3s":       round(acc["views3s"]),
        "product":       get_product(name),
    }

def _rank_visible_names(ads):
    """모든 정렬×제품군 조합의 TOP 20에 한 번이라도 등장하는 광고명 집합."""
    # 총광고비·3초재생율·25%재생율은 높을수록 위, 조회당비용은 낮을수록 위
    DESC = {"spend": True, "play3_rate": True, "p25_rate": True, "cost_per_view": False}
    names = set()
    for metric, desc in DESC.items():
        for pf in ("all", "빙과", "제과"):
            pool = [a for a in ads if (pf == "all" or a["product"] == pf)]
            pool.sort(key=lambda a: a[metric], reverse=desc)
            for a in pool[:20]:
                names.add(a["name"])
    return names

def collect_rankings():
    """이번 달(1일~오늘)·이번 주(월~오늘) F_V+BT 영상 성과를 광고명 단위로 집계."""
    today = datetime.today()
    m_start = today.replace(day=1).strftime("%Y-%m-%d")
    m_stop  = today.strftime("%Y-%m-%d")
    monday  = today - timedelta(days=today.weekday())
    w_start = monday.strftime("%Y-%m-%d")
    w_stop  = today.strftime("%Y-%m-%d")
    print(f"🏆 순위 집계: 월간 {m_start}~{m_stop} / 주간 {w_start}~{w_stop}")

    try:
        active = fetch_account_active_ads(m_start, m_stop)
    except Exception as e:
        print(f"  ⚠️  순위용 광고 목록 조회 실패 → 순위 탭 건너뜀: {e}")
        return {"month": {"label": f"{m_start} ~ {m_stop}", "ads": []},
                "week":  {"label": f"{w_start} ~ {w_stop}", "ads": []}}

    name_to_ids = {}
    for r in active:
        name = r.get("ad_name", "")
        if not name_matches(name):
            continue
        if float(r.get("spend", 0) or 0) <= 0:
            continue
        name_to_ids.setdefault(name, []).append(r.get("ad_id", ""))

    print(f"  → 대상 광고 {len(name_to_ids)}개 (월간 일자 조회 중)")
    month_ads, week_ads = [], []
    for name, ad_ids in name_to_ids.items():
        m = {"spend": 0.0, "plays": 0.0, "views3s": 0.0, "p25": 0.0}
        w = {"spend": 0.0, "plays": 0.0, "views3s": 0.0, "p25": 0.0}
        for ad_id in ad_ids:
            for d in fetch_ad_daily_rows(ad_id, m_start, m_stop):
                em = extract_video_metrics(d)
                dt = d.get("date_start", "")
                for key, acc in (("m", m),):
                    pass
                m["spend"] += em["spend"]; m["plays"] += em["plays"]
                m["views3s"] += em["views3s"]; m["p25"] += em["p25"]
                if w_start <= dt <= w_stop:
                    w["spend"] += em["spend"]; w["plays"] += em["plays"]
                    w["views3s"] += em["views3s"]; w["p25"] += em["p25"]
        if m["spend"] > 0:
            month_ads.append(_rank_entry(name, m))
        if w["spend"] > 0:
            week_ads.append(_rank_entry(name, w))

    print(f"  → 월간 {len(month_ads)}개 / 주간 {len(week_ads)}개 집계 완료")

    visible = _rank_visible_names(month_ads) | _rank_visible_names(week_ads)
    print(f"  → 순위 등장 광고 {len(visible)}개 썸네일 수집")
    media_map = {}
    for name in visible:
        ad_ids = name_to_ids.get(name, [])
        if not ad_ids:
            continue
        creative_id = fetch_ad_creative_id(ad_ids[0])
        media = fetch_creative_media(creative_id)
        local_img = download_media(media["image_url"], safe_filename(name, "rank", "jpg"))
        local_video = ""
        if media.get("video_url"):
            local_video = download_media(media["video_url"], safe_filename(name, "rank", "mp4"), skip_if_exists=True)
        media_map[name] = {
            "image_url":       local_img if local_img else media["image_url"],
            "is_video":        True,
            "video_url":       local_video if local_video else media.get("video_url", ""),
            "video_permalink": media.get("video_permalink", ""),
        }

    for lst in (month_ads, week_ads):
        for a in lst:
            mm = media_map.get(a["name"], {})
            a["image_url"]       = mm.get("image_url", "")
            a["is_video"]        = mm.get("is_video", True)
            a["video_url"]       = mm.get("video_url", "")
            a["video_permalink"] = mm.get("video_permalink", "")

    return {
        "month": {"label": f"{m_start} ~ {m_stop}", "ads": month_ads},
        "week":  {"label": f"{w_start} ~ {w_stop}", "ads": week_ads},
    }

# ── 누적 아카이브 로드/저장 ───────────────────────────────────────────────────
def load_archive():
    if os.path.exists(ARCHIVE_FILE):
        with open(ARCHIVE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_archive(archive):
    with open(ARCHIVE_FILE, "w", encoding="utf-8") as f:
        json.dump(archive, f, ensure_ascii=False, indent=2)

TABS_CACHE = "tabs_cache.json"

def load_tabs_cache():
    if os.path.exists(TABS_CACHE):
        try:
            with open(TABS_CACHE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_tabs_cache(daily_ads, d_start, d_stop, rankings):
    try:
        with open(TABS_CACHE, "w", encoding="utf-8") as f:
            json.dump({"daily_ads": daily_ads, "daily_start": d_start,
                       "daily_stop": d_stop, "rankings": rankings}, f, ensure_ascii=False)
    except Exception as e:
        print(f"  ⚠️  탭 캐시 저장 실패: {e}")

FIRE_SEEN_FILE = "fire_seen.json"

def load_fire_seen():
    if os.path.exists(FIRE_SEEN_FILE):
        try:
            with open(FIRE_SEEN_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_fire_seen(seen):
    try:
        with open(FIRE_SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(seen, f, ensure_ascii=False)
    except Exception as e:
        print(f"  ⚠️  불씨 발굴이력 저장 실패: {e}")

ALERT_SENT_FILE = "alert_sent.json"

def load_alert_sent():
    if os.path.exists(ALERT_SENT_FILE):
        try:
            with open(ALERT_SENT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_alert_sent(sent):
    try:
        with open(ALERT_SENT_FILE, "w", encoding="utf-8") as f:
            json.dump(sent, f, ensure_ascii=False)
    except Exception as e:
        print(f"  ⚠️  알림 이력 저장 실패: {e}")

# ── 병합 (같은 광고명은 최신 수집값으로 교체 = upsert) ───────────────────────
def merge_archive(existing, new_results, period_label):
    existing_map = {ad["name"]: ad for ad in existing}
    for new_ad in new_results:
        name = new_ad["name"]
        old = existing_map.get(name)
        periods = list(old.get("periods", [])) if old else []
        if period_label not in periods:
            periods.append(period_label)
        new_ad["periods"] = periods
        if old:
            for k in ("image_url", "video_url", "video_permalink"):
                if not new_ad.get(k) and old.get(k):
                    new_ad[k] = old[k]
        existing_map[name] = new_ad
    merged = list(existing_map.values())
    merged.sort(key=lambda x: (GRADE_ORDER.get(x.get("grade", "C급"), 99), -x.get("total_spend", 0)))
    return merged

# ── HTML 생성 ────────────────────────────────────────────────────────────────
def get_product(name):
    if "빙과" in name:
        return "빙과"
    if "제과" in name:
        return "제과"
    return "기타"

def build_html(ads_data, daily_ads=None, daily_start="", daily_stop="", rankings=None):
    KST = timezone(timedelta(hours=9))
    updated = datetime.now(KST).strftime("%Y-%m-%d %H:%M (KST)")
    all_periods = sorted({p for ad in ads_data for p in ad.get("periods", [])})
    periods_str = " · ".join(all_periods) if all_periods else "전체"
    period_options = '<option value="all">전체</option>' + "".join(
        f'<option value="{p}">{p}</option>'
        for p in sorted(all_periods, key=month_index, reverse=True)
    )
    filter_desc = f"{MEDIA_TAG}" + (f" · 광고명 {'/'.join(NAME_INCLUDES)} 포함" if NAME_INCLUDES else " 전체")

    # ── 아카이브 카드 ──
    cards_html = ""
    for ad in ads_data:
        grade_color = get_grade_color(ad["grade"])
        img = ad.get("image_url", "")
        video = ad.get("video_url", "")
        video_permalink = ad.get("video_permalink", "")

        img_tag = (f'<img src="{img}" alt="광고 미디어" onerror="this.style.display=\'none\'">'
                   if img else '<div class="no-img">미리보기 없음</div>')

        play_overlay = ""
        card_click_attr = ""
        card_classes = "card"
        periods = ad.get("periods", [])
        old_video = periods and is_old_month(max(periods, key=month_index))
        if old_video and video_permalink:
            play_overlay = '<div class="play-icon">▶</div>'
            card_click_attr = f' data-permalink="{video_permalink}"'
            card_classes += " has-permalink"
        elif video:
            play_overlay = '<div class="play-icon">▶</div>'
            card_click_attr = f' data-video="{video}"'
            card_classes += " has-video"
        elif video_permalink:
            play_overlay = '<div class="play-icon">▶</div>'
            card_click_attr = f' data-permalink="{video_permalink}"'
            card_classes += " has-permalink"

        periods_tag = " ".join(f'<span class="period-tag">{p}</span>' for p in ad.get("periods", []))
        periods_attr = " ".join(ad.get("periods", []))
        product = get_product(ad["name"])

        cards_html += f"""
        <div class="{card_classes}" data-grade="{ad['grade']}" data-product="{product}" data-periods="{periods_attr}" data-name="{ad['name']}"{card_click_attr}>
            <div class="card-img">
                {img_tag}
                {play_overlay}
                <span class="grade-badge" style="background:{grade_color}">{ad['grade']}</span>
            </div>
            <div class="card-body">
                <p class="ad-name">{ad['name']}</p>
                <div class="metrics">
                    <div class="metric"><span class="label">총 광고비</span><span class="value">{ad.get('total_spend', 0):,.0f}원</span></div>
                    <div class="metric"><span class="label">일 평균 광고비</span><span class="value">{ad['daily_spend']:,.0f}원</span></div>
                    <div class="metric"><span class="label">조회당 비용</span><span class="value">{ad['cost_per_view']:,.1f}원</span></div>
                    <div class="metric"><span class="label">3초 재생율</span><span class="value">{ad['play3_rate']:.1f}%</span></div>
                    <div class="metric"><span class="label">25% 재생율</span><span class="value">{ad['p25_rate']:.1f}%</span></div>
                    <div class="metric"><span class="label">3초 조회수</span><span class="value">{ad['views3s']:,.0f}</span></div>
                    <div class="metric"><span class="label">집행일수</span><span class="value">{int(ad.get('active_days', 0))}일</span></div>
                </div>
                <div class="periods">{periods_tag}</div>
            </div>
        </div>"""

    # ── 일광고비 카드 ──
    daily_ads = daily_ads or []

    def _daily_card(ad):
        img = ad.get("image_url", "")
        video = ad.get("video_url", "")
        video_permalink = ad.get("video_permalink", "")
        img_tag = (f'<img src="{img}" alt="광고 미디어" onerror="this.style.display=\'none\'">'
                   if img else '<div class="no-img">미리보기 없음</div>')
        play_overlay = ""
        card_click_attr = ""
        card_classes = "fire-card"
        if video:
            play_overlay = '<div class="play-icon">▶</div>'
            card_click_attr = f' data-video="{video}"'
            card_classes += " has-video"
        elif video_permalink:
            play_overlay = '<div class="play-icon">▶</div>'
            card_click_attr = f' data-permalink="{video_permalink}"'
            card_classes += " has-permalink"

        product = get_product(ad["name"])
        new_badge = '<span class="new-badge">NEW!</span>' if ad.get("is_new") else ''

        return f"""
        <div class="{card_classes}" data-product="{product}"{card_click_attr}>
            <div class="card-img">
                {img_tag}
                {play_overlay}
                <span class="match-badge">🔥 불씨</span>
                {new_badge}
            </div>
            <div class="card-body">
                <p class="ad-name">{ad['name']}</p>
                <div class="metrics">
                    <div class="metric"><span class="label">총 광고비 (7일)</span><span class="value">{ad['total_spend']:,.0f}원</span></div>
                    <div class="metric"><span class="label">일 평균 광고비</span><span class="value peak">{ad['daily_spend']:,.0f}원</span></div>
                    <div class="metric"><span class="label">조회당 비용</span><span class="value">{ad['cost_per_view']:,.1f}원</span></div>
                    <div class="metric"><span class="label">3초 재생율</span><span class="value">{ad['play3_rate']:.1f}%</span></div>
                    <div class="metric"><span class="label">25% 재생율</span><span class="value">{ad['p25_rate']:.1f}%</span></div>
                    <div class="metric"><span class="label">일광고비 (최고)</span><span class="value">{ad['peak_daily_spend']:,.0f}원</span></div>
                </div>
            </div>
        </div>"""

    ember_cards_html = "".join(_daily_card(a) for a in daily_ads)

    rankings = rankings or {"month": {"label": "", "ads": []}, "week": {"label": "", "ads": []}}
    rank_json = json.dumps(rankings, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>영상 고효율 광고 아카이브 (BT)</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Pretendard:wght@400;500;600;700&display=swap');
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  :root {{ --bg:#0F0F12; --surface:#1A1A20; --border:#2A2A35; --text:#F0F0F5; --muted:#8A8A9A; }}
  body {{ background:var(--bg); color:var(--text); font-family:'Pretendard',-apple-system,sans-serif; min-height:100vh; }}
  header {{ padding:40px 32px 24px; border-bottom:1px solid var(--border); display:flex; align-items:flex-end; justify-content:space-between; flex-wrap:wrap; gap:16px; }}
  .header-left h1 {{ font-size:22px; font-weight:700; letter-spacing:-0.5px; }}
  .header-left p {{ margin-top:4px; font-size:13px; color:var(--muted); }}
  .updated {{ font-size:12px; color:var(--muted); }}
  .header-right {{ text-align:right; }}
  .refresh-notice {{ font-size:11px; color:#6A6A7A; margin-top:6px; line-height:1.5; }}
  .refresh-notice .kbd {{ color:var(--muted); font-weight:600; }}
  .controls {{ padding:16px 32px 0; display:flex; gap:8px; flex-wrap:wrap; align-items:center; }}
  .filter-label {{ font-size:11px; color:var(--muted); margin-right:2px; }}
  .filter-btn {{ padding:6px 16px; border-radius:20px; border:1px solid var(--border); background:transparent; color:var(--muted); font-size:13px; cursor:pointer; transition:all .15s; font-family:inherit; }}
  .filter-btn:hover {{ border-color:#555; color:var(--text); }}
  .filter-btn.active {{ background:var(--text); color:var(--bg); border-color:var(--text); font-weight:600; }}
  .divider {{ width:1px; height:20px; background:var(--border); margin:0 4px; }}
  .period-select {{ padding:6px 30px 6px 14px; border-radius:20px; border:1px solid var(--border); background:var(--surface) url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'><path d='M1 1l4 4 4-4' stroke='%238A8A9A' stroke-width='1.5' fill='none' stroke-linecap='round'/></svg>") no-repeat right 12px center; color:var(--text); font-size:13px; font-family:inherit; cursor:pointer; -webkit-appearance:none; appearance:none; }}
  .period-select:hover {{ border-color:#555; }}
  .period-select:focus {{ outline:none; border-color:var(--text); }}
  .name-search {{ padding:6px 14px; border-radius:20px; border:1px solid var(--border); background:var(--surface); color:var(--text); font-size:13px; font-family:inherit; min-width:200px; }}
  .name-search::placeholder {{ color:var(--muted); }}
  .name-search:hover {{ border-color:#555; }}
  .name-search:focus {{ outline:none; border-color:var(--text); }}
  .count {{ margin-left:auto; font-size:13px; color:var(--muted); }}
  .product-filters {{ padding:10px 32px 16px; display:flex; gap:8px; flex-wrap:wrap; align-items:center; }}
  .gallery {{ padding:8px 32px 60px; display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:20px; }}
  .card {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; overflow:hidden; transition:transform .2s,box-shadow .2s; }}
  .card:hover {{ transform:translateY(-4px); box-shadow:0 12px 32px rgba(0,0,0,.4); }}
  .card-img {{ position:relative; background:#111; overflow:hidden; display:flex; align-items:center; justify-content:center; min-height:200px; }}
  .card-img img {{ width:100%; height:auto; display:block; object-fit:contain; }}
  .no-img {{ width:100%; height:200px; display:flex; align-items:center; justify-content:center; color:var(--muted); font-size:12px; }}
  .grade-badge {{ position:absolute; top:10px; right:10px; padding:3px 10px; border-radius:20px; font-size:12px; font-weight:700; color:#fff; }}
  .new-badge {{ position:absolute; top:10px; right:54px; padding:3px 9px; border-radius:20px; font-size:11px; font-weight:800; color:#fff; background:#FF2D55; box-shadow:0 2px 6px rgba(255,45,85,.5); letter-spacing:.3px; }}
  .match-badge {{ position:absolute; top:10px; right:10px; padding:3px 10px; border-radius:20px; font-size:12px; font-weight:700; color:#fff; background:#FF6B00; }}
  .new-badge-left {{ left:10px; right:auto; }}
  .play-icon {{ position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); width:54px; height:54px; border-radius:50%; background:rgba(0,0,0,.55); border:2px solid rgba(255,255,255,.9); color:#fff; font-size:20px; display:flex; align-items:center; justify-content:center; padding-left:4px; pointer-events:none; transition:background .15s; }}
  .card.has-video, .card.has-permalink {{ cursor:pointer; }}
  .card.has-video:hover .play-icon, .card.has-permalink:hover .play-icon {{ background:rgba(0,0,0,.8); }}
  .modal {{ position:fixed; inset:0; background:rgba(0,0,0,.85); display:none; align-items:center; justify-content:center; z-index:1000; padding:24px; }}
  .modal.open {{ display:flex; }}
  .modal video {{ max-width:90vw; max-height:85vh; border-radius:8px; background:#000; }}
  .modal img {{ max-width:90vw; max-height:85vh; border-radius:8px; object-fit:contain; }}
  #modalVideo, #modalImage {{ display:none; }}
  .modal-close {{ position:absolute; top:20px; right:28px; font-size:32px; color:#fff; cursor:pointer; line-height:1; background:none; border:none; }}
  .card-body {{ padding:16px; }}
  .ad-name {{ font-size:11px; color:var(--muted); margin-bottom:12px; line-height:1.5; word-break:break-all; }}
  .metrics {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; }}
  .metric {{ display:flex; flex-direction:column; gap:2px; }}
  .metric .label {{ font-size:10px; color:var(--muted); letter-spacing:0.5px; }}
  .metric .value {{ font-size:13px; font-weight:600; display:flex; align-items:center; gap:4px; }}
  .periods {{ margin-top:10px; display:flex; flex-wrap:wrap; gap:4px; }}
  .period-tag {{ font-size:10px; color:var(--muted); border:1px solid var(--border); border-radius:4px; padding:1px 6px; }}
  .tabs {{ display:flex; gap:4px; padding:16px 32px 0; border-bottom:1px solid var(--border); flex-wrap:wrap; }}
  .tab-btn {{ padding:10px 20px; border:none; background:transparent; color:var(--muted); font-size:14px; font-weight:600; cursor:pointer; border-bottom:2px solid transparent; font-family:inherit; transition:all .15s; }}
  .tab-btn:hover {{ color:var(--text); }}
  .tab-btn.active {{ color:var(--text); border-bottom-color:var(--text); }}
  .tab-panel {{ display:none; }}
  .tab-panel.active {{ display:block; }}
  .panel-pad {{ padding:60px 32px; }}
  .section-head {{ padding:24px 32px 8px; display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; }}
  .section-title {{ font-size:18px; font-weight:700; letter-spacing:-0.3px; }}
  .section-sub {{ font-size:12px; color:var(--muted); }}
  .fire-card {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; overflow:hidden; transition:transform .2s,box-shadow .2s; }}
  .fire-card:hover {{ transform:translateY(-4px); box-shadow:0 12px 32px rgba(0,0,0,.4); }}
  .value.peak {{ color:#FF6B00; }}
  .fire-card.has-video, .fire-card.has-permalink {{ cursor:pointer; }}
  .fire-card.has-video:hover .play-icon, .fire-card.has-permalink:hover .play-icon {{ background:rgba(0,0,0,.8); }}
  .rank-table-wrap {{ padding:8px 32px 40px; overflow-x:auto; }}
  .rank-table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  .rank-table th, .rank-table td {{ padding:12px 14px; text-align:right; white-space:nowrap; border-bottom:1px solid var(--border); }}
  .rank-table th {{ color:var(--muted); font-weight:600; font-size:12px; position:sticky; top:0; background:var(--bg); }}
  .rank-table th.rank-col, .rank-table td.rank-col {{ text-align:center; width:48px; }}
  .rank-table th.name-col, .rank-table td.name-col {{ text-align:left; white-space:normal; min-width:220px; }}
  .rank-table td.rank-col {{ font-weight:700; font-size:15px; }}
  .rank-table th.active-col {{ color:var(--text); }}
  .rank-table td.active-col {{ color:#FF6B00; font-weight:700; }}
  .rank-table tbody tr:hover {{ background:var(--surface); }}
  .rank-table td.thumb {{ width:56px; padding-right:0; }}
  .rank-table tr.clickable {{ cursor:pointer; }}
  .rtw {{ position:relative; width:44px; height:44px; border-radius:8px; overflow:hidden; background:#23232B; display:flex; align-items:center; justify-content:center; }}
  .rtw img {{ width:100%; height:100%; object-fit:cover; display:block; }}
  .rtw .noimg {{ color:var(--muted); font-size:10px; }}
  .rtw .pl {{ position:absolute; right:2px; bottom:2px; width:16px; height:16px; border-radius:50%; background:rgba(0,0,0,.7); color:#fff; font-size:8px; display:flex; align-items:center; justify-content:center; }}
  .mtag {{ display:inline-block; padding:2px 8px; border-radius:6px; font-size:11px; font-weight:600; }}
  .mtag.video {{ background:rgba(191,90,242,.15); color:#BF5AF2; }}
  .empty {{ grid-column:1/-1; text-align:center; padding:80px 0; color:var(--muted); }}
</style>
</head>
<body>
<header>
  <div class="header-left">
    <h1>영상 고효율 광고 아카이브 <span style="color:#BF5AF2;">(BT)</span></h1>
    <p>대상: {filter_desc} · 누적 조회: {periods_str}</p>
  </div>
  <div class="header-right">
    <span class="updated">마지막 업데이트: {updated}</span>
    <p class="refresh-notice">업데이트 날짜가 안 바뀌었다면<br><span class="kbd">Cmd+Shift+R</span> 로 강력 새로고침 해주세요!</p>
  </div>
</header>
<div class="tabs">
  <button class="tab-btn" data-tab="rankPanel">순위</button>
  <button class="tab-btn active" data-tab="dailyPanel">일광고비</button>
  <button class="tab-btn" data-tab="archivePanel">고효율 아카이브</button>
</div>

<section class="tab-panel" id="rankPanel">
  <div class="section-head">
    <h2 class="section-title">🏆 영상 성과 순위 TOP 20</h2>
    <span class="section-sub" id="rankSub"></span>
  </div>
  <div class="product-filters">
    <span class="filter-label">기간</span>
    <button class="filter-btn rperiod-btn active" data-period="month">월별</button>
    <button class="filter-btn rperiod-btn" data-period="week">주별</button>
    <div class="divider"></div>
    <span class="filter-label">정렬</span>
    <button class="filter-btn rmetric-btn active" data-metric="spend">총 광고비</button>
    <button class="filter-btn rmetric-btn" data-metric="cost_per_view">조회당 비용</button>
    <button class="filter-btn rmetric-btn" data-metric="play3_rate">3초 재생율</button>
    <button class="filter-btn rmetric-btn" data-metric="p25_rate">25% 재생율</button>
    <div class="divider"></div>
    <span class="filter-label">제품군</span>
    <button class="filter-btn rproduct-btn active" data-product="all">전체</button>
    <button class="filter-btn rproduct-btn" data-product="빙과">빙과</button>
    <button class="filter-btn rproduct-btn" data-product="제과">제과</button>
  </div>
  <div class="rank-table-wrap">
    <table class="rank-table">
      <thead>
        <tr>
          <th class="rank-col">#</th>
          <th class="name-col" colspan="2">광고명</th>
          <th data-col="spend">총 광고비</th>
          <th data-col="cost_per_view">조회당 비용</th>
          <th data-col="play3_rate">3초 재생율</th>
          <th data-col="p25_rate">25% 재생율</th>
          <th>3초 조회수</th>
        </tr>
      </thead>
      <tbody id="rankBody"></tbody>
    </table>
    <div class="empty" id="rankEmpty" style="display:none;">해당 조건의 광고가 없습니다.</div>
  </div>
</section>

<section class="tab-panel active" id="dailyPanel">
  <div class="section-head">
    <h2 class="section-title">🔥 이번주 불씨</h2>
    <span class="section-sub">최근 7일 · {daily_start} ~ {daily_stop} · 조회당 비용 10원 이하 또는 일 광고비 150만원 이상</span>
  </div>
  <div class="product-filters">
    <span class="filter-label">제품군</span>
    <button class="filter-btn dproduct-btn active" data-product="all">전체</button>
    <button class="filter-btn dproduct-btn" data-product="빙과">빙과</button>
    <button class="filter-btn dproduct-btn" data-product="제과">제과</button>
    <span class="count" id="dailyCount"></span>
  </div>
  <div class="gallery" id="dailyGallery">
    {ember_cards_html}
  </div>
  <div class="empty" id="dailyEmpty" style="display:none;">불씨 발굴중...</div>
</section>

<section class="tab-panel" id="archivePanel">
<div class="controls">
  <span class="filter-label">등급</span>
  <button class="filter-btn grade-btn active" data-grade="all">전체</button>
  <button class="filter-btn grade-btn" data-grade="SS급">SS급</button>
  <button class="filter-btn grade-btn" data-grade="S급">S급</button>
  <button class="filter-btn grade-btn" data-grade="A급">A급</button>
  <button class="filter-btn grade-btn" data-grade="B급">B급</button>
  <button class="filter-btn grade-btn" data-grade="C급">C급</button>
  <span class="count" id="count"></span>
</div>
<div class="product-filters">
  <span class="filter-label">제품군</span>
  <button class="filter-btn product-btn active" data-product="all">전체</button>
  <button class="filter-btn product-btn" data-product="빙과">빙과</button>
  <button class="filter-btn product-btn" data-product="제과">제과</button>
  <div class="divider"></div>
  <span class="filter-label">기간</span>
  <select class="period-select" id="periodSelect">
    {period_options}
  </select>
  <div class="divider"></div>
  <span class="filter-label">검색</span>
  <input type="text" class="name-search" id="nameSearch" placeholder="광고명 검색" autocomplete="off">
</div>
<div class="gallery" id="gallery">
  {cards_html or '<div class="empty">수집 기준(조회당 비용 10원 이하 또는 일광고비 150만원 이상)을 충족하는 영상이 없습니다.</div>'}
</div>
</section>

<div class="modal" id="videoModal">
  <button class="modal-close" id="modalClose">&times;</button>
  <video id="modalVideo" controls></video>
  <img id="modalImage" alt="광고 이미지">
</div>
<script>
  const cards = [...document.querySelectorAll('#archivePanel .card')];
  const countEl = document.getElementById('count');
  let activeGrade = 'all';
  let activeProduct = 'all';
  let activePeriod = 'all';
  let searchQuery = '';

  function applyFilters() {{
    let v = 0;
    cards.forEach(c => {{
      const gradeOk = activeGrade === 'all' || c.dataset.grade === activeGrade;
      const productOk = activeProduct === 'all' || c.dataset.product === activeProduct;
      const periodOk = activePeriod === 'all' || (c.dataset.periods || '').split(' ').includes(activePeriod);
      const nameOk = !searchQuery || (c.dataset.name || '').toLowerCase().includes(searchQuery);
      const show = gradeOk && productOk && periodOk && nameOk;
      c.style.display = show ? '' : 'none';
      if (show) v++;
    }});
    countEl.textContent = v + '개';
  }}
  applyFilters();

  document.querySelectorAll('.grade-btn').forEach(btn => {{
    btn.addEventListener('click', () => {{
      document.querySelectorAll('.grade-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      activeGrade = btn.dataset.grade;
      applyFilters();
    }});
  }});
  document.querySelectorAll('.product-btn').forEach(btn => {{
    btn.addEventListener('click', () => {{
      document.querySelectorAll('.product-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      activeProduct = btn.dataset.product;
      applyFilters();
    }});
  }});
  const periodSelect = document.getElementById('periodSelect');
  if (periodSelect) periodSelect.addEventListener('change', () => {{
    activePeriod = periodSelect.value;
    applyFilters();
  }});
  const nameSearch = document.getElementById('nameSearch');
  if (nameSearch) nameSearch.addEventListener('input', () => {{
    searchQuery = nameSearch.value.trim().toLowerCase();
    applyFilters();
  }});

  // ── 영상·이미지 모달 ──
  const modal = document.getElementById('videoModal');
  const modalVideo = document.getElementById('modalVideo');
  const modalImage = document.getElementById('modalImage');
  const modalClose = document.getElementById('modalClose');
  function openVideoModal(src) {{
    if (!src) return;
    modalImage.style.display = 'none';
    modalVideo.style.display = 'block';
    modalVideo.src = src;
    modal.classList.add('open');
    modalVideo.play().catch(() => {{}});
  }}
  function openImageModal(src) {{
    if (!src) return;
    modalVideo.pause();
    modalVideo.src = '';
    modalVideo.style.display = 'none';
    modalImage.style.display = 'block';
    modalImage.src = src;
    modal.classList.add('open');
  }}
  document.querySelectorAll('#archivePanel .card.has-video, #dailyPanel .fire-card.has-video').forEach(card => {{
    card.addEventListener('click', () => openVideoModal(card.dataset.video));
  }});
  document.querySelectorAll('#archivePanel .card.has-permalink, #dailyPanel .fire-card.has-permalink').forEach(card => {{
    card.addEventListener('click', () => {{
      const url = card.dataset.permalink;
      if (url) window.open(url, '_blank');
    }});
  }});
  function closeModal() {{
    modal.classList.remove('open');
    modalVideo.pause();
    modalVideo.src = '';
    modalImage.src = '';
  }}
  modalClose.addEventListener('click', closeModal);
  modal.addEventListener('click', e => {{ if (e.target === modal) closeModal(); }});
  document.addEventListener('keydown', e => {{ if (e.key === 'Escape') closeModal(); }});

  // ── 탭 전환 ──
  document.querySelectorAll('.tab-btn').forEach(btn => {{
    btn.addEventListener('click', () => {{
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById(btn.dataset.tab).classList.add('active');
    }});
  }});

  // ── 불씨 필터 (제품군만) ──
  const fireCards  = [...document.querySelectorAll('#dailyGallery .fire-card')];
  const dailyCount = document.getElementById('dailyCount');
  const dailyEmpty = document.getElementById('dailyEmpty');
  let dProduct = 'all';
  function applyDaily() {{
    let v = 0;
    fireCards.forEach(c => {{
      const show = dProduct === 'all' || c.dataset.product === dProduct;
      c.style.display = show ? '' : 'none';
      if (show) v++;
    }});
    dailyCount.textContent = v + '개';
    dailyEmpty.style.display = v === 0 ? '' : 'none';
  }}
  document.querySelectorAll('.dproduct-btn').forEach(btn => {{
    btn.addEventListener('click', () => {{
      document.querySelectorAll('.dproduct-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      dProduct = btn.dataset.product;
      applyDaily();
    }});
  }});
  applyDaily();

  // ── 순위 탭 ──
  const RANK_DATA = {rank_json};
  const rankBody  = document.getElementById('rankBody');
  const rankSub   = document.getElementById('rankSub');
  const rankEmpty = document.getElementById('rankEmpty');
  let rPeriod = 'month', rMetric = 'spend', rProduct = 'all';
  // true=내림차순(높을수록 위), cost_per_view는 낮을수록 위
  const METRIC_DESC = {{ spend: true, play3_rate: true, p25_rate: true, cost_per_view: false }};
  const fmt = n => (n || 0).toLocaleString('ko-KR');
  function rankThumb(a) {{
    const play = a.is_video ? '<span class="pl">▶</span>' : '';
    const inner = a.image_url
      ? `<img src="${{a.image_url}}" onerror="this.style.display='none'">`
      : `<span class="noimg">${{a.product || ''}}</span>`;
    return `<div class="rtw">${{inner}}${{play}}</div>`;
  }}
  function renderRank() {{
    const src = RANK_DATA[rPeriod] || {{ label: '', ads: [] }};
    rankSub.textContent = src.label;
    let ads = (src.ads || []).filter(a =>
      (rProduct === 'all' || a.product === rProduct)
    );
    const desc = METRIC_DESC[rMetric];
    ads = ads.slice().sort((a, b) => desc ? b[rMetric] - a[rMetric] : a[rMetric] - b[rMetric]).slice(0, 20);
    document.querySelectorAll('.rank-table th[data-col]').forEach(th =>
      th.classList.toggle('active-col', th.dataset.col === rMetric)
    );
    if (!ads.length) {{ rankBody.innerHTML = ''; rankEmpty.style.display = ''; return; }}
    rankEmpty.style.display = 'none';
    rankBody.innerHTML = ads.map((a, i) => {{
      const medal = i === 0 ? '🥇' : i === 1 ? '🥈' : i === 2 ? '🥉' : (i + 1);
      const cls = m => rMetric === m ? ' class="active-col"' : '';
      let attr = '', klass = '';
      if (a.is_video && a.video_url) {{ attr = ` data-video="${{a.video_url}}"`; klass = ' class="clickable"'; }}
      else if (a.is_video && a.video_permalink) {{ attr = ` data-permalink="${{a.video_permalink}}"`; klass = ' class="clickable"'; }}
      else if (a.image_url) {{ attr = ` data-image="${{a.image_url}}"`; klass = ' class="clickable"'; }}
      return `<tr${{klass}}${{attr}}>
        <td class="rank-col">${{medal}}</td>
        <td class="thumb">${{rankThumb(a)}}</td>
        <td class="name-col">${{a.name}}<span class="mtag video">영상</span></td>
        <td${{cls('spend')}}>${{fmt(a.spend)}}원</td>
        <td${{cls('cost_per_view')}}>${{a.cost_per_view}}원</td>
        <td${{cls('play3_rate')}}>${{a.play3_rate}}%</td>
        <td${{cls('p25_rate')}}>${{a.p25_rate}}%</td>
        <td>${{fmt(a.views3s)}}</td>
      </tr>`;
    }}).join('');
  }}
  rankBody.addEventListener('click', e => {{
    const tr = e.target.closest('tr[data-video], tr[data-permalink], tr[data-image]');
    if (!tr) return;
    if (tr.dataset.video) openVideoModal(tr.dataset.video);
    else if (tr.dataset.permalink) window.open(tr.dataset.permalink, '_blank');
    else if (tr.dataset.image) openImageModal(tr.dataset.image);
  }});
  function wireRank(cls, setter) {{
    document.querySelectorAll(cls).forEach(btn => {{
      btn.addEventListener('click', () => {{
        document.querySelectorAll(cls).forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        setter(btn);
        renderRank();
      }});
    }});
  }}
  wireRank('.rperiod-btn',  b => rPeriod  = b.dataset.period);
  wireRank('.rmetric-btn',  b => rMetric  = b.dataset.metric);
  wireRank('.rproduct-btn', b => rProduct = b.dataset.product);
  renderRank();
</script>
</body>
</html>"""

# ── 아카이브 수집 ─────────────────────────────────────────────────────────────
def collect_archive(month_tag, date_start, date_stop):
    """해당 월의 F_V+BT 영상 중 수집 기준 충족분 반환."""
    ads = fetch_ads(month_tag)
    print(f"  → [{month_tag}] 대상 영상 {len(ads)}개 발견")

    # 같은 광고명이 여러 개면 합산
    raw = {}
    for ad in ads:
        ad_id, ad_name = ad["id"], ad["name"]
        creative_id = ad.get("creative", {}).get("id", "")
        metrics = fetch_insights(ad_id, date_start, date_stop)
        if not metrics:
            continue
        raw.setdefault(ad_name, []).append({"creative_id": creative_id, "metrics": metrics})

    new_results = []
    for ad_name, candidates in raw.items():
        # 합산
        agg = {"total_spend": 0.0, "plays": 0.0, "views3s": 0.0, "p25": 0.0,
               "clicks": 0.0, "peak_daily": 0.0}
        active_date_set = set()
        for c in candidates:
            m = c["metrics"]
            agg["total_spend"] += m["total_spend"]
            agg["plays"]       += m["plays"]
            agg["views3s"]     += m["views3s"]
            agg["p25"]         += m["p25"]
            agg["clicks"]      += m["clicks"]
            agg["peak_daily"]   = max(agg["peak_daily"], m["peak_daily"])
            active_date_set.update(d for d in m["active_dates"] if d)

        active_days   = len(active_date_set)
        total_spend   = agg["total_spend"]
        daily_spend   = total_spend / active_days if active_days else 0
        cost_per_view = total_spend / agg["views3s"] if agg["views3s"] else 0
        play3_rate    = (agg["views3s"] / agg["plays"] * 100) if agg["plays"] else 0
        p25_rate      = (agg["p25"] / agg["plays"] * 100) if agg["plays"] else 0
        cpc           = total_spend / agg["clicks"] if agg["clicks"] else 0

        # ── 수집 기준 판정 ──
        if not qualifies(cost_per_view, daily_spend, agg["views3s"]):
            continue

        grade = get_grade(total_spend)

        if len(candidates) > 1:
            print(f"  중복 {len(candidates)}개 합산: {ad_name[:40]} (총 {total_spend:,.0f}원 / {active_days}일)")
        print(f"    → {grade} | 총 {total_spend:,.0f}원 / 조회당 {cost_per_view:.1f}원 / 3초재생 {play3_rate:.1f}% ({active_days}일)")

        creative_id = candidates[0]["creative_id"]
        media = fetch_creative_media(creative_id)
        img_filename = safe_filename(ad_name, "thumb", "jpg")
        local_img = download_media(media["image_url"], img_filename)

        local_video = ""
        old_month = is_old_month(month_tag)
        if media["video_url"] and not old_month:
            vid_filename = safe_filename(ad_name, "video", "mp4")
            local_video = download_media(media["video_url"], vid_filename, skip_if_exists=True)
        stored_video = "" if old_month else (local_video if local_video else media["video_url"])

        new_results.append({
            "name":          ad_name,
            "total_spend":   total_spend,
            "daily_spend":   daily_spend,
            "active_days":   active_days,
            "cost_per_view": cost_per_view,
            "play3_rate":    play3_rate,
            "p25_rate":      p25_rate,
            "cpc":           cpc,
            "views3s":       agg["views3s"],
            "plays":         agg["plays"],
            "image_url":     local_img if local_img else media["image_url"],
            "video_url":     stored_video,
            "video_permalink": media.get("video_permalink", ""),
            "grade":         grade,
        })

    print(f"  → [{month_tag}] 수집 기준 충족 영상 {len(new_results)}개")
    return new_results

def expand_month_tags(spec):
    spec = (spec or "").strip()
    if not spec:
        return []
    sep = "~" if "~" in spec else ("-" if "-" in spec else None)
    if sep is None:
        return [spec]
    a, b = [s.strip() for s in spec.split(sep, 1)]
    def to_n(t):
        yy, mm = t.split(".")
        return int("20" + yy) * 12 + (int(mm) - 1)
    def to_tag(n):
        y, m = divmod(n, 12)
        return f"{y % 100:02d}.{m + 1:02d}"
    lo, hi = sorted([to_n(a), to_n(b)])
    return list(reversed([to_tag(n) for n in range(lo, hi + 1)]))

def run_archive_for_month(merged, month_tag, date_start=None, date_stop=None):
    if not (date_start and date_stop):
        yy, mm = month_tag.split(".")
        start = datetime(int("20" + yy), int(mm), 1)
        stop = datetime.today()
        date_start, date_stop = start.strftime("%Y-%m-%d"), stop.strftime("%Y-%m-%d")
    print(f"📡 [{month_tag}] 아카이브 수집 (성과 기간: {date_start} ~ {date_stop})")
    try:
        results = collect_archive(month_tag, date_start, date_stop)
        merged = merge_archive(merged, results, month_tag)
    except Exception as e:
        print(f"  ⚠️  [{month_tag}] 수집 실패(건너뜀): {e}")
    return merged

# ── 영상 정리 (용량 관리) ─────────────────────────────────────────────────────
def cleanup_videos(merged, daily_ads=None, rankings=None):
    keep = set()
    for ad in merged:
        periods = ad.get("periods", [])
        if periods and not is_old_month(max(periods, key=month_index)):
            keep.add(safe_filename(ad["name"], "video", "mp4"))
        else:
            if ad.get("video_url"):
                ad["video_url"] = ""
    for a in (daily_ads or []):
        keep.add(safe_filename(a["name"], "daily", "mp4"))
    if rankings:
        for key in ("month", "week"):
            for a in rankings.get(key, {}).get("ads", []):
                keep.add(safe_filename(a["name"], "rank", "mp4"))
    removed, freed = 0, 0
    if os.path.isdir(IMAGES_DIR):
        for fn in os.listdir(IMAGES_DIR):
            if not fn.endswith(".mp4") or fn in keep:
                continue
            fp = os.path.join(IMAGES_DIR, fn)
            try:
                freed += os.path.getsize(fp)
                os.remove(fp)
                removed += 1
            except Exception as e:
                print(f"  ⚠️  영상 삭제 실패: {fp} — {e}")
    if removed:
        print(f"  🗑️  영상 정리: {removed}개 삭제 (약 {freed/1024/1024:.1f}MB), 로컬 유지 {len(keep)}개")
    else:
        print(f"  ℹ️  삭제할 영상 없음 (로컬 유지 {len(keep)}개)")
    return merged

# ── 슬랙 알림 ─────────────────────────────────────────────────────────────────
def _slack_post(text, thread_ts=None):
    payload = {"channel": SLACK_CHANNEL_ID, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    try:
        r = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                     "Content-Type": "application/json; charset=utf-8"},
            json=payload, timeout=15,
        )
        data = r.json()
        if data.get("ok"):
            return data.get("ts")
        print(f"  ⚠️  슬랙 전송 실패: {data.get('error')}")
        return None
    except Exception as e:
        print(f"  ⚠️  슬랙 전송 오류: {e}")
        return None

def notify_slack_new_fires(daily_ads):
    if not (SLACK_BOT_TOKEN and SLACK_CHANNEL_ID):
        return
    today_kst = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d")
    alert_sent = load_alert_sent()
    new_all = sorted(
        [a for a in (daily_ads or [])
         if a.get("is_new") and alert_sent.get(a["name"]) != today_kst],
        key=lambda x: -x["daily_spend"])
    if not new_all:
        print("  ℹ️  보낼 새 불씨 없음(또는 이미 오늘 전송함) → 슬랙 알림 생략")
        return
    parent = f"🔥 *[오늘의 영상 불씨]* {today_kst}\n🔥 새 불씨 {len(new_all)}건"
    parent_ts = _slack_post(parent)
    if not parent_ts:
        print("  ⚠️  부모 메시지 전송 실패 → 상세 댓글 생략")
        return
    lines = [f"🔥 *새 불씨 발견!* ({len(new_all)}건)"]
    for a in new_all:
        lines.append(f"*{a['name']}*")
        lines.append(f"　일평균 {int(round(a['daily_spend'])):,}원 · 조회당 {a['cost_per_view']:.1f}원 · 3초재생 {a['play3_rate']:.1f}% · 25%재생 {a['p25_rate']:.1f}%")
    lines.append("")
    lines.append(f"👉 자세히 보기: {SITE_URL}")
    if _slack_post("\n".join(lines), thread_ts=parent_ts):
        print(f"  📨 슬랙 알림 전송: 불씨 {len(new_all)}건 (스레드)")
        for a in new_all:
            alert_sent[a["name"]] = today_kst
        alert_sent = {name: d for name, d in alert_sent.items() if d == today_kst}
        save_alert_sent(alert_sent)

# ── 메인 ─────────────────────────────────────────────────────────────────────
def main():
    existing = load_archive()
    print(f"기존 아카이브 {len(existing)}개")
    merged = existing

    auto_archive = os.environ.get("AUTO_ARCHIVE", "").strip().lower() in ("1", "yes", "true")

    # ── 1) 고효율 아카이브 수집 ──
    try:
        if MONTH_TAG:
            tags = expand_month_tags(MONTH_TAG)
            print(f"대상 월(최신순): {', '.join(tags)}")
            if len(tags) == 1 and DATE_START and DATE_STOP:
                merged = run_archive_for_month(merged, tags[0], DATE_START, DATE_STOP)
                save_archive(merged)
            else:
                for i, tag in enumerate(tags):
                    merged = run_archive_for_month(merged, tag)
                    save_archive(merged)
                    print(f"  → [{tag}] 저장 완료 (누적 {len(merged)}개)")
                    if i < len(tags) - 1:
                        time.sleep(30)
            print(f"  → 최종 총 {len(merged)}개")
        elif auto_archive:
            today = datetime.today()
            tags, first = [], today.replace(day=1)
            for _ in range(3):
                tags.append(first.strftime("%y.%m"))
                first = (first - timedelta(days=1)).replace(day=1)
            tags = list(reversed(tags))
            print(f"🗓️  자동 아카이브 갱신(최근 3개월): {', '.join(tags)}")
            for tag in tags:
                merged = run_archive_for_month(merged, tag)
            save_archive(merged)
            print(f"  → 병합 후 총 {len(merged)}개")
        else:
            print("ℹ️  아카이브 갱신 안 함 → 일광고비/순위만 갱신")
    except Exception as e:
        print(f"  ⚠️  아카이브 단계 오류(기존 데이터 유지하고 계속): {e}")
        save_archive(merged)

    # ── 2) 일광고비·순위 ──
    today = datetime.today()
    d_start = (today - timedelta(days=6)).strftime("%Y-%m-%d")
    d_stop  = today.strftime("%Y-%m-%d")

    if MONTH_TAG:
        print("ℹ️  수동 월 실행 → 일광고비·순위는 이전 계산값 유지(재계산 생략)")
        cache = load_tabs_cache()
        daily_ads = cache.get("daily_ads", [])
        d_start   = cache.get("daily_start", d_start)
        d_stop    = cache.get("daily_stop", d_stop)
        rankings  = cache.get("rankings")
        if not cache:
            print("  ⚠️  캐시 없음 → 일광고비·순위 탭은 다음 정기 실행 때 채워집니다")
    else:
        try:
            daily_ads = collect_daily_spikes(d_start, d_stop)
        except Exception as e:
            print(f"  ⚠️  일광고비 단계 오류(건너뜀): {e}")
            daily_ads = []
        notify_slack_new_fires(daily_ads)
        try:
            rankings = collect_rankings()
        except Exception as e:
            print(f"  ⚠️  순위 단계 오류(건너뜀): {e}")
            rankings = None
        save_tabs_cache(daily_ads, d_start, d_stop, rankings)

    # ── 2-b) 영상 정리 ──
    try:
        merged = cleanup_videos(merged, daily_ads, rankings)
        save_archive(merged)
    except Exception as e:
        print(f"  ⚠️  영상 정리 단계 오류(건너뜀): {e}")

    # ── 3) HTML 생성 ──
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(build_html(merged, daily_ads, d_start, d_stop, rankings))
    print("✅ 완료")

if __name__ == "__main__":
    main()
