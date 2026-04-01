import os
import re
import sys
import json
import time
import hmac
import hashlib
import asyncio
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional, Dict, Any, Tuple

from fastapi import FastAPI, Request, Response, Header, HTTPException
from pydantic import BaseModel
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# =========================================================
# 路径与配置
# =========================================================

def get_runtime_dir() -> Path:
    # PyInstaller 单文件模式下，运行目录应取 exe 所在目录，而不是临时解压目录。
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = get_runtime_dir()
DATA_DIR = BASE_DIR / "runtime_data"
SCREENSHOT_DIR = DATA_DIR / "screenshots"
LOG_FILE = DATA_DIR / "tasks.jsonl"
CONFIG_FILE = BASE_DIR / "config.json"
BROWSERS_DIR = BASE_DIR / "browsers"

DATA_DIR.mkdir(exist_ok=True)
SCREENSHOT_DIR.mkdir(exist_ok=True)

URL_RE = re.compile(r"https?://[^\s]+")


def ensure_playwright_env():
    """
    固定 Playwright 浏览器资源目录，避免打包后默认落到用户缓存路径，
    也便于发布时把 browsers 目录和 exe 放在一起。
    """
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(BROWSERS_DIR))


def get_runtime_info() -> Dict[str, Any]:
    return {
        "frozen": bool(getattr(sys, "frozen", False)),
        "base_dir": str(BASE_DIR),
        "config_file": str(CONFIG_FILE),
        "config_exists": CONFIG_FILE.exists(),
        "data_dir": str(DATA_DIR),
        "screenshot_dir": str(SCREENSHOT_DIR),
        "browsers_dir": str(BROWSERS_DIR),
        "browsers_dir_exists": BROWSERS_DIR.exists(),
        "playwright_browsers_path": os.getenv("PLAYWRIGHT_BROWSERS_PATH", ""),
        "onebot_secret_enabled": bool(ONEBOT_SECRET),
    }


def load_config() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    if not CONFIG_FILE.exists():
        raise RuntimeError("缺少配置文件: backend/config.json")

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    profile = data.get("profile")
    settings = data.get("settings")

    if not isinstance(profile, dict):
        raise RuntimeError("config.json 缺少 profile 对象")
    if not isinstance(settings, dict):
        raise RuntimeError("config.json 缺少 settings 对象")

    required_profile = [
        "name",
        "student_id",
        "mobile",
        "building",
        "college",
        "student_type",
        "political_status",
        "activity_name",
    ]
    for key in required_profile:
        if key not in profile:
            raise RuntimeError("config.json 的 profile 缺少字段: %s" % key)

    defaults = {
        "auto_submit": True,
        "dedup_ttl_seconds": 1800,
        "max_retries": 2,
        "goto_timeout_ms": 20000,
        "form_wait_timeout_ms": 25000,
        "headless": False,
        "host": "127.0.0.1",
        "port": 8000,
    }
    for k, v in defaults.items():
        settings.setdefault(k, v)

    # 清理常见的尾部空格，避免表单误填
    for key in ("name", "student_id", "mobile", "building", "college", "student_type", "political_status"):
        profile[key] = str(profile.get(key, "")).strip()

    if isinstance(profile.get("activity_name"), list):
        profile["activity_name"] = [str(x).strip() for x in profile["activity_name"]]
    else:
        profile["activity_name"] = str(profile["activity_name"]).strip()

    return profile, settings


ensure_playwright_env()
PROFILE, SETTINGS = load_config()

# NapCat / OneBot secret
# 优先读取环境变量 ONEBOT_SECRET；若为空则读取 config.json 的 onebot_secret
ONEBOT_SECRET = os.getenv("ONEBOT_SECRET", "").strip()
if not ONEBOT_SECRET:
    with open(CONFIG_FILE, "r", encoding="utf-8") as _f:
        _cfg = json.load(_f)
    ONEBOT_SECRET = str(_cfg.get("onebot_secret", "")).strip()

# =========================================================
# 全局状态
# =========================================================

app = FastAPI(title="QQ Jinshuju Auto Signup")

PLAYWRIGHT = None
BROWSER = None
CONTEXT = None
PAGE = None

task_queue = None
processed_urls = {}
worker_lock = None

# =========================================================
# 数据模型
# =========================================================


class MessagePayload(BaseModel):
    text: str
    source: str = "unknown"
    sender: str = ""
    chat_name: str = ""


# =========================================================
# 基础工具函数
# =========================================================

def now_ts():
    return time.time()


def ts_str():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def append_log(record):
    row = dict(record)
    row["logged_at"] = ts_str()
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def sanitize_filename(text):
    return re.sub(r'[\\/:*?"<>|]+', "_", text)


def normalize_text(text):
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def is_jinshuju_url(url):
    try:
        host = (urlparse(url).netloc or "").lower()
        return "jinshuju.com" in host
    except Exception:
        return False


def extract_jinshuju_urls(text):
    urls = []
    for m in URL_RE.findall(text or ""):
        url = m.strip().rstrip(").,]}>\"'")
        if is_jinshuju_url(url):
            urls.append(url)

    seen = set()
    result = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


def should_skip_url(url):
    item = processed_urls.get(url)
    if not item:
        return False

    if now_ts() - item["time"] > SETTINGS["dedup_ttl_seconds"]:
        return False

    return item["status"] in {"queued", "processing", "success"}


def mark_url(url, status, extra=None):
    data = {
        "status": status,
        "time": now_ts(),
    }
    if extra:
        data.update(extra)
    processed_urls[url] = data


async def save_screenshot(page, prefix):
    filename = sanitize_filename("%s_%s.png" % (prefix, int(time.time())))
    path = SCREENSHOT_DIR / filename
    await page.screenshot(path=str(path), full_page=True)
    return str(path)


# =========================================================
# OneBot / NapCat 工具
# =========================================================

def verify_onebot_signature(raw_body, x_signature):
    if not ONEBOT_SECRET:
        return True

    if not x_signature or not x_signature.startswith("sha1="):
        return False

    expected = hmac.new(
        ONEBOT_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha1
    ).hexdigest()

    received = x_signature[len("sha1="):].strip()
    return hmac.compare_digest(expected, received)


def onebot_message_to_text(message):
    if isinstance(message, str):
        return message

    if isinstance(message, list):
        parts = []
        for seg in message:
            if not isinstance(seg, dict):
                continue

            seg_type = seg.get("type")
            data = seg.get("data", {}) or {}

            if seg_type == "text":
                text = data.get("text")
                if text:
                    parts.append(str(text))
            else:
                for key in ("text", "url", "title", "content", "file"):
                    value = data.get(key)
                    if value:
                        parts.append(str(value))

        return " ".join(parts).strip()

    return ""


def parse_onebot_sender_and_chat(event):
    sender = event.get("sender", {}) or {}
    nickname = sender.get("nickname") or ""
    card = sender.get("card") or ""
    user_id = event.get("user_id", "")

    sender_name = card or nickname or (str(user_id) if user_id else "")

    if event.get("message_type") == "group":
        group_id = event.get("group_id", "")
        chat_name = "group:%s" % group_id
    else:
        chat_name = "private"

    return sender_name, chat_name


# =========================================================
# Playwright
# =========================================================

async def block_unneeded(route):
    req = route.request
    rtype = req.resource_type
    url = req.url.lower()

    if rtype in {"image", "media", "font"}:
        await route.abort()
        return

    if any(x in url for x in [
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
        ".woff", ".woff2", ".ttf"
    ]):
        await route.abort()
        return

    await route.continue_()


async def init_browser():
    global PLAYWRIGHT, BROWSER, CONTEXT, PAGE

    PLAYWRIGHT = await async_playwright().start()
    BROWSER = await PLAYWRIGHT.chromium.launch(headless=SETTINGS["headless"])
    CONTEXT = await BROWSER.new_context(service_workers="block")
    await CONTEXT.route("**/*", block_unneeded)
    PAGE = await CONTEXT.new_page()
    print("[INFO] 浏览器预热完成")


async def close_browser():
    global PLAYWRIGHT, BROWSER, CONTEXT, PAGE

    try:
        if PAGE:
            await PAGE.close()
    except Exception:
        pass

    try:
        if CONTEXT:
            await CONTEXT.close()
    except Exception:
        pass

    try:
        if BROWSER:
            await BROWSER.close()
    except Exception:
        pass

    try:
        if PLAYWRIGHT:
            await PLAYWRIGHT.stop()
    except Exception:
        pass


# =========================================================
# 表单适配逻辑（不依赖固定 field_x 编号）
# =========================================================

FORM_SEQUENCE = [
    {"key": "name", "type": "text", "label": "姓名"},
    {"key": "student_id", "type": "text", "label": "学号"},
    {"key": "mobile", "type": "mobile", "label": "手机"},
    {"key": "building", "type": "dropdown", "label": "所住楼栋"},
    {"key": "activity_name", "type": "radio", "label": "活动项"},
    {"key": "college", "type": "text", "label": "学院"},
    {"key": "student_type", "type": "radio", "label": "生源类别"},
    {"key": "political_status", "type": "radio", "label": "政治面貌"},
]

LABEL_ALIASES = {
    "name": ["姓名"],
    "student_id": ["学号"],
    "mobile": ["手机", "联系电话", "手机号"],
    "building": ["所住楼栋", "楼栋", "宿舍楼栋"],
    "activity_name": ["活动项", "活动项目", "活动名称", "志愿活动"],
    "college": ["学院", "学院名称"],
    "student_type": ["生源类别"],
    "political_status": ["政治面貌"],
}


async def get_field_containers(page):
    containers = page.locator("div.field-container")
    total = await containers.count()
    result = []
    for i in range(total):
        result.append(containers.nth(i))
    return result


async def get_container_label(container):
    try:
        label = container.locator(".ant-form-item-label")
        if await label.count() > 0:
            text = await label.first.text_content()
            return normalize_text(text)
    except Exception:
        pass
    return ""


async def has_visible_text_input(container):
    locator = container.locator(
        "input[type='text']:not(.ant-select-selection-search-input)"
    )
    return await locator.count() > 0


async def is_interactive_container(container):
    try:
        if await container.locator("input[type='radio']").count() > 0:
            return True

        if await container.locator(".ant-select-selector").count() > 0:
            return True

        if await has_visible_text_input(container):
            return True

        return False
    except Exception:
        return False


async def detect_container_type(container):
    if await container.locator(".ant-select-selector").count() > 0:
        return "dropdown"

    if await container.locator("input[type='radio']").count() > 0:
        return "radio"

    if await container.locator(".mobile-field input[type='text']").count() > 0:
        return "mobile"

    if await has_visible_text_input(container):
        return "text"

    return "unknown"


async def fill_text_in_container(container, value):
    input_box = container.locator(
        "input[type='text']:not(.ant-select-selection-search-input)"
    ).first
    await input_box.scroll_into_view_if_needed()
    await input_box.click()
    await input_box.fill(str(value))


async def select_dropdown_in_container(container, option_text):
    selector = container.locator(".ant-select-selector").first
    await selector.scroll_into_view_if_needed()
    await selector.click()

    page = container.page
    option_locator = page.locator(
        '.ant-select-item-option:not(.ant-select-item-option-disabled):has-text("%s")'
        % option_text
    )

    if await option_locator.count() == 0:
        disabled_or_any = page.locator(
            '.ant-select-item-option:has-text("%s")' % option_text
        )
        if await disabled_or_any.count() > 0:
            raise Exception("下拉选项存在但不可选: %s" % option_text)
        raise Exception("下拉框中未找到选项: %s" % option_text)

    await option_locator.first.click()


async def select_radio_in_container(container, option_text):
    if isinstance(option_text, str):
        candidates = [option_text]
    else:
        candidates = list(option_text)

    labels = container.locator("label")
    count = await labels.count()

    if count == 0:
        raise Exception("当前单选题没有找到任何 label")

    all_options = []

    for i in range(count):
        label = labels.nth(i)
        text = normalize_text(await label.text_content())
        all_options.append(text)

        for candidate in candidates:
            if candidate in text:
                await label.scroll_into_view_if_needed()
                await label.click(force=True)
                print("[OK] 单选命中: %s | 页面文本: %s" % (candidate, text))
                return

    raise Exception(
        "单选题中未找到候选项: %s | 页面现有选项: %s"
        % (candidates, all_options)
    )


async def try_fill_by_label_first(page):
    filled = set()
    containers = await get_field_containers(page)

    for container in containers:
        if not await is_interactive_container(container):
            continue

        label_text = await get_container_label(container)
        ctype = await detect_container_type(container)

        for key, aliases in LABEL_ALIASES.items():
            if key in filled:
                continue

            if not any(alias in label_text for alias in aliases):
                continue

            value = PROFILE[key]

            if key in {"name", "student_id", "college"} and ctype == "text":
                await fill_text_in_container(container, value)
                print("[OK] 按标签填写 %s -> %s" % (label_text, value))
                filled.add(key)
                break

            if key == "mobile" and ctype in {"mobile", "text"}:
                await fill_text_in_container(container, value)
                print("[OK] 按标签填写 %s -> %s" % (label_text, value))
                filled.add(key)
                break

            if key == "building" and ctype == "dropdown":
                await select_dropdown_in_container(container, value)
                print("[OK] 按标签选择 %s -> %s" % (label_text, value))
                filled.add(key)
                break

            if key in {"activity_name", "student_type", "political_status"} and ctype == "radio":
                await select_radio_in_container(container, value)
                print("[OK] 按标签选择 %s -> %s" % (label_text, value))
                filled.add(key)
                break

    return filled


async def fill_by_sequence(page, already_filled=None):
    if already_filled is None:
        already_filled = set()

    containers = await get_field_containers(page)

    interactive = []
    for container in containers:
        if await is_interactive_container(container):
            interactive.append(container)

    pending_specs = [x for x in FORM_SEQUENCE if x["key"] not in already_filled]

    if len(interactive) < len(pending_specs):
        raise Exception(
            "页面可填写字段数量不足。interactive=%s pending=%s"
            % (len(interactive), len(pending_specs))
        )

    cursor = 0

    for spec in pending_specs:
        expected_type = spec["type"]
        value = PROFILE[spec["key"]]
        matched = False

        while cursor < len(interactive):
            container = interactive[cursor]
            cursor += 1

            label_text = await get_container_label(container)
            ctype = await detect_container_type(container)

            type_ok = False
            if expected_type == "text" and ctype == "text":
                type_ok = True
            elif expected_type == "mobile" and ctype in {"mobile", "text"}:
                type_ok = True
            elif expected_type == "dropdown" and ctype == "dropdown":
                type_ok = True
            elif expected_type == "radio" and ctype == "radio":
                type_ok = True

            if not type_ok:
                continue

            if expected_type in {"text", "mobile"}:
                await fill_text_in_container(container, value)
                print("[OK] 按顺序填写 %s -> %s | label=%s" % (spec["key"], value, label_text))
                matched = True
                break

            if expected_type == "dropdown":
                await select_dropdown_in_container(container, value)
                print("[OK] 按顺序选择 %s -> %s | label=%s" % (spec["key"], value, label_text))
                matched = True
                break

            if expected_type == "radio":
                await select_radio_in_container(container, value)
                print("[OK] 按顺序选择 %s -> %s | label=%s" % (spec["key"], value, label_text))
                matched = True
                break

        if not matched:
            raise Exception("按顺序未找到可匹配字段: %s" % spec["key"])


async def fill_and_submit(page):
    filled = await try_fill_by_label_first(page)
    await fill_by_sequence(page, already_filled=filled)

    print("[STEP] 表单填写完成")

    if SETTINGS["auto_submit"]:
        submit_btn = page.locator("button.published-form__submit").first
        await submit_btn.scroll_into_view_if_needed()
        await submit_btn.click()
        print("[OK] 已点击提交")
        return "submitted"
    else:
        print("[INFO] 当前为仅填写模式，未自动提交")
        return "filled_only"


# =========================================================
# 任务处理
# =========================================================

async def handle_url(url, source="unknown", sender="", chat_name=""):
    global PAGE

    total_start = time.perf_counter()
    print("[INFO] 开始处理链接: %s" % url)

    mark_url(url, "processing", {
        "source": source,
        "sender": sender,
        "chat_name": chat_name,
    })

    append_log({
        "event": "task_start",
        "url": url,
        "source": source,
        "sender": sender,
        "chat_name": chat_name,
    })

    try:
        nav_start = time.perf_counter()

        await PAGE.goto(
            url,
            wait_until="domcontentloaded",
            timeout=SETTINGS["goto_timeout_ms"]
        )

        await PAGE.locator("div.field-container").first.wait_for(
            timeout=SETTINGS["form_wait_timeout_ms"]
        )

        nav_end = time.perf_counter()
        print("[TIME] 页面可操作耗时: %.3f 秒" % (nav_end - nav_start))

        fill_start = time.perf_counter()
        result = await fill_and_submit(PAGE)
        fill_end = time.perf_counter()

        screenshot = await save_screenshot(PAGE, "success")

        print("[TIME] 填写阶段耗时: %.3f 秒" % (fill_end - fill_start))
        print("[TIME] 总耗时: %.3f 秒" % (fill_end - total_start))

        mark_url(url, "success", {
            "source": source,
            "sender": sender,
            "chat_name": chat_name,
            "result": result,
            "screenshot": screenshot,
        })

        append_log({
            "event": "task_success",
            "url": url,
            "source": source,
            "sender": sender,
            "chat_name": chat_name,
            "result": result,
            "screenshot": screenshot,
            "elapsed_seconds": round(fill_end - total_start, 3),
        })

    except PlaywrightTimeoutError as e:
        fail_time = time.perf_counter()
        screenshot = ""

        try:
            screenshot = await save_screenshot(PAGE, "timeout")
        except Exception:
            pass

        print("[ERROR] 超时异常: %r" % e)
        print("[TIME] 从开始处理到报错耗时: %.3f 秒" % (fail_time - total_start))

        mark_url(url, "failed", {
            "source": source,
            "sender": sender,
            "chat_name": chat_name,
            "error": repr(e),
            "screenshot": screenshot,
        })

        append_log({
            "event": "task_failed",
            "url": url,
            "source": source,
            "sender": sender,
            "chat_name": chat_name,
            "error": repr(e),
            "screenshot": screenshot,
            "elapsed_seconds": round(fail_time - total_start, 3),
        })
        raise

    except Exception as e:
        fail_time = time.perf_counter()
        screenshot = ""

        try:
            screenshot = await save_screenshot(PAGE, "error")
        except Exception:
            pass

        print("[ERROR] 执行异常: %r" % e)
        print("[TIME] 从开始处理到报错耗时: %.3f 秒" % (fail_time - total_start))

        mark_url(url, "failed", {
            "source": source,
            "sender": sender,
            "chat_name": chat_name,
            "error": repr(e),
            "screenshot": screenshot,
        })

        append_log({
            "event": "task_failed",
            "url": url,
            "source": source,
            "sender": sender,
            "chat_name": chat_name,
            "error": repr(e),
            "screenshot": screenshot,
            "elapsed_seconds": round(fail_time - total_start, 3),
        })
        raise


async def handle_with_retry(url, source, sender, chat_name):
    last_error = None
    for attempt in range(SETTINGS["max_retries"] + 1):
        try:
            print("[INFO] 处理任务 attempt=%s url=%s" % (attempt + 1, url))
            await handle_url(url, source, sender, chat_name)
            return
        except Exception as e:
            last_error = e
            if attempt < SETTINGS["max_retries"]:
                print("[WARN] 即将重试 url=%s" % url)
                await asyncio.sleep(1.5)
    raise last_error


async def worker():
    while True:
        task = await task_queue.get()
        url = task["url"]
        source = task.get("source", "unknown")
        sender = task.get("sender", "")
        chat_name = task.get("chat_name", "")

        try:
            async with worker_lock:
                await handle_with_retry(url, source, sender, chat_name)
        except Exception as e:
            print("[ERROR] worker 最终失败: %s -> %r" % (url, e))
        finally:
            task_queue.task_done()


async def enqueue_urls(urls, source, sender, chat_name):
    queued = []
    skipped = []

    for url in urls:
        if should_skip_url(url):
            skipped.append(url)
            continue

        mark_url(url, "queued", {
            "source": source,
            "sender": sender,
            "chat_name": chat_name,
        })

        await task_queue.put({
            "url": url,
            "source": source,
            "sender": sender,
            "chat_name": chat_name,
        })

        queued.append(url)

        append_log({
            "event": "task_queued",
            "url": url,
            "source": source,
            "sender": sender,
            "chat_name": chat_name,
        })

    return queued, skipped


# =========================================================
# HTTP API
# =========================================================

@app.get("/")
async def root():
    return {
        "ok": True,
        "service": "qq-jinshuju-auto-signup",
        "queue_size": task_queue.qsize(),
        "auto_submit": SETTINGS["auto_submit"],
    }


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/status")
async def status():
    recent_items = list(processed_urls.items())[-20:]
    recent = dict(recent_items)
    return {
        "ok": True,
        "queue_size": task_queue.qsize(),
        "recent_urls": recent,
        "auto_submit": SETTINGS["auto_submit"],
        "log_file": str(LOG_FILE),
        "screenshot_dir": str(SCREENSHOT_DIR),
    }


@app.get("/debug/env")
async def debug_env():
    return {
        "ok": True,
        "runtime": get_runtime_info(),
        "settings": {
            "host": SETTINGS["host"],
            "port": SETTINGS["port"],
            "headless": SETTINGS["headless"],
            "auto_submit": SETTINGS["auto_submit"],
        },
    }


@app.post("/message")
async def receive_message(payload: MessagePayload):
    urls = extract_jinshuju_urls(payload.text)

    if not urls:
        append_log({
            "event": "message_ignored",
            "source": payload.source,
            "sender": payload.sender,
            "chat_name": payload.chat_name,
            "reason": "no_jinshuju_url"
        })
        return {"ok": True, "queued": 0, "reason": "no_jinshuju_url"}

    queued, skipped = await enqueue_urls(
        urls=urls,
        source=payload.source,
        sender=payload.sender,
        chat_name=payload.chat_name,
    )

    return {
        "ok": True,
        "queued": len(queued),
        "queued_urls": queued,
        "skipped_urls": skipped,
    }


@app.post("/url")
async def receive_url(request: Request):
    data = await request.json()

    url = (data.get("url") or "").strip()
    source = data.get("source", "manual")
    sender = data.get("sender", "")
    chat_name = data.get("chat_name", "")

    if not url or not is_jinshuju_url(url):
        return {"ok": False, "error": "invalid_jinshuju_url"}

    queued, skipped = await enqueue_urls(
        urls=[url],
        source=source,
        sender=sender,
        chat_name=chat_name,
    )

    return {
        "ok": True,
        "queued": len(queued),
        "queued_urls": queued,
        "skipped_urls": skipped,
    }


@app.post("/onebot")
async def onebot_webhook(
    request: Request,
    x_signature: Optional[str] = Header(default=None, alias="X-Signature"),
    x_self_id: Optional[str] = Header(default=None, alias="X-Self-ID"),
):
    raw_body = await request.body()

    if not verify_onebot_signature(raw_body, x_signature):
        raise HTTPException(status_code=403, detail="invalid signature")

    try:
        event = json.loads(raw_body.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")

    if event.get("post_type") != "message":
        return Response(status_code=204)

    message_type = event.get("message_type")
    if message_type not in {"private", "group"}:
        return Response(status_code=204)

    text = onebot_message_to_text(event.get("message"))
    raw_message = event.get("raw_message")

    if raw_message and len(raw_message) > len(text):
        text = raw_message

    sender_name, chat_name = parse_onebot_sender_and_chat(event)
    urls = extract_jinshuju_urls(text)

    if not urls:
        append_log({
            "event": "onebot_message_ignored",
            "reason": "no_jinshuju_url",
            "self_id": x_self_id or event.get("self_id"),
            "message_type": message_type,
            "sender": sender_name,
            "chat_name": chat_name,
            "text_preview": text[:200],
        })
        return Response(status_code=204)

    queued, skipped = await enqueue_urls(
        urls=urls,
        source="onebot",
        sender=sender_name,
        chat_name=chat_name,
    )

    append_log({
        "event": "onebot_message_received",
        "self_id": x_self_id or event.get("self_id"),
        "message_type": message_type,
        "sender": sender_name,
        "chat_name": chat_name,
        "message_id": event.get("message_id"),
        "queued_count": len(queued),
        "skipped_count": len(skipped),
    })

    return Response(status_code=204)


# =========================================================
# 生命周期
# =========================================================

@app.on_event("startup")
async def on_startup():
    global task_queue, worker_lock

    print("[INFO] 服务启动中...")
    print("[INFO] 运行目录: %s" % BASE_DIR)
    print("[INFO] 配置文件: %s" % CONFIG_FILE)
    print("[INFO] 浏览器目录: %s" % BROWSERS_DIR)
    task_queue = asyncio.Queue()
    worker_lock = asyncio.Lock()
    await init_browser()
    asyncio.create_task(worker())
    print("[INFO] worker 已启动")


@app.on_event("shutdown")
async def on_shutdown():
    print("[INFO] 服务关闭中...")
    await close_browser()


# =========================================================
# 本地启动入口
# =========================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=SETTINGS["host"],
        port=SETTINGS["port"],
        reload=False,
    )
