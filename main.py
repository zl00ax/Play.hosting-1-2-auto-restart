import json
import os
import random
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from seleniumbase import SB

from proxy_manager import SingBoxProxy
from telegram_notify import escape_html, send_telegram_report, send_telegram_text

LOGIN_URL = "https://panel.play.hosting/auth/login"
SCREENSHOT_DIR = "screenshots"
BASE_PROXY_PORT = 10808
MAX_ACCOUNT_RETRIES = 2

VIEWPORT_POOL = [
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1280, "height": 800},
]

STATE_LABELS = {
    "online": "🟢 在线运行中 (Online)",
    "starting": "🟡 正在启动 (Starting)",
    "offline": "🔴 已停机 (Offline)",
    "limbo": "💤 深度休眠 (Server in limbo)",
    "restoring": "⏳ 正在恢复存档 (Restoring)",
    "unknown": "❓ 未知状态 (Unknown)",
}


def now_cst() -> str:
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S (UTC+8)")


def mask_email(email: str) -> str:
    if "@" not in email:
        return email[:2] + "***"
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        masked = local[0] + "***"
    else:
        masked = local[:2] + "***" + local[-1]
    return f"{masked}@{domain}"


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", name or "account")
    return cleaned[:32] or "account"


def parse_float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def load_accounts() -> List[Dict[str, str]]:
    raw_json = os.getenv("ACCOUNTS_JSON", "").strip()
    accounts: List[Dict[str, str]] = []

    if raw_json:
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError as e:
            raise ValueError(f"ACCOUNTS_JSON 格式解析失败，请检查 JSON 语法: {e}")

        if not isinstance(parsed, list) or not parsed:
            raise ValueError("ACCOUNTS_JSON 必须是非空 JSON 数组")

        for i, item in enumerate(parsed, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"ACCOUNTS_JSON 第 {i} 项不是合法对象")
            email = str(item.get("email") or "").strip()
            password = str(item.get("password") or "").strip()
            if not email or not password:
                raise ValueError(f"ACCOUNTS_JSON 第 {i} 项缺少 email 或 password")
            accounts.append(
                {
                    "name": str(item.get("name") or f"账号#{i}").strip(),
                    "email": email,
                    "password": password,
                    "server_id": str(item.get("server_id") or "").strip(),
                    "proxy": str(item.get("proxy") or "").strip(),
                }
            )
        return accounts

    single_email = os.getenv("PLAY_EMAIL", "").strip()
    single_pass = os.getenv("PLAY_PASSWORD", "").strip()
    if single_email and single_pass:
        return [
            {
                "name": "默认账号",
                "email": single_email,
                "password": single_pass,
                "server_id": os.getenv("PLAY_SERVER_ID", "").strip(),
                "proxy": os.getenv("PROXY_URL", "").strip(),
            }
        ]

    raise ValueError("未配置 ACCOUNTS_JSON（或单账号 PLAY_EMAIL / PLAY_PASSWORD）")


def cdp_eval(sb: Any, js_iife: str, default: Any = None) -> Any:
    try:
        return sb.cdp.evaluate(js_iife)
    except Exception:
        return default


def get_body_text(sb: Any) -> str:
    text = cdp_eval(
        sb,
        "(() => (document.body && document.body.innerText) ? document.body.innerText : '')()",
        "",
    )
    return str(text or "")


def get_input_value(sb: Any, css_selector: str) -> str:
    js = f"""(() => {{
        const el = document.querySelector({json.dumps(css_selector)});
        return el ? (el.value || '') : '';
    }})()"""
    return str(cdp_eval(sb, js, "") or "")


def is_css_visible(sb: Any, css_selector: str) -> bool:
    js = f"""(() => {{
        const el = document.querySelector({json.dumps(css_selector)});
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const st = window.getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
    }})()"""
    return bool(cdp_eval(sb, js, False))


def is_button_text_visible(sb: Any, text: str) -> bool:
    js = f"""(() => {{
        const kw = {json.dumps(text)}.toLowerCase();
        const btns = Array.from(document.querySelectorAll('button, a'));
        return btns.some(b => {{
            const t = (b.innerText || '').trim().toLowerCase();
            if (!t.includes(kw)) return false;
            const r = b.getBoundingClientRect();
            const st = window.getComputedStyle(b);
            return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
        }});
    }})()"""
    return bool(cdp_eval(sb, js, False))


def has_turnstile_widget(sb: Any) -> bool:
    """检查登录页是否真正加载出了 Cloudflare Turnstile 组件"""
    js = """(() => {
        if (document.querySelector('input[name="cf-turnstile-response"]')) return true;
        if (document.querySelector('iframe[src*="challenges.cloudflare.com"]')) return true;
        if (document.querySelector('.cf-turnstile')) return true;
        return false;
    })()"""
    return bool(cdp_eval(sb, js, False))


def get_top_status_badge(sb: Any) -> str:
    """精准读取顶部状态栏胶囊文案（Online / Starting / Offline / Stopping）"""
    js = """(() => {
        const els = Array.from(document.querySelectorAll('span, div, p'));
        for (const el of els) {
            const r = el.getBoundingClientRect();
            if (r.top < 0 || r.top > 120 || r.width <= 0 || r.height <= 0 || r.width > 200) continue;
            const t = (el.innerText || '').trim();
            if (t === 'Online' || t === 'Starting' || t === 'Offline' || t === 'Stopping') {
                return t.toLowerCase();
            }
        }
        return '';
    })()"""
    return str(cdp_eval(sb, js, "") or "")


def dismiss_consent_modal(sb: Any) -> bool:
    """自动关闭欧洲节点触发的 GDPR / Google 隐私同意弹窗"""
    js = """(() => {
        const selectors = [
            'button.fc-cta-consent',
            '.fc-consent-root button.fc-primary-button',
            'button[aria-label="Consent"]'
        ];
        for (const sel of selectors) {
            const el = document.querySelector(sel);
            if (el) {
                const r = el.getBoundingClientRect();
                const st = window.getComputedStyle(el);
                if (r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none') {
                    el.click();
                    return 'selector:' + sel;
                }
            }
        }
        const btns = Array.from(document.querySelectorAll('button'));
        for (const b of btns) {
            const t = (b.innerText || '').trim().toLowerCase();
            if (t === 'consent' || t === 'accept all' || t === 'i agree') {
                const r = b.getBoundingClientRect();
                const st = window.getComputedStyle(b);
                if (r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none') {
                    b.click();
                    return 'text:' + t;
                }
            }
        }
        return '';
    })()"""
    res = cdp_eval(sb, js, "")
    if res:
        print(f"[Auth] 检测到隐私同意弹窗，已自动点击 Consent ({res})")
        sb.sleep(1.5)
        return True
    return False


def fill_input_verified(sb: Any, css_selector: str, value: str, label: str) -> None:
    """输入文本并校验实际写入值，防止被延迟弹出的 GDPR 弹窗遮挡导致漏填"""
    for attempt in range(1, 4):
        dismiss_consent_modal(sb)
        try:
            sb.cdp.click(css_selector)
            sb.sleep(random.uniform(0.2, 0.4))
            cdp_eval(
                sb,
                f"""(() => {{
                    const el = document.querySelector({json.dumps(css_selector)});
                    if (el) {{
                        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                        setter.call(el, '');
                        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        el.focus();
                    }}
                }})()""",
                None,
            )
            sb.cdp.type(css_selector, value)
            sb.sleep(random.uniform(0.3, 0.6))
        except Exception as e:
            print(f"[Auth] 第 {attempt} 次填写 {label} 异常: {e}")

        if get_input_value(sb, css_selector) == value:
            return

        print(f"[Auth] 第 {attempt} 次填写 {label} 未生效（可能被弹窗遮挡），清理弹窗后重试...")
        dismiss_consent_modal(sb)
        sb.sleep(0.8)

    cdp_eval(
        sb,
        f"""(() => {{
            const el = document.querySelector({json.dumps(css_selector)});
            if (el) {{
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                setter.call(el, {json.dumps(value)});
                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                el.dispatchEvent(new Event('change', {{ bubbles: true }}));
            }}
        }})()""",
        None,
    )
    sb.sleep(0.4)


def click_button_by_text(
    sb: Any,
    include_text: str,
    exclude_text: str = "",
    in_header_only: bool = False,
    require_ready: bool = True,
) -> bool:
    js = f"""(() => {{
        document.querySelectorAll('[data-ph-target]').forEach(e => e.removeAttribute('data-ph-target'));
        const kw = {json.dumps(include_text)}.toLowerCase();
        const ex = {json.dumps(exclude_text)}.toLowerCase();
        const requireReady = {json.dumps(require_ready)};
        const root = {json.dumps(in_header_only)} ? (document.querySelector('header') || document) : document;
        const btns = Array.from(root.querySelectorAll('button'));
        for (const b of btns) {{
            const t = (b.innerText || '').trim().toLowerCase();
            if (!t.includes(kw)) continue;
            if (ex && t.includes(ex)) continue;
            const r = b.getBoundingClientRect();
            const st = window.getComputedStyle(b);
            if (r.width <= 0 || r.height <= 0 || st.visibility === 'hidden' || st.display === 'none') continue;
            if (requireReady) {{
                const ariaDisabled = b.getAttribute('aria-disabled') === 'true';
                const opacity = parseFloat(st.opacity || '1');
                if (b.disabled || ariaDisabled || st.pointerEvents === 'none' || opacity < 0.75) {{
                    continue;
                }}
            }}
            b.scrollIntoView({{ block: 'center' }});
            b.setAttribute('data-ph-target', '1');
            return true;
        }}
        return false;
    }})()"""
    if not cdp_eval(sb, js, False):
        return False

    sb.sleep(0.3)
    try:
        sb.cdp.click('[data-ph-target="1"]')
        return True
    except Exception:
        return bool(
            cdp_eval(
                sb,
                """(() => {
                    const el = document.querySelector('[data-ph-target="1"]');
                    if (el) { el.click(); return true; }
                    return false;
                })()""",
                False,
            )
        )


def get_turnstile_token(sb: Any) -> str:
    token = cdp_eval(
        sb,
        """(() => {
            const el = document.querySelector('input[name="cf-turnstile-response"]');
            return el ? el.value : '';
        })()""",
        "",
    )
    return str(token or "")


def click_turnstile_by_pyautogui(sb: Any, allow_generic_box: bool = False) -> bool:
    import pyautogui

    pyautogui.FAILSAFE = False

    info = cdp_eval(
        sb,
        f"""(() => {{
            const winX = window.screenX || 0;
            const winY = window.screenY || 0;
            const barH = Math.max(0, window.outerHeight - window.innerHeight);
            const barW = Math.max(0, window.outerWidth - window.innerWidth);
            const allowGeneric = {json.dumps(allow_generic_box)};

            const sels = [
                'iframe[src*="challenges.cloudflare.com"]',
                'iframe[title*="Cloudflare"]',
                'iframe[title*="challenge"]'
            ];
            for (const sel of sels) {{
                const el = document.querySelector(sel);
                if (el) {{
                    const r = el.getBoundingClientRect();
                    if (r.width > 20 && r.height > 20) {{
                        return {{ source: sel, winX, winY, barW, barH, left: r.left, top: r.top, width: r.width, height: r.height }};
                    }}
                }}
            }}

            const inp = document.querySelector('input[name="cf-turnstile-response"]');
            if (inp) {{
                let el = inp.parentElement;
                while (el && el !== document.body) {{
                    const r = el.getBoundingClientRect();
                    if (r.width >= 150 && r.height >= 45 && r.height <= 140) {{
                        return {{ source: 'turnstile-parent', winX, winY, barW, barH, left: r.left, top: r.top, width: r.width, height: r.height }};
                    }}
                    el = el.parentElement;
                }}
            }}

            if (allowGeneric) {{
                const divs = Array.from(document.querySelectorAll('div'));
                for (const d of divs) {{
                    const r = d.getBoundingClientRect();
                    if (r.width >= 260 && r.width <= 360 && r.height >= 55 && r.height <= 90 && r.top > 120 && r.top < 600) {{
                        return {{ source: 'widget-box', winX, winY, barW, barH, left: r.left, top: r.top, width: r.width, height: r.height }};
                    }}
                }}
            }}
            return null;
        }})()""",
        None,
    )

    if not isinstance(info, dict):
        print("[Auth] 未定位到 Turnstile 容器坐标")
        return False

    abs_x = int(info["winX"] + info["barW"] / 2 + info["left"] + random.uniform(25, 34))
    abs_y = int(info["winY"] + info["barH"] + info["top"] + info["height"] / 2 + random.uniform(-3, 3))
    print(
        f"[Auth] PyAutoGUI 精确点击 ({abs_x}, {abs_y}) "
        f"[来源={info['source']}, win=({info['winX']},{info['winY']}), barH={info['barH']}, "
        f"box=({int(info['left'])},{int(info['top'])},{int(info['width'])}x{int(info['height'])})]"
    )

    pyautogui.moveTo(
        max(10, abs_x - random.randint(80, 160)),
        max(10, abs_y + random.randint(-50, 50)),
        duration=random.uniform(0.15, 0.3),
    )
    pyautogui.moveTo(
        abs_x,
        abs_y,
        duration=random.uniform(0.4, 0.75),
        tween=pyautogui.easeOutQuad,
    )
    time.sleep(random.uniform(0.1, 0.25))
    pyautogui.click()
    return True


def attempt_click_cf(sb: Any, attempt_no: int, allow_generic_box: bool = False) -> None:
    if attempt_no % 2 == 1:
        try:
            print(f"[Auth] 第 {attempt_no} 次过盾尝试：SeleniumBase uc_gui_click_captcha()")
            sb.uc_gui_click_captcha()
            return
        except Exception as e:
            print(f"[Auth] uc_gui_click_captcha 异常，改用精确坐标点击: {e}")

    print(f"[Auth] 第 {attempt_no} 次过盾尝试：PyAutoGUI 精确坐标点击")
    try:
        if not click_turnstile_by_pyautogui(sb, allow_generic_box=allow_generic_box):
            sb.uc_gui_click_captcha()
    except Exception as e:
        print(f"[Auth] 精确坐标点击异常: {e}")


def wait_for_login_form_or_bypass_cf(sb: Any, max_wait: int = 80) -> None:
    print("[Auth] 等待登录表单加载（纯 CDP 模式自动过 Cloudflare 全屏盾）...")
    start_ts = time.time()
    last_title = ""
    clicked_times = 0
    last_click_ts = 0.0

    while time.time() - start_ts < max_wait:
        if is_css_visible(sb, 'input[type="password"]'):
            print("[Auth] 登录表单已加载完成")
            return

        current_url = ""
        try:
            current_url = sb.cdp.get_current_url() or ""
        except Exception:
            pass

        if "/server/" in current_url:
            print("[Auth] 页面已直接跳转至服务器面板")
            return

        try:
            title = sb.cdp.get_title() or ""
        except Exception:
            title = ""

        if title != last_title:
            print(f"[Auth] 当前页面标题: {title!r} | URL: {current_url}")
            last_title = title

        elapsed = time.time() - start_ts
        lower_title = title.lower()
        body_lower = get_body_text(sb).lower()
        is_cf_shield = any(
            kw in lower_title or kw in body_lower
            for kw in (
                "just a moment",
                "performing security verification",
                "attention required",
                "verify you are human",
                "请稍候",
            )
        )

        if (
            is_cf_shield
            and clicked_times < 4
            and elapsed > 5
            and (time.time() - last_click_ts >= 13)
        ):
            clicked_times += 1
            attempt_click_cf(sb, clicked_times, allow_generic_box=True)
            last_click_ts = time.time()
            sb.sleep(5)
            continue

        sb.sleep(2)

    snippet = re.sub(r"\s+", " ", get_body_text(sb).strip())[:200]
    raise RuntimeError(
        f"等待登录表单超时({max_wait}s) | Title: {last_title!r} | 页面文本: {snippet}"
    )


def wait_and_solve_login_turnstile(sb: Any, max_wait: int = 55) -> str:
    """
    返回值：
    - "passed": 已拿到有效 Turnstile Token
    - "missing": 超过 16s 页面仍未渲染出 Turnstile 组件（需刷新页面）
    - "timeout": 验证框存在但超时未通过
    """
    print("[Auth] 等待登录页 Cloudflare Turnstile 验证...")
    start_ts = time.time()
    clicked_times = 0
    last_click_ts = 0.0

    while time.time() - start_ts < max_wait:
        dismiss_consent_modal(sb)

        token = get_turnstile_token(sb)
        if len(token) > 10:
            print("[Auth] 登录页 Turnstile 验证已通过")
            return "passed"

        elapsed = time.time() - start_ts
        widget_present = has_turnstile_widget(sb)

        if not widget_present:
            if elapsed >= 16:
                print("[Auth] 16 秒内未检测到 Turnstile 验证框渲染，判定组件加载异常")
                return "missing"
            sb.sleep(2)
            continue

        if elapsed > 6 and clicked_times < 3 and (time.time() - last_click_ts >= 12):
            clicked_times += 1
            attempt_click_cf(sb, clicked_times, allow_generic_box=False)
            last_click_ts = time.time()
            sb.sleep(5)
            continue

        sb.sleep(2)

    print("[Auth] 登录页 Turnstile 等待超时")
    return "timeout"


def login_play_hosting(sb: Any, email: str, password: str) -> None:
    print(f"[Auth] 正在以纯 CDP 模式打开登录页: {LOGIN_URL}")
    sb.activate_cdp_mode(LOGIN_URL)
    sb.sleep(4)

    email_sel = 'input[type="email"], input[name="email"], input[autocomplete="email"]'
    pass_sel = 'input[type="password"]'

    # 若遇 Turnstile 组件未加载出来，自动刷新登录页重试 1 次
    for page_try in range(1, 3):
        if page_try > 1:
            print("[Auth] 刷新登录页重新加载 Turnstile 组件...")
            sb.cdp.open(LOGIN_URL)
            sb.sleep(4)

        wait_for_login_form_or_bypass_cf(sb, max_wait=80)
        if "/auth/login" not in (sb.cdp.get_current_url() or ""):
            return

        sb.sleep(2.0)
        dismiss_consent_modal(sb)

        fill_input_verified(sb, email_sel, email, "邮箱")
        sb.sleep(random.uniform(0.3, 0.7))
        fill_input_verified(sb, pass_sel, password, "密码")
        sb.sleep(random.uniform(0.5, 1.0))

        ts_status = wait_and_solve_login_turnstile(sb, max_wait=55)
        if ts_status == "missing" and page_try == 1:
            continue

        dismiss_consent_modal(sb)
        if get_input_value(sb, email_sel) != email:
            fill_input_verified(sb, email_sel, email, "邮箱")
        if get_input_value(sb, pass_sel) != password:
            fill_input_verified(sb, pass_sel, password, "密码")

        if not click_button_by_text(sb, "Log in", require_ready=False):
            sb.cdp.click('button[type="submit"]')
        print("[Auth] 已点击 Log in，等待登录跳转...")

        deadline = time.time() + 35
        while time.time() < deadline:
            cur_url = sb.cdp.get_current_url() or ""
            if "/auth/login" not in cur_url:
                print(f"[Auth] 登录成功，当前页面: {cur_url}")
                sb.sleep(random.uniform(2.0, 3.5))
                return
            sb.sleep(1.5)

        if page_try == 1:
            print("[Auth] 本次提交未跳转，刷新登录页重试一次...")
            continue

    raise RuntimeError("登录超时或失败（可能账号密码错误、Turnstile 未加载或验证未通过）")


def open_server_page(sb: Any, server_id: str) -> str:
    cur_url = sb.cdp.get_current_url() or ""
    if server_id:
        target_url = (
            server_id
            if server_id.startswith("http")
            else f"https://panel.play.hosting/server/{server_id.strip('/')}"
        )
        if target_url not in cur_url:
            print(f"[Panel] 跳转到服务器页面: {target_url}")
            sb.cdp.open(target_url)
    elif "/server/" not in cur_url:
        print("[Panel] 未指定 server_id，尝试自动打开第一个服务器...")
        deadline_link = time.time() + 20
        while time.time() < deadline_link:
            dismiss_consent_modal(sb)
            if is_css_visible(sb, 'a[href*="/server/"]'):
                sb.cdp.click('a[href*="/server/"]')
                break
            sb.sleep(1)

    deadline = time.time() + 40
    keywords = (
        "Server in limbo",
        "Restoring server",
        "Connect with",
        "Offline",
        "Online",
        "Starting",
    )
    while time.time() < deadline:
        dismiss_consent_modal(sb)
        text = get_body_text(sb)
        if any(k in text for k in keywords):
            break
        sb.sleep(1.5)

    sb.sleep(3)
    dismiss_consent_modal(sb)
    return sb.cdp.get_current_url() or ""


def detect_server_state(sb: Any) -> str:
    body_text = get_body_text(sb)

    if "Server in limbo" in body_text or is_button_text_visible(sb, "Wake server"):
        return "limbo"

    if "Restoring server" in body_text or "Resuming your server" in body_text:
        return "restoring"

    badge = get_top_status_badge(sb)
    if badge == "online":
        return "online"
    if badge == "starting":
        return "starting"
    if badge in ("offline", "stopping"):
        return "offline"

    has_stop = is_button_text_visible(sb, "Stop")
    has_restart = is_button_text_visible(sb, "Restart")
    if has_stop or has_restart:
        if "Starting for " in body_text or "Pre-start tasks" in body_text:
            return "starting"
        return "online"

    if (
        "Your server is offline" in body_text
        or is_button_text_visible(sb, "Start server")
        or is_button_text_visible(sb, "Start")
    ):
        return "offline"

    return "unknown"


def click_start_button(sb: Any, max_wait: int = 45) -> None:
    """等待面板控制台加载完毕、Start 按钮解除置灰后再点击开机"""
    print(f"[Action] 等待 Start 开机按钮就绪（最长 {max_wait}s）...")
    deadline = time.time() + max_wait
    logged_waiting = False

    while time.time() < deadline:
        dismiss_consent_modal(sb)

        cur_state = detect_server_state(sb)
        if cur_state in ("starting", "online"):
            print(f"[Action] 服务器已变为 {cur_state}，跳过点击 Start")
            return
        if cur_state in ("limbo", "restoring"):
            print(f"[Action] 等待 Start 期间服务器进入 {cur_state}，交由外层重新唤醒")
            return

        if click_button_by_text(sb, "Start", in_header_only=True, require_ready=True):
            print("[Action] 已点击顶栏 Start 按钮")
            return
        if click_button_by_text(sb, "Start", exclude_text="Start server", require_ready=True):
            print("[Action] 已点击 Start 按钮")
            return
        if click_button_by_text(sb, "Start server", require_ready=True):
            print("[Action] 已点击 Start server 按钮")
            return
        if click_button_by_text(sb, "Start", require_ready=True):
            print("[Action] 已点击可用 Start 按钮")
            return

        if not logged_waiting:
            print("[Action] Start 按钮暂未就绪（控制台正在加载），等待解除置灰...")
            logged_waiting = True
        sb.sleep(2.5)

    if click_button_by_text(sb, "Start", require_ready=False):
        print("[Action] 等待超时，已尝试兜底点击 Start 按钮")
        return

    raise RuntimeError(f"等待 {max_wait}s 后仍未找到可点击的 Start 开机按钮")


def wait_for_restore_complete(sb: Any, max_wait: int = 240) -> str:
    print("[Action] 等待服务器从 Limbo 恢复（最长 4 分钟）...")
    deadline = time.time() + max_wait
    while time.time() < deadline:
        state = detect_server_state(sb)
        print(f"[Action] 恢复检查当前状态: {state}")
        if state not in ("limbo", "restoring"):
            return state
        sb.sleep(6)
    return detect_server_state(sb)


def wait_for_boot(sb: Any, max_wait: int = 120) -> str:
    print(f"[Action] 等待服务器完成启动变为 Online（最长 {max_wait}s）...")
    deadline = time.time() + max_wait
    retried_start = False
    start_ts = time.time()

    while time.time() < deadline:
        state = detect_server_state(sb)
        print(f"[Action] 开机检查当前状态: {state}")

        if state == "online":
            sb.sleep(5)
            confirm_state = detect_server_state(sb)
            print(f"[Action] 5s 后复核状态: {confirm_state}")
            if confirm_state == "online":
                return "online"
            state = confirm_state

        if state in ("limbo", "restoring"):
            print(f"[Action] 启动过程中检测到服务器掉回 {state}！")
            return state

        if state == "offline" and not retried_start and (time.time() - start_ts >= 15):
            print("[Action] 15 秒后仍为 offline，补点一次 Start 按钮...")
            retried_start = True
            try:
                click_start_button(sb, max_wait=20)
            except Exception as e:
                print(f"[Action] 补点 Start 失败: {e}")

        sb.sleep(5)

    return detect_server_state(sb)


def save_cdp_screenshot(sb: Any, path: str) -> None:
    try:
        sb.cdp.save_screenshot(path)
    except Exception:
        try:
            sb.save_screenshot(path)
        except Exception:
            pass


def process_single_account(
    account: Dict[str, str],
    index: int,
    total: int,
    tg_token: str,
    tg_chat_id: str,
) -> Dict[str, Any]:
    name = account["name"]
    email = account["email"]
    password = account["password"]
    server_id = account["server_id"]
    proxy_url = account["proxy"]

    masked = mask_email(email)
    screenshot_path = os.path.join(
        SCREENSHOT_DIR, f"{index:02d}_{sanitize_filename(name)}.png"
    )

    print(f"\n==================== [{index}/{total}] {name} ({masked}) ====================")

    actions: List[str] = []
    initial_state = "unknown"
    final_state = "unknown"
    current_url = ""
    error_msg = ""
    exit_ip = ""
    protocol = "direct"

    for retry_no in range(1, MAX_ACCOUNT_RETRIES + 1):
        if retry_no > 1:
            wait_sec = random.uniform(8.0, 15.0)
            print(f"[Retry] {name} 第 {retry_no - 1} 次失败，等待 {wait_sec:.1f}s 后进行第 {retry_no} 次重试...")
            time.sleep(wait_sec)

        local_port = BASE_PROXY_PORT + index * 10 + retry_no
        proxy = SingBoxProxy(proxy_url, local_port=local_port)
        user_data_dir = tempfile.mkdtemp(prefix=f"ph_uc_profile_{index}_{retry_no}_")

        actions = []
        initial_state = "unknown"
        final_state = "unknown"
        current_url = ""
        error_msg = ""

        try:
            local_proxy = proxy.start()
            exit_ip = proxy.exit_ip or ""
            protocol = proxy.protocol
            sb_proxy = (
                local_proxy.replace("http://", "").replace("https://", "")
                if local_proxy
                else None
            )
            viewport = random.choice(VIEWPORT_POOL)

            with SB(
                uc=True,
                headless=False,
                proxy=sb_proxy,
                user_data_dir=user_data_dir,
                locale_code="en",
            ) as sb:
                try:
                    sb.driver.set_window_rect(
                        x=0, y=0, width=viewport["width"], height=viewport["height"]
                    )
                except Exception:
                    sb.set_window_size(viewport["width"], viewport["height"])

                try:
                    login_play_hosting(sb, email, password)
                    current_url = open_server_page(sb, server_id)

                    initial_state = detect_server_state(sb)
                    print(f"[Status] 初始服务器状态: {initial_state}")
                    state = initial_state

                    for attempt in range(1, 3):
                        if state == "limbo":
                            if not click_button_by_text(sb, "Wake server"):
                                raise RuntimeError("检测到 Limbo 状态，但未找到 Wake server 按钮")
                            actions.append(
                                "点击 Wake server 唤醒" if attempt == 1 else "掉回休眠，重新唤醒"
                            )
                            sb.sleep(3)
                            state = wait_for_restore_complete(sb)
                        elif state == "restoring":
                            actions.append("等待服务器恢复完成")
                            state = wait_for_restore_complete(sb)

                        if state == "offline":
                            click_start_button(sb, max_wait=45)
                            state = detect_server_state(sb)
                            if state in ("limbo", "restoring"):
                                continue
                            actions.append("点击 Start 开机")
                            sb.sleep(4)
                            state = wait_for_boot(sb, max_wait=120)
                        elif state == "starting":
                            if attempt == 1:
                                actions.append("等待启动完成")
                            state = wait_for_boot(sb, max_wait=120)
                        elif state == "online":
                            if attempt == 1:
                                actions.append("运行正常，无需操作")
                            break

                        if state in ("online", "starting"):
                            break
                        if state in ("limbo", "restoring") and attempt == 1:
                            print("[Action] 检测到服务器被踢回休眠，开始第 2 轮自动唤醒开机...")
                            continue
                        break

                    final_state = state
                    sb.sleep(2)
                    save_cdp_screenshot(sb, screenshot_path)
                    print(f"[Done] {name} 最终状态: {final_state}，截图: {screenshot_path}")

                except Exception as e:
                    error_msg = str(e)
                    print(f"[Error] {name} 第 {retry_no} 次执行异常: {error_msg}")
                    save_cdp_screenshot(sb, screenshot_path)
                    raise

        except Exception as e:
            if not error_msg:
                error_msg = str(e)
            print(f"[Failed] {name} 第 {retry_no} 次尝试失败: {error_msg}")
        finally:
            proxy.stop()
            shutil.rmtree(user_data_dir, ignore_errors=True)

        if not error_msg and final_state in ("online", "starting"):
            if retry_no > 1:
                actions.insert(0, f"第{retry_no}次重试成功")
            break

    ok = not error_msg and final_state in ("online", "starting")

    if error_msg:
        title = f"❌ [{index}/{total}] {name} 保活失败"
    elif initial_state in ("limbo", "offline", "restoring") and final_state in (
        "starting",
        "online",
    ):
        title = f"🚀 [{index}/{total}] {name} 已自动开机"
    elif ok:
        title = f"✅ [{index}/{total}] {name} 巡检正常"
    else:
        title = f"⚠️ [{index}/{total}] {name} 状态异常"

    proxy_info = (
        f"{protocol.upper()} ({exit_ip})"
        if exit_ip
        else ("直连" if not proxy_url else "代理连接失败")
    )
    action_text = " -> ".join(actions) if actions else "无"

    lines = [
        f"<b>{escape_html(title)}</b>",
        f"👤 账号: <code>{escape_html(masked)}</code>",
        f"🕒 时间: <code>{now_cst()}</code>",
        f"🌐 出口节点: <code>{escape_html(proxy_info)}</code>",
    ]
    if current_url:
        lines.append(f"🔗 面板: <code>{escape_html(current_url)}</code>")
    lines.extend(
        [
            f"📌 初始状态: {STATE_LABELS.get(initial_state, initial_state)}",
            f"🛠 执行动作: {escape_html(action_text)}",
            f"📊 最终状态: {STATE_LABELS.get(final_state, final_state)}",
        ]
    )
    if error_msg:
        lines.append(f"❗ 错误: <code>{escape_html(error_msg[:300])}</code>")

    send_telegram_report(
        bot_token=tg_token,
        chat_id=tg_chat_id,
        caption="\n".join(lines),
        image_path=screenshot_path,
    )

    return {
        "name": name,
        "email": masked,
        "ok": ok,
        "initial_state": initial_state,
        "final_state": final_state,
        "exit_ip": exit_ip or "N/A",
        "error": error_msg,
    }


def main() -> None:
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)

    tg_token = os.getenv("TG_BOT_TOKEN", "").strip()
    tg_chat_id = os.getenv("TG_CHAT_ID", "").strip()
    delay_min = max(5.0, parse_float_env("ACCOUNT_DELAY_MIN", 15.0))
    delay_max = max(delay_min, parse_float_env("ACCOUNT_DELAY_MAX", 40.0))
    shuffle_enabled = os.getenv("SHUFFLE_ACCOUNTS", "true").strip().lower() != "false"

    try:
        accounts = load_accounts()
    except ValueError as e:
        print(f"[Fatal] 配置错误: {e}")
        send_telegram_text(
            tg_token,
            tg_chat_id,
            f"❌ <b>Play.Hosting 配置错误</b>\n<code>{escape_html(str(e))}</code>",
        )
        sys.exit(1)

    if shuffle_enabled and len(accounts) > 1:
        random.shuffle(accounts)
        print(f"[Init] 共加载 {len(accounts)} 个账号（已随机打乱巡检顺序）")
    else:
        print(f"[Init] 共加载 {len(accounts)} 个账号")

    start_jitter = random.uniform(3.0, 12.0)
    print(f"[Init] 随机延迟 {start_jitter:.1f}s 后开始巡检...")
    time.sleep(start_jitter)

    results: List[Dict[str, Any]] = []
    total = len(accounts)

    for idx, account in enumerate(accounts, start=1):
        res = process_single_account(
            account=account,
            index=idx,
            total=total,
            tg_token=tg_token,
            tg_chat_id=tg_chat_id,
        )
        results.append(res)

        if idx < total:
            cooldown = random.uniform(delay_min, delay_max)
            print(
                f"[Cooldown] 账号 {account['name']} 处理完毕，空置冷却 {cooldown:.1f}s 后继续下一个账号..."
            )
            time.sleep(cooldown)

    success_count = sum(1 for r in results if r["ok"])
    fail_count = total - success_count

    if total > 1:
        summary_lines = [
            "<b>📋 Play.Hosting 多账号巡检汇总</b>",
            f"🕒 完成时间: <code>{now_cst()}</code>",
            f"📈 结果: 成功 <b>{success_count}</b> / 失败 <b>{fail_count}</b>（共 {total} 个）",
            "",
        ]
        for r in results:
            icon = "✅" if r["ok"] else "❌"
            line = (
                f"{icon} <b>{escape_html(r['name'])}</b> "
                f"({escape_html(r['initial_state'])} → {escape_html(r['final_state'])}) "
                f"| IP: <code>{escape_html(r['exit_ip'])}</code>"
            )
            summary_lines.append(line)

        send_telegram_text(tg_token, tg_chat_id, "\n".join(summary_lines))

    if fail_count > 0:
        print(f"[Exit] 存在 {fail_count} 个账号执行异常")
        sys.exit(1)

    print("[Exit] 全部账号巡检完成")


if __name__ == "__main__":
    main()
