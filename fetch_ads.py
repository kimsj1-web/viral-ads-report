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
DATE_START    = os.environ.get("DATE_START", "")  # 수동 기간 지정(선택)
DATE_STOP     = os.environ.get("DATE_STOP", "")   # 수동 기간 지정(선택)
SLACK_BOT_TOKEN  = os.environ.get("SLACK_BOT_TOKEN", "")   # 새 불씨 슬랙 알림 (Bot Token, 없으면 알림 생략)
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "")  # 알림 보낼 채널 ID
SITE_URL      = os.environ.get("SITE_URL", "https://kimsj1-web.github.io/ad-archive-video/")

API_VERSION  = "v19.0"
BASE_URL     = f"https://graph.facebook.com/{API_VERSION}"
ARCHIVE_FILE = "archive.json"
IMAGES_DIR   = "images"  # 썸네일/영상 저장 폴더

# ── 고효율 아카이브 수집 기준 ─────────────────────────────────────────────────
# [1단계] 총 광고비 150만원 이상인 광고 중
# [2단계] 조회당 비용 10원 이하  또는  일 최고 광고비 150만원 이상
ARCHIVE_DAYS          = 30          # 수집 기간: 최근 N일
MIN_TOTAL_SPEND       = 1_500_000  # [1단계] 총 광고비(원) 이상
CPV_THRESHOLD         = 10          # [2단계-a] 조회당 비용(원) 이하
PEAK_SPEND_THRESHOLD  = 1_500_000  # [2단계-b] 일 최고 광고비(원) 이상

def qualifies(total_spend, cost_per_view, peak_daily, total_views):
    """고효율 아카이브 수집 기준 충족 여부.
    1) 총 광고비 150만원 이상이어야 하고,
    2) (조회당 10원 이하) 또는 (일 최고 광고비 150만원 이상) 중 하나 충족.
    - 조회수가 0이면 조회당비용 계산 불가 → 일광고비 기준만 적용"""
    if total_spend < MIN_TOTAL_SPEND:
        return False
    cpv_ok  = (total_views > 0) and (cost_per_view <= CPV_THRESHOLD)
    peak_ok = peak_daily >= PEAK_SPEND_THRESHOLD
    return cpv_ok or peak_ok

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

# ── 광고명 필터 ───────────────────────────────────────────────────────────────
def name_matches(name):
    """MEDIA_TAG + NAME_INCLUDES 전부 포함하는 광고명만 True."""
    if MEDIA_TAG and MEDIA_TAG not in name:
        return False
    for kw in NAME_INCLUDES:
        if kw not in name:
            return False
    return True

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

# ── 고효율 아카이브 수집 (최근 N일 전수 → 기준 필터) ──────────────────────────
def collect_archive(date_start, date_stop):
    """최근 기간에 지출 있었던 F_V+BT 영상을 전수 조회해,
    '총 광고비 150만 이상 AND (조회당 10원 이하 OR 일 최고 150만 이상)' 충족분 반환.
    - 광고명 단위로 합산(같은 이름 여러 ad_id면 합침)."""
    print(f"📡 아카이브 수집: {date_start} ~ {date_stop} ({MEDIA_TAG}+{'/'.join(NAME_INCLUDES) or '전체'})")

    try:
        active = fetch_account_active_ads(date_start, date_stop)
    except Exception as e:
        print(f"  ⚠️  광고 목록 조회 실패 → 아카이브 건너뜀: {e}")
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
                if m["spend"] > 0:
                    date_spend[dt] = date_spend.get(dt, 0.0) + m["spend"]
                for k in agg:
                    agg[k] += m[k]

        if not date_spend:
            continue
        total_spend   = agg["spend"]
        peak_daily    = max(date_spend.values())
        active_days   = len(date_spend)
        daily_spend   = total_spend / active_days if active_days else 0  # 일 평균(표시용)
        cost_per_view = total_spend / agg["views3s"] if agg["views3s"] else 0
        play3_rate    = (agg["views3s"] / agg["plays"] * 100) if agg["plays"] else 0
        p25_rate      = (agg["p25"] / agg["plays"] * 100) if agg["plays"] else 0
        cpc           = total_spend / agg["clicks"] if agg["clicks"] else 0

        # ── 수집 기준 판정 (총 150만↑ AND (조회당 10원↓ OR 일최고 150만↑)) ──
        if not qualifies(total_spend, cost_per_view, peak_daily, agg["views3s"]):
            continue
        peak_date = max(date_spend, key=date_spend.get)
        grade = get_grade(total_spend)

        print(f"  🎬 {grade} | {name[:42]} | 총 {total_spend:,.0f}원 / 조회당 {cost_per_view:.1f}원 / 일최고 {peak_daily:,.0f}원 / 3초 {play3_rate:.1f}%")

        creative_id = fetch_ad_creative_id(ad_ids[0])
        media = fetch_creative_media(creative_id)
        img_filename = safe_filename(name, "thumb", "jpg")
        local_img = download_media(media["image_url"], img_filename)
        local_video = ""
        if media.get("video_url"):
            vid_filename = safe_filename(name, "video", "mp4")
            local_video = download_media(media["video_url"], vid_filename, skip_if_exists=True)

        results.append({
            "name":          name,
            "total_spend":   total_spend,
            "daily_spend":   daily_spend,
            "peak_daily":    peak_daily,
            "peak_date":     peak_date,
            "active_days":   active_days,
            "cost_per_view": cost_per_view,
            "play3_rate":    play3_rate,
            "p25_rate":      p25_rate,
            "cpc":           cpc,
            "views3s":       agg["views3s"],
            "plays":         agg["plays"],
            "image_url":     local_img if local_img else media["image_url"],
            "video_url":     local_video if local_video else media.get("video_url", ""),
            "video_permalink": media.get("video_permalink", ""),
            "grade":         grade,
        })

    # 총 광고비 높은 순 기본 정렬
    results.sort(key=lambda x: -x["total_spend"])
    print(f"  → 수집 기준 충족 영상 {len(results)}개")
    return results

# ── 누적 아카이브 로드/저장 ───────────────────────────────────────────────────
def load_archive():
    if os.path.exists(ARCHIVE_FILE):
        with open(ARCHIVE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_archive(archive):
    with open(ARCHIVE_FILE, "w", encoding="utf-8") as f:
        json.dump(archive, f, ensure_ascii=False, indent=2)

# ── 병합 (같은 광고명은 최신 수집값으로 교체 = upsert) ───────────────────────
def merge_archive(existing, new_results):
    """같은 광고명은 최신 값으로 교체. 이번에 수집 안 된 기존 광고는 그대로 보존.
    first_seen(최초 수집일)은 보존해서 '신규 여부' 판단에 쓸 수 있게 한다."""
    today_kst = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d")
    existing_map = {ad["name"]: ad for ad in existing}
    for new_ad in new_results:
        name = new_ad["name"]
        old = existing_map.get(name)
        # 최초 수집일 보존
        new_ad["first_seen"] = (old or {}).get("first_seen", today_kst)
        new_ad["last_seen"]  = today_kst
        # 미디어가 이번에 비면 기존 값 유지
        if old:
            for k in ("image_url", "video_url", "video_permalink"):
                if not new_ad.get(k) and old.get(k):
                    new_ad[k] = old[k]
        existing_map[name] = new_ad
    merged = list(existing_map.values())
    merged.sort(key=lambda x: (GRADE_ORDER.get(x.get("grade", "C급"), 99), -x.get("total_spend", 0)))
    return merged

# ── 영상 정리 (용량 관리): 아카이브에 없는 .mp4 삭제 ─────────────────────────
def cleanup_videos(merged):
    keep = set()
    for ad in merged:
        keep.add(safe_filename(ad["name"], "video", "mp4"))
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
        print(f"  🗑️  영상 정리: {removed}개 삭제 (약 {freed/1024/1024:.1f}MB), 유지 {len(keep)}개")
    else:
        print(f"  ℹ️  삭제할 영상 없음 (유지 {len(keep)}개)")
    return merged

# ── HTML 생성 ────────────────────────────────────────────────────────────────
def get_product(name):
    if "빙과" in name:
        return "빙과"
    if "제과" in name:
        return "제과"
    return "기타"

def build_html(ads_data, range_label=""):
    KST = timezone(timedelta(hours=9))
    updated = datetime.now(KST).strftime("%Y-%m-%d %H:%M (KST)")
    filter_desc = f"{MEDIA_TAG}" + (f" · 광고명 {'/'.join(NAME_INCLUDES)} 포함" if NAME_INCLUDES else " 전체")

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
        if video:
            play_overlay = '<div class="play-icon">▶</div>'
            card_click_attr = f' data-video="{video}"'
            card_classes += " has-video"
        elif video_permalink:
            play_overlay = '<div class="play-icon">▶</div>'
            card_click_attr = f' data-permalink="{video_permalink}"'
            card_classes += " has-permalink"

        product = get_product(ad["name"])

        cards_html += f"""
        <div class="{card_classes}" data-grade="{ad['grade']}" data-product="{product}" data-name="{ad['name']}" data-spend="{ad.get('total_spend', 0):.0f}" data-daily="{ad.get('daily_spend', 0):.0f}" data-cpv="{ad.get('cost_per_view', 0):.2f}" data-play3="{ad.get('play3_rate', 0):.2f}" data-p25="{ad.get('p25_rate', 0):.2f}"{card_click_attr}>
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
                    <div class="metric"><span class="label">일 최고 광고비</span><span class="value peak">{ad.get('peak_daily', 0):,.0f}원</span></div>
                    <div class="metric"><span class="label">조회당 비용</span><span class="value">{ad['cost_per_view']:,.1f}원</span></div>
                    <div class="metric"><span class="label">3초 재생율</span><span class="value">{ad['play3_rate']:.1f}%</span></div>
                    <div class="metric"><span class="label">25% 재생율</span><span class="value">{ad['p25_rate']:.1f}%</span></div>
                    <div class="metric"><span class="label">3초 조회수</span><span class="value">{ad['views3s']:,.0f}</span></div>
                    <div class="metric"><span class="label">집행일수</span><span class="value">{int(ad.get('active_days', 0))}일</span></div>
                </div>
            </div>
        </div>"""

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
  .criteria {{ padding:14px 32px; background:var(--surface); border-bottom:1px solid var(--border); font-size:12px; color:var(--muted); }}
  .criteria b {{ color:var(--text); font-weight:600; }}
  .controls {{ padding:16px 32px 0; display:flex; gap:8px; flex-wrap:wrap; align-items:center; }}
  .filter-label {{ font-size:11px; color:var(--muted); margin-right:2px; }}
  .filter-btn {{ padding:6px 16px; border-radius:20px; border:1px solid var(--border); background:transparent; color:var(--muted); font-size:13px; cursor:pointer; transition:all .15s; font-family:inherit; }}
  .filter-btn:hover {{ border-color:#555; color:var(--text); }}
  .filter-btn.active {{ background:var(--text); color:var(--bg); border-color:var(--text); font-weight:600; }}
  .divider {{ width:1px; height:20px; background:var(--border); margin:0 4px; }}
  .name-search {{ padding:6px 14px; border-radius:20px; border:1px solid var(--border); background:var(--surface); color:var(--text); font-size:13px; font-family:inherit; min-width:220px; }}
  .name-search::placeholder {{ color:var(--muted); }}
  .name-search:hover {{ border-color:#555; }}
  .name-search:focus {{ outline:none; border-color:var(--text); }}
  .count {{ margin-left:auto; font-size:13px; color:var(--muted); }}
  .gallery {{ padding:16px 32px 60px; display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:20px; }}
  .card {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; overflow:hidden; transition:transform .2s,box-shadow .2s; }}
  .card:hover {{ transform:translateY(-4px); box-shadow:0 12px 32px rgba(0,0,0,.4); }}
  .card-img {{ position:relative; background:#111; overflow:hidden; display:flex; align-items:center; justify-content:center; min-height:200px; }}
  .card-img img {{ width:100%; height:auto; display:block; object-fit:contain; }}
  .no-img {{ width:100%; height:200px; display:flex; align-items:center; justify-content:center; color:var(--muted); font-size:12px; }}
  .grade-badge {{ position:absolute; top:10px; right:10px; padding:3px 10px; border-radius:20px; font-size:12px; font-weight:700; color:#fff; }}
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
  .metric .value {{ font-size:13px; font-weight:600; }}
  .value.peak {{ color:#FF6B00; }}
  .empty {{ grid-column:1/-1; text-align:center; padding:80px 0; color:var(--muted); }}
</style>
</head>
<body>
<header>
  <div class="header-left">
    <h1>영상 고효율 광고 아카이브 <span style="color:#BF5AF2;">(BT)</span></h1>
    <p>대상: {filter_desc}{(' · ' + range_label) if range_label else ''}</p>
  </div>
  <div class="header-right">
    <span class="updated">마지막 업데이트: {updated}</span>
    <p class="refresh-notice">업데이트 날짜가 안 바뀌었다면<br><span class="kbd">Cmd+Shift+R</span> 로 강력 새로고침 해주세요!</p>
  </div>
</header>
<div class="criteria">
  📌 수집 기준: <b>총 광고비 150만원 이상</b>인 광고 중 <b>조회당 비용 10원 이하</b> 또는 <b>일 최고 광고비 150만원 이상</b> · 최근 {ARCHIVE_DAYS}일 성과 기준
</div>

<section id="archivePanel">
<div class="controls">
  <span class="filter-label">정렬</span>
  <button class="filter-btn sort-btn active" data-sort="spend">총 광고비</button>
  <button class="filter-btn sort-btn" data-sort="daily">일 광고비</button>
  <button class="filter-btn sort-btn" data-sort="cpv">조회당 비용</button>
  <button class="filter-btn sort-btn" data-sort="play3">3초 재생율</button>
  <button class="filter-btn sort-btn" data-sort="p25">25% 재생율</button>
  <span class="count" id="count"></span>
</div>
<div class="controls" style="margin-top:12px;">
  <span class="filter-label">등급</span>
  <button class="filter-btn grade-btn active" data-grade="all">전체</button>
  <button class="filter-btn grade-btn" data-grade="SS급">SS급</button>
  <button class="filter-btn grade-btn" data-grade="S급">S급</button>
  <button class="filter-btn grade-btn" data-grade="A급">A급</button>
  <button class="filter-btn grade-btn" data-grade="B급">B급</button>
  <button class="filter-btn grade-btn" data-grade="C급">C급</button>
  <div class="divider"></div>
  <span class="filter-label">제품군</span>
  <button class="filter-btn product-btn active" data-product="all">전체</button>
  <button class="filter-btn product-btn" data-product="빙과">빙과</button>
  <button class="filter-btn product-btn" data-product="제과">제과</button>
  <div class="divider"></div>
  <span class="filter-label">검색</span>
  <input type="text" class="name-search" id="nameSearch" placeholder="광고명 검색" autocomplete="off">
</div>
<div class="gallery" id="gallery">
  {cards_html or '<div class="empty">수집 기준(총 광고비 150만원 이상 + 조회당 비용 10원 이하 또는 일 최고 광고비 150만원 이상)을 충족하는 영상이 없습니다.</div>'}
</div>
</section>

<div class="modal" id="videoModal">
  <button class="modal-close" id="modalClose">&times;</button>
  <video id="modalVideo" controls></video>
  <img id="modalImage" alt="광고 이미지">
</div>
<script>
  const gallery = document.getElementById('gallery');
  const cards = [...document.querySelectorAll('#archivePanel .card')];
  const countEl = document.getElementById('count');
  let activeGrade = 'all';
  let activeProduct = 'all';
  let searchQuery = '';
  let activeSort = 'spend';
  // cpv(조회당 비용)만 낮을수록 좋으므로 오름차순, 나머지는 내림차순
  const SORT_CFG = {{
    spend: {{ key: 'spend', asc: false }},
    daily: {{ key: 'daily', asc: false }},
    cpv:   {{ key: 'cpv',   asc: true  }},
    play3: {{ key: 'play3', asc: false }},
    p25:   {{ key: 'p25',   asc: false }},
  }};

  function applyFilters() {{
    const cfg = SORT_CFG[activeSort] || SORT_CFG.spend;
    const sorted = cards.slice().sort((a, b) => {{
      const av = parseFloat(a.dataset[cfg.key]) || 0;
      const bv = parseFloat(b.dataset[cfg.key]) || 0;
      return cfg.asc ? av - bv : bv - av;
    }});
    sorted.forEach(c => gallery.appendChild(c));

    let v = 0;
    cards.forEach(c => {{
      const gradeOk = activeGrade === 'all' || c.dataset.grade === activeGrade;
      const productOk = activeProduct === 'all' || c.dataset.product === activeProduct;
      const nameOk = !searchQuery || (c.dataset.name || '').toLowerCase().includes(searchQuery);
      const show = gradeOk && productOk && nameOk;
      c.style.display = show ? '' : 'none';
      if (show) v++;
    }});
    countEl.textContent = v + '개';
  }}
  applyFilters();

  document.querySelectorAll('.sort-btn').forEach(btn => {{
    btn.addEventListener('click', () => {{
      document.querySelectorAll('.sort-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      activeSort = btn.dataset.sort;
      applyFilters();
    }});
  }});
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
  document.querySelectorAll('#archivePanel .card.has-video').forEach(card => {{
    card.addEventListener('click', () => openVideoModal(card.dataset.video));
  }});
  document.querySelectorAll('#archivePanel .card.has-permalink').forEach(card => {{
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
</script>
</body>
</html>"""

# ── 슬랙 알림 (새로 수집된 영상) ──────────────────────────────────────────────
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

def load_alert_sent():
    if os.path.exists("alert_sent.json"):
        try:
            with open("alert_sent.json", "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_alert_sent(sent):
    try:
        with open("alert_sent.json", "w", encoding="utf-8") as f:
            json.dump(sent, f, ensure_ascii=False)
    except Exception as e:
        print(f"  ⚠️  알림 이력 저장 실패: {e}")

def notify_slack_new(merged):
    """오늘 처음 아카이브에 들어온 영상(first_seen == 오늘)을 슬랙에 알림."""
    if not (SLACK_BOT_TOKEN and SLACK_CHANNEL_ID):
        return
    today_kst = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d")
    alert_sent = load_alert_sent()
    new_all = sorted(
        [a for a in merged
         if a.get("first_seen") == today_kst and a.get("last_seen") == today_kst
         and alert_sent.get(a["name"]) != today_kst],
        key=lambda x: -x["total_spend"])
    if not new_all:
        print("  ℹ️  오늘 새로 들어온 영상 없음 → 슬랙 알림 생략")
        return
    parent = f"🎬 *[새 고효율 영상]* {today_kst}\n새로 아카이브된 영상 {len(new_all)}건"
    parent_ts = _slack_post(parent)
    if not parent_ts:
        return
    lines = [f"🎬 *새 고효율 영상 {len(new_all)}건*"]
    for a in new_all:
        lines.append(f"*[{a['grade']}] {a['name']}*")
        lines.append(f"　총 {int(round(a['total_spend'])):,}원 · 조회당 {a['cost_per_view']:.1f}원 · 3초재생 {a['play3_rate']:.1f}% · 25%재생 {a['p25_rate']:.1f}%")
    lines.append("")
    lines.append(f"👉 자세히 보기: {SITE_URL}")
    if _slack_post("\n".join(lines), thread_ts=parent_ts):
        print(f"  📨 슬랙 알림 전송: {len(new_all)}건")
        for a in new_all:
            alert_sent[a["name"]] = today_kst
        alert_sent = {name: d for name, d in alert_sent.items() if d == today_kst}
        save_alert_sent(alert_sent)

# ── 메인 ─────────────────────────────────────────────────────────────────────
def main():
    existing = load_archive()
    print(f"기존 아카이브 {len(existing)}개")

    # 수집 기간: 직접 지정(DATE_START/STOP) 있으면 그걸로, 없으면 최근 ARCHIVE_DAYS일
    today = datetime.today()
    if DATE_START and DATE_STOP:
        d_start, d_stop = DATE_START, DATE_STOP
    else:
        d_start = (today - timedelta(days=ARCHIVE_DAYS - 1)).strftime("%Y-%m-%d")
        d_stop  = today.strftime("%Y-%m-%d")
    range_label = f"{d_start} ~ {d_stop}"

    try:
        new_results = collect_archive(d_start, d_stop)
        merged = merge_archive(existing, new_results)
    except Exception as e:
        print(f"  ⚠️  수집 오류(기존 데이터 유지): {e}")
        merged = existing

    # 슬랙 알림 (새로 들어온 것)
    try:
        notify_slack_new(merged)
    except Exception as e:
        print(f"  ⚠️  슬랙 알림 오류(건너뜀): {e}")

    # 영상 정리
    try:
        merged = cleanup_videos(merged)
    except Exception as e:
        print(f"  ⚠️  영상 정리 오류(건너뜀): {e}")

    save_archive(merged)

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(build_html(merged, range_label))
    print(f"✅ 완료 (총 {len(merged)}개)")

if __name__ == "__main__":
    main()
